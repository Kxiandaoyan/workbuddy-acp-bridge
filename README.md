# Agent Bridges（WorkBuddy / ZCode / Hermes）

**本仓库提供三个桥接工具，让其他 agent（Claude、Codex、Hermes、OpenClaw 等）
投递消息到用户桌面上已经打开、已有完整上下文的会话——不用新开会话、不用重讲背景。**

| 桥接 | 投递目标 | 一句话定位 | 入口 |
|---|---|---|---|
| **WorkBuddy 桥接** | WorkBuddy（CodeBuddy Code）PC 客户端 | 消息**实时显示**在客户端界面，用户看得见 | [`acp_live_send.py`](#文件)（本目录） |
| **ZCode 桥接** | ZCode 桌面端 / CLI | **借用指定会话的上下文**接着干，把结果拿回来 | [`zcode-bridge/`](zcode-bridge/README.md)（子目录） |
| **Hermes 桥接** | Hermes Desktop（NousResearch hermes-agent） | 消息**实时显示**在桌面端界面（多客户端 fan-out） | [`hermes-bridge/`](hermes-bridge/README.md)（子目录） |

三者都用 Python 标准库写成、零依赖、都带忙闲检测，且**忙闲输出格式完全一致**
（`IDLE|...` / `BUSY|现在会话正忙，请稍后再发。|...`，退出码 0/4），
调用方可以用同一套逻辑对接任意目标。按需选一个：

- 想让用户**在 WorkBuddy 界面上实时看到**外部 agent 发来的消息 → WorkBuddy 桥接
- 只想让外部 agent **用上某会话的上下文把活干完、把结果拿回来** → ZCode 桥接
- 想给 **Hermes Desktop** 里打开的会话发消息（实时显示 + 原上下文干活）→ Hermes 桥接

> 下文是 **WorkBuddy 桥接**的完整说明；ZCode 桥接见
> [`zcode-bridge/README.md`](zcode-bridge/README.md)，Hermes 桥接见
> [`hermes-bridge/README.md`](hermes-bridge/README.md)。

---

# WorkBuddy 桥接（`acp_live_send.py`）

**直接给 WorkBuddy 桌面客户端里已经打开的会话发消息，消息在 PC 客户端界面
实时显示，并把 WorkBuddy 的回复拿回来。**

## 为什么需要它

多 agent 协作里最常见的痛点：你在 Claude / Codex / Hermes / OpenClaw 那边跑分析、
跑回测、跑工程任务，但真正有完整代码上下文、有工具权限、有项目历史的是
WorkBuddy（CodeBuddy Code）里那个已经打开的会话。如果让外部 agent 新开一个会话，
上下文是空的，什么都要重讲一遍。

这个桥接解决的就是这一步：**把外部 agent 的结论/指令，投递到用户桌面上那个
“懂行”的 WorkBuddy 会话里**，用户在自己的 PC 客户端里就能实时看到消息和回复，
不用切换窗口、不用复制粘贴。

```
外部 agent（Claude / Codex / Hermes / OpenClaw / 任何能跑 Python 的进程）
   │  acp_live_send.py  -- 本地 127.0.0.1，无需任何密码或令牌
   ▼
WorkBuddy PC 客户端的 interactive session（客户端自己 daemon 激活的）
   │  daemon → 主进程 wb:event → 渲染进程
   ▼
PC 客户端对话界面实时显示消息 + WorkBuddy agent 的回复
```

**安全模型**：全部流量都在 `127.0.0.1` 上。WorkBuddy 的 ACP 主通道对 loopback
做了豁免，`POST /api/v1/acp/connect` 直接下发一次性的 `connectionId` + `sessionToken`，
不需要任何长期密钥、不需要登录、不需要配置。本工具也不读写任何凭证文件。

## 文件

| 文件 | 说明 |
|---|---|
| `acp_live_send.py` | **WorkBuddy 桥接核心工具**。自动发现活跃会话端点 → 免密 connect → load → prompt。Python 标准库，零依赖。 |
| `acp_live_test.py` | 最小冒烟测试（手动传端口），用来单独验证 ACP 四步调用链。 |
| `zcode-bridge/zcode_send.py` | **ZCode 桥接核心工具**。按 sessionId 或项目目录进入指定会话续聊（`--resume`），借用完整上下文。 |
| `zcode-bridge/README.md` | ZCode 桥接的完整文档（原理 / 繁忙检测 / 与本桥接的分工）。 |
| `hermes-bridge/hermes_send.py` | **Hermes 桥接核心工具**。发现 Desktop 的 serve 后端 → WS JSON-RPC `prompt.submit`，消息实时进桌面端会话。 |
| `hermes-bridge/README.md` | Hermes 桥接的完整文档（架构 / 发现链 / busy 语义 / 多客户端安全）。 |
| `README.md` | 本文档（三个桥接的总说明 + WorkBuddy 桥接详细用法）。 |

## 用法

只需要 Python 3（标准库即可，无第三方依赖）。

### 1. 列出当前活跃的会话

```bash
python acp_live_send.py --list
```

```
pid=39092  port=13416  session=<uuid>  cwd=C:\path\to\project
```

> **前提**：目标对话必须在 PC 客户端里**打开/聚焦过**，客户端的 daemon 才会为它
> 激活 interactive session；空闲一段时间后 session 会被回收（端口随之消失），
> 此时发消息会报 “no live interactive session”。**加 `--ensure` 可以自动重新
> 激活**（见 2b），或者在客户端里点一下那个对话。

### 2. 发消息（用 sessionId 精确指定，推荐）

```bash
python acp_live_send.py --session-id <uuid> --msg "回测跑完了，把结果汇总到报告里"
```

也可以按项目路径自动匹配会话（不用记 UUID）：

```bash
python acp_live_send.py --cwd "C:\path\to\project" --msg "..."
```

### 2b. 会话没活端点？自动激活（`--ensure`）

会话空闲一段时间后会被客户端回收回预热池，此时直接发会报
“no live interactive session”。**不用手动去客户端点那个对话**，
加 `--ensure` 就行——工具会打开 `workbuddy://chat/<sessionId>` 协议链接，
客户端接到后自动把该对话重新激活成一个活会话：

```bash
python acp_live_send.py --session-id <uuid> --msg "..." --ensure
```

> 前提是 PC 客户端正在运行，且系统已注册 `workbuddy://` 协议（安装客户端时
> 自动注册）。`--ensure` 需要 `--session-id`（按 cwd 匹配时无法确定要激活
> 哪个对话）。这条路径让外部 agent 可以**完全无人值守**地投递消息。

### 3. 只检查忙闲，不发消息

```bash
python acp_live_send.py --session-id <uuid> --check
```

输出固定为一行 `状态|提示语|session=...|last_status=...`，退出码区分状态，
调用方可以直接取提示语转发给自己的用户：

| 退出码 | 输出 | 含义 |
|---|---|---|
| 0 | `IDLE\|会话空闲，可以发送。\|...` | 空闲，可发 |
| 4 | `BUSY\|现在会话正忙，请稍后再发。\|...` | 正忙，别发 |

### 4. 忙时等待 / 不闯入

```bash
python acp_live_send.py --session-id <uuid> --msg "..." --wait-idle 60
```

仍在忙就每 2 秒轮询一次，直到空闲或超时（超时同样以退出码 4 返回
`BUSY|...`），避免打断 WorkBuddy 正在执行的任务。

> **同一会话同时只应有一个调用方在发**。拿到 BUSY 就退出，不要重试轰炸。

## 多 agent 协作的典型姿势

- **分工投递**：Hermes 负责数据采集和回测，把结论用一句话发给 WorkBuddy 会话，
  让有项目上下文的 WorkBuddy 接着写代码 / 改策略 / 出报告。
- **人工在环**：外部 agent 每次投递前先 `--check`，忙就返回
  “现在会话正忙，请稍后再发。”，用户决定什么时候再试——消息不会
  静默丢失，也不会互相覆盖。
- **双向闭环**：`acp_live_send.py` 把 WorkBuddy 的回复尾部打到 stdout，
  外部 agent 可以直接读回来继续处理。

## ACP 调用序列（排错时照着对）

```
POST /api/v1/acp/connect   （无 body，免密）    → {"connectionId":..., "sessionToken":...}
POST /api/v1/acp  headers: acp-connection-id + acp-session-token + x-codebuddy-request:1
POST /api/v1/acp           initialize           → 握手 + agentCapabilities
POST /api/v1/acp           session/load   {sessionId, cwd, mcpServers:[]}
POST /api/v1/acp           session/prompt {sessionId, prompt:[{type:"text",text:...}]}
                             → SSE 流：agent_message_chunk / tool_call / session_end
```

- **`session/load` 必须带 `mcpServers: []`**，否则报
  `Invalid params: mcpServers expected array`。
- **`session/prompt` 必须带 `sessionId`**，否则报 `sessionId expected string`。
- prompt 的 SSE 是 chunked 流，agent 跑完才结束；检测到
  `finishReason:stop` / `outcome:SUCCESS` 后主动断开，避免长连接卡死。
- **端点发现**：`~/.workbuddy/sessions/<pid>.json` 里 `kind == "interactive"`
  且 `sessionId` 匹配的记录 → 取 `pid` → `netstat -ano` 找该 pid 在
  127.0.0.1 上的 LISTENING 端口。**端口每次都变**（session 用完会被回收
  回 prewarm 池），必须每次实时发现，不能固化。

## 已知限制

- 只能投递到 PC 客户端**已经打开过的**会话（客户端未激活的对话没有活端点）。
- 同一会话一次只能处理一条消息；并发投递会互相打断。
- 忙闲判断依赖读取会话 transcript 文件的尾部状态，属于本地最优估计，
  极端情况下（例如 agent 刚好在两种状态之间）可能有几秒延迟。
- 仅在 Windows + WorkBuddy PC 客户端环境下验证过。

## 许可

MIT。

## ZCode 桥接：本仓库的第二个工具

如果协作对象是 **ZCode**（而不是 WorkBuddy），不用另找工具——同仓库的
[`zcode-bridge/`](zcode-bridge/README.md) 子目录提供了对等的桥接：

```bash
python zcode-bridge/zcode_send.py --list                              # 列会话（含 busy 标记）
python zcode-bridge/zcode_send.py --session-id sess_xxx --msg "..."    # 借用指定会话上下文续聊
python zcode-bridge/zcode_send.py --cwd "C:\path\to\project" --msg "..."  # 按项目目录自动取会话
```

它走 ZCode 官方的 `--resume <sessionId>` 续聊机制，**实测不 fork**（就在原会话
追加，sessionId 不变）、跨进程上下文召回成功、桌面端创建的会话同样能续。
繁忙检测同样有，且**输出格式和本桥接完全一致**（`IDLE|...` / `BUSY|...`，
退出码 0/4），调用方可以用同一套解析逻辑对接两个目标。

### 三个桥接怎么选

| | WorkBuddy 桥接 | ZCode 桥接 | Hermes 桥接 |
|---|---|---|---|
| **投递目标** | WorkBuddy（CodeBuddy Code）PC 客户端 | ZCode 桌面端 / CLI | Hermes Desktop（hermes-agent） |
| **核心机制** | 直连 daemon 的 interactive session，走 ACP 协议 | CLI `--resume`，官方续聊机制 | Desktop 自带 serve 后端的 WS JSON-RPC `prompt.submit` |
| **界面实时刷新** | ✅ 消息实时显示在客户端对话界面 | ❌ 追加轮次需重新点开该会话才见 | ✅ 多客户端 fan-out，桌面端实时显示 |
| **借用上下文** | ✅ 用会话已有上下文 | ✅ 同样用会话已有上下文 | ✅ 原会话上下文继续干活 |
| **依赖** | PC 客户端在运行 + 会话已激活（可 `--ensure` 自动激活） | ZCode 已安装 + 会话库可读 | Hermes Desktop 在运行（它 spawn serve 后端） |
| **典型场景** | 人工在环：让用户在界面上看到外部 agent 的投递 | 无人值守：借上下文把活干完、把结果拿回来 | 跨 agent 协作：给 Hermes fleet 里的活跃会话派活 |

一句话：**要“看得见”用 WorkBuddy/Hermes 桥接，要“接着干”用 ZCode 桥接。**
完整说明见 [`zcode-bridge/README.md`](zcode-bridge/README.md) 与
[`hermes-bridge/README.md`](hermes-bridge/README.md)。
