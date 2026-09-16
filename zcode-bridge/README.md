# ZCode Bridge — 外部 agent 给 ZCode 会话发消息（带完整上下文）

本子目录解决多 agent 协作的另一半：**借用 ZCode 里已有会话的上下文继续聊**。

[WorkBuddy 桥接](../README.md) 追求的是“消息实时显示在界面”；ZCode 桥接追求的是
**“用上指定会话的完整上下文，把结果拿回来”**——一个外部 agent（Claude / Codex /
Hermes / OpenClaw / WorkBuddy / 任何能跑 Python 的进程）只要知道 sessionId，
就能让 ZCode 在原会话上接着干活。

## 为什么需要它

多 agent 协作最常见的浪费：外部 agent 在别的体系里跑完分析/回测，想让 ZCode
接着写代码，结果只能新开会话——上下文是空的，之前聊过什么、做过什么决定、
记过什么约定，全要重讲一遍。

ZCode 的 `--resume <sessionId>` 就是官方的“进入指定会话续聊”机制。实测确认：

1. **不 fork，就在原会话上追加**——`--resume` 后返回的 sessionId 一字不差，
   该目录的会话行数不变，消息在原会话上追加。
2. **上下文真的带上**——第一轮让会话记住暗号“蓝鲸七号”，**换一个全新进程**
   `--resume` 进去问“暗号是什么”，回答“蓝鲸七号”。跨进程上下文召回没问题。
3. **桌面端创建的会话同样能续**——CLI 和桌面版底层共用同一个会话库
   （`~/.zcode/cli/db/db.sqlite`），格式天然一致。

## 前提条件

- 已安装 **ZCode 桌面版**（CLI 包在 `resources/glm/zcode.cjs`，工具会自动找）。
- 模型凭据自动从 `~/.zcode/v2/config.json` 读当前启用的 provider，也可用环境变量
  `ZCODE_MODEL` / `ZCODE_BASE_URL` / `ZCODE_API_KEY` 覆盖。
- Python 3（仅标准库）。需要能执行 node（工具会找系统 node 或自带 node）。

## 用法

### 1. 列出会话（含忙闲标记）

```bash
python zcode_send.py --list                       # 全部最近会话
python zcode_send.py --list --cwd "C:\path\to\project"   # 某个项目目录的会话
```

```
session=sess_700837dc-... busy=False cwd=C:\...\project title=记住暗号：蓝鲸七号...
```

### 2. 给指定会话发消息（用它的完整上下文继续）

```bash
python zcode_send.py --session-id sess_700837dc-... --msg "暗号是什么？"
```

不知道 sessionId 时，按目录自动取最新会话：

```bash
python zcode_send.py --cwd "C:\path\to\project" --msg "继续上次的任务"
```

强制新开会话（不借用上下文）：

```bash
python zcode_send.py --cwd "C:\path\to\project" --msg "..." --new
```

### 3. 只检查忙闲，不发消息

```bash
python zcode_send.py --session-id sess_xxx --check
```

| 退出码 | 输出 | 含义 |
|---|---|---|
| 0 | `IDLE\|会话空闲，可以发送。\|session=...` | 空闲，可发 |
| 4 | `BUSY\|现在会话正忙，请稍后再发。\|session=...` | 正忙，别发 |

### 4. 忙时等待 / 不闯入

```bash
python zcode_send.py --session-id sess_xxx --msg "..." --wait-idle 60
```

仍在忙就每 2 秒轮询，直到空闲或超时（超时同样以退出码 4 返回 `BUSY|...`）。

> **同一会话同时只应有一个调用方在发**。ZCode 正在跑任务时往同一会话再发消息
> 有覆盖风险——拿到 BUSY 就退出，等它空闲再发。

## 繁忙检测的原理（及为什么不用 turn_usage）

会话库 `~/.zcode/cli/db/db.sqlite` 里有 `turn_usage` 表，`status` 取值
`running / completed / error / cancelled`。**但它不可靠**：读 CLI 源码发现
`upsertTurnUsage` 是在 turn **结束**时才写的（携带 `completedAt`），进行中的
turn 在表里根本没有行（实测 `SELECT COUNT(*) FROM turn_usage WHERE
status='running'` 在任务跑到一半时仍是 0）。

可靠的实时信号在 **`message` 表**：每条 assistant 消息的 JSON `data.time` 对象，
任务进行中**没有 `completed` 键**，完成后才有。实测：

- 任务跑到一半：尾部 assistant 消息 → `has_completed == False` → 判忙 ✅
- 任务正常结束：尾部 assistant 消息 → `has_completed == True` → 判闲 ✅
- 尾部是裸 user 消息（刚排队，agent 还没开工）→ 判忙

这个判据同时覆盖“被强杀的会话”——没正常收尾的会话尾部消息永远没有
`completed`，`--list` 里会正确标成 busy，提醒你别去碰它。

## 会话 id 从哪来

1. 每次 `--list` 的输出里；
2. 会话库（只读查询）：`~/.zcode/cli/db/db.sqlite` 的 `session` 表按 `directory`
   过滤；
3. 桌面端客户端里打开的会话也在同一个库——**桌面端创建的会话同样能用
   `--session-id` 续聊**。

## 与 WorkBuddy 桥接的分工

| | WorkBuddy 桥接（上层目录） | ZCode 桥接（本目录） |
|---|---|---|
| 目标 | 消息**实时显示**在 PC 客户端界面 | **借用指定会话的上下文**接着干 |
| 机制 | 直连 daemon 的 interactive session，走 ACP 协议 | CLI `--resume`，官方续聊机制 |
| 界面刷新 | ✅ 实时 | ❌ 追加的轮次要重新点开该会话才能看到 |
| 上下文 | 用会话已有上下文 | ✅ 同样用会话已有上下文 |

单纯“用上下文 + 拿结果”，CLI 这条路就够了，而且更稳（不依赖端口发现、
不依赖会话激活）。只有你还想要“消息实时显示在界面”时，才需要上层的
WorkBuddy 桥接。

## 已知限制

- 会话必须在 ZCode 里**曾经存在**（库里要有这条 session 记录）。
- 追加的轮次**不会**在已打开的桌面端界面上实时刷新，重新点开该会话可见完整记录。
- 繁忙检测读的是共享库的最新状态，属于本地最优估计；极端情况下（agent 恰在
  两种状态之间）可能有几秒延迟。
- 仅在 Windows + ZCode 桌面版环境下验证过。
