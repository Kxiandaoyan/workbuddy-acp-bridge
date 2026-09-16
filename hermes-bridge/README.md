# Hermes Desktop Bridge

**让外部 agent（WorkBuddy / Claude / Codex / 任何能跑 Python 的进程）给 Hermes
Desktop（PC 客户端）里已经打开的会话发消息，消息实时显示在客户端界面，agent
在原会话上下文里继续干活。**

与 WorkBuddy 桥接（`../acp_live_send.py`）和 ZCode 桥接（`../zcode-bridge/`）
同一套用法约定：`--list` / `--check` / `--msg` / `--wait-idle`，忙闲输出格式
完全一致。

## 原理（一句话）

Hermes Desktop 是 Electron 壳，后端是它自己 spawn 的 headless `hermes serve`
进程（每个 profile 一个），暴露 **JSON-RPC over WebSocket**（`/api/ws`）。
桌面端渲染进程就是这条 WS 的一个客户端——**网关原生多客户端 fan-out**，
所以桥接作为第二个客户端连上去发 `prompt.submit`，消息会出现在所有已打开
该会话的界面里，且断开不影响别的客户端。

```
外部 agent
   │  hermes_send.py（纯标准库）
   ▼
%APPDATA%/Hermes/backend-ownership.json  → 各 profile 的 serve PID
   │  netstat 找监听端口（serve shim 的 python 子进程才是真服务）+ GET / 探测确认
   ▼
GET http://127.0.0.1:<port>/  →  window.__HERMES_SESSION_TOKEN__（loopback 免登录）
   ▼
WS /api/ws?token=...  →  JSON-RPC
   ├─ session.list        列会话（DB 全量）
   ├─ session.most_recent 最近会话
   ├─ session.active_list 活跃会话 + status（busy 检测用）
   └─ prompt.submit       发消息 → {status: streaming}
```

## 用法

```bash
python hermes_send.py --list                                  # 列所有后端 + 会话
python hermes_send.py --profile <name> --list                 # 指定 profile 的会话
python hermes_send.py --profile <name> --check                # 查该 profile 最近会话忙闲
python hermes_send.py --profile <name> --msg "消息"            # 发到该 profile 最近会话
python hermes_send.py --session-id <sid> --check              # 只查忙闲
python hermes_send.py --session-id <sid> --msg "消息"          # 发消息（跨后端自动定位）
python hermes_send.py --session-id <sid> --msg "..." --wait-idle 60   # 忙时等待
python hermes_send.py --session-id <sid> --msg "..." --force-busy     # 忙时也发（会重定向在跑的 turn）
```

**多 profile 说明**：Desktop 会为每个 profile spawn 一个独立的 `hermes serve`
后端。`--profile` 按 serve 进程命令行里的 `--profile <name>` 匹配（Windows 上
经 CIM 读取；受限沙箱里降级为 `(unknown)`，此时用 `--session-id` 跨后端自动
定位更可靠——工具会遍历所有后端找到拥有该会话的那个）。

**DB-only 会话自动激活**：目标会话若没在 Desktop 里打开过（gateway 内存里
没有），`prompt.submit` 会报 `session not found`——工具自动先 `session.resume`
把它拉成 live（与用户在 Desktop 里点开一个历史会话完全同路径），再发送。

**忙闲输出约定**（与 WorkBuddy / ZCode 桥接一致）：

| 退出码 | 输出 | 含义 |
|---|---|---|
| 0 | `IDLE\|会话空闲，可以发送。\|session=...` | 空闲，可发 |
| 4 | `BUSY\|现在会话正忙，请稍后再发。\|session=...\|status=streaming` | 正忙，别发 |

## 关键设计决策

### 为什么 busy 检测用 `session.active_list` 而不是别的

`prompt.submit` 遵循 `display.busy_input_mode`（默认 `interrupt`）——**忙时
提交会重定向/打断正在跑的 turn**。桌面端用户正在看的那轮任务会被劫持。所以
桥接**必须**先查再发：`session.active_list` 返回每个 live 会话的
`status`（idle/streaming/…），纯只读、不建 agent、不绑 transport。

（`session.resume` 也返回 `running`，但它会 build agent + attach transport，
重量级且有副作用，不适合做探针。）

### 为什么安全（不抢桌面端的连接）

网关的 transport 是 **viewers 集合（多路订阅）**，`_rebind_live_transport`
是 attach 不是 replace；桥接 WS 断开时走 `_detach_transport_from_sessions`，
还有其他 viewer 的会话继续流式，不会被 reap。实测发消息后桌面端正常收到。

### token 获取

serve 后端在 loopback 模式下 `GET /` 返回注入
`window.__HERMES_SESSION_TOKEN__` 的页面（桌面端 Electron 启动时也这么拿）。
桥接照抄这条官方路径，无任何凭证落盘。gated（OAuth）模式下拿不到 token，
工具会明确报错。

## 限制

- Windows 实测（backend-ownership.json 是 Desktop 的 Electron userData 路径，
  其他 OS 路径不同但机制相同：`~/Library/Application Support/Hermes/` 等）。
- 需要 Hermes Desktop 正在运行（它才是 serve 进程的 owner）。
- 同一会话一次一个 turn；并发投递会排队或重定向，请配合 `--check`。
- `--session-id` 用 `session.list` 的 DB id（`YYYYMMDD_...` 时间戳格式）或
  `session.active_list` 的 runtime id 都行——busy 检测两个都匹配。
