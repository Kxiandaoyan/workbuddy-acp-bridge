# ZCode 本地劫持：免云端向桌面端注入消息并**实时显示**

把 ZCode 桌面版"手机远程控制"的 WebSocket 通道从云端中继（`wss://zcode.z.ai/ws`）接管到本机，
使外部程序（hermes 等）**无需云端、无需手机、无需扫码**，直接向桌面端会话注入消息。

与主桥 [`../zcode_aps.mjs`](../README.md) 的分工：

| | `../zcode_aps.mjs`（主桥，日常用） | 本目录 `hijack/`（可选增强） |
|---|---|---|
| 消息发送 | ✓（独立 app-server 实例执行） | ✓（**桌面自己的实例执行**） |
| 忙闲检验 | ✓（`--check/--wait-idle/--queue-busy`） | ✗（另有 `requestedDelivery:"queue"` 排队语义） |
| 桌面端实时显示 | ✗（客户端不刷新外部轮次，需切走切回） | **✓（同手机官方体验，流式上屏）** |
| 前置条件 | 无（随时可用） | 桌面版需带环境变量重启一次 |
| 恢复原状 | 不需要 | 退出后正常重启 ZCode 即回官方云通道 |

**经验法则：只发消息用主桥；要"人在桌面客户端上看着消息实时出现"才启用本方案。**

---

## 一、原理

ZCode 桌面版留有两个官方环境变量（生产包 app.asar 逆向所得）：

- `ZCODE_WEB_REMOTE_CONTROL_RELAY_WS_URL` —— 远程控制中继 WebSocket 地址（默认 `wss://zcode.z.ai/ws`）
- `ZCODE_WEB_REMOTE_CONTROL_URL` —— 扫码页指向的控制端网页地址

把它们指到本机假中继（`relay.mjs`，127.0.0.1:8899），桌面版就会按 device 协议连上来
（`device_register → auth 挑战应答 → 心跳`）。中继是我们的服务器：鉴权直接放行、
配对由我们定义（控制端发 `controller_register` 即配对成立，无需扫码）。

关键在桥的语义：桌面收到 `workspace-bridge-open` 后 **attach 到窗口自己的活跃实例**执行
（app.asar 代码原话：`workspace bridge active window=<windowId>`、`workspaceHostAttachment`），
桌面 UI 与远端控制端是同一 conversation topic 的订阅者（连接参数
`clientMode:"desktop-continuous" / "web-remote-replayable"`）。所以本通道进来的消息
**由桌面自己的实例处理、UI 实时流式显示**——这正是主桥做不到的"连贯显示"。

```
hermes ──WS──▶ relay.mjs(127.0.0.1:8899) ◀──WS── ZCode 桌面版
控制端              假中继(我们=服务器)              device(attach 到窗口实例)
                        │
                        ▼ 桥上 rpc-frame 双向流
              桌面自己的实例执行消息 → UI 实时上屏
```

---

## 二、协议栈（2026-09-18 静态逆向，源：app.asar + resources/glm/zcode.cjs）

自外向内四层。**每一层都是 strict schema，多一个字段/类型不对都会被静默拒收**
（这正是调试困难的原因，也是下文"排错"一节的方法论依据）。

### 1. 中继信封（WebSocket 文本帧）

```json
{ "type": "data", "payload": <rpc-frame | rpc-frame-ack>, "client_ts": 123, "server_ts": 123 }
```

中继控制消息（非 data）：`controller_register` / `controller_ack` / `controller_ping` /
device 侧 `device_register` / `device_register_ack` / `auth_init` / `auth_challenge` /
`auth_response` / `auth_ack` / `pair_status_query` / `pair_status_ack`
（配对成立值是 `"matched"`，桌面端 `applyPairStatus` 只认 `waiting/matched`）。

### 2. rpc-frame（传输层，分片 + CRC）

```json
{ "zcode_type": "rpc-frame",
  "bridgeSessionId": "...", "bridgeGeneration": 1, "recoveryId": "...",
  "seq": 1, "messageSeq": 1,
  "fragmentIndex": 0, "fragmentCount": 1,
  "messageBytes": 48,
  "checksum": { "algorithm": "crc32", "value": "0d1f2e3a" },
  "dataBase64": "..." }
```

- **`checksum.value` 必须是 8 位小写十六进制字符串**（`/^[0-9a-f]{8}$/`）。
  ⚠️ 历史教训：2026-09-16 首测把它发成了数字 → 传输层 zod 拒收 →
  `bridge-degraded: rpc-transport-fault`。内层 payload 当时根本没被解析。
- 分片上限 64 片、单逻辑消息 16MB、重组超时 30s；`fragmentIndex < fragmentCount`，
  `fragmentCount ≤ messageBytes`。
- 对端每个逻辑消息要回 `rpc-frame-ack`（带 `ackMessageSeq`）；`terminal.mjs` 已自动做。
- `terminal.mjs` 的 crc32 已对桌面首帧样本验证一致（`04 01 06 c8 01 00` → `b4ff6360`）。

### 3. AppPayload 桥接层（控制端 ↔ 桌面的建桥握手，走 `{type:"data",payload:{...}}`）

| zcode_type | 方向 | 作用 |
|---|---|---|
| `bootstrap-request/response` | 控→桌 / 桌→控 | 开场握手，response 带窗口/工作区概览 |
| `workspace-list-request/response` | 控→桌 / 桌→控 | 列可远程的工作区（`workspacePath/label/kind`） |
| `workspace-list-updated` | 桌→控 | 工作区列表变化推送 |
| **`workspace-bridge-open`** | 控→桌 | `{requestId, bridgeSessionId, workspaceKey, taskId?}` 建桥 |
| `workspace-bridge-ready/error` | 桌→控 | 建桥成功/失败（成功后桥上开始跑 rpc-frame 流） |
| `bridge-degraded` | 桌→控 | 传输层判废（原因：`rpc-transport-fault/rpc-frame-gap/buffer-overflow/buffer-timeout`） |
| `mobile-view-state-update` / `platform-request/response` / `mobile-diagnostic` | — | 手机端 UI 态、平台调用、诊断（可忽略） |

### 4. 内层 conversation topic 协议（桥上重组后的 payload，v4）

逻辑帧（JSON 形式，`wireVersion:3`）：

```json
{ "wireVersion": 3, "kind": "complete", "deliveryKind": "initial",
  "logicalFrameId": "<uuid>", "logicalFrameOrdinal": 1,
  "topic": "conversation/<sessionId>",
  "subscriptionId": "<id>",
  "frame": <命令信封> }
```

命令信封与词汇表（zcode.cjs `G0n`/`dWi`，节选常用）：

```json
{ "commandId": "<uuid>", "clientId": "<uuid>", "sessionId": "sess_...",
  "baseRevision": 12, "baseLogEpoch": "...",       // CAS 语义，部分命令必带
  "type": "sendText",
  "payload": { "text": "你好", "requestedDelivery": "startNow | queue | guide",
               "heldQueueDisposition": "keepQueueAndSend", "modelSelection": {...} },
  "issuedAt": 1234567890123 }
```

| type | 用途 |
|---|---|
| `sendText` | **发消息**（`requestedDelivery:"queue"` = 排队不打断，对应主桥 `--queue-busy`） |
| `createSession` / `createSelectionSideSession` | 开新会话 |
| `stop` / `compact` / `retryTurn` / `forkAssistant` / `editUserQuery` | 轮次控制 |
| `sendQueuedNow` / `editQueueItem` / `reorderQueueItem` / `deleteQueueItem` / `setAutoDrain` | 队列管理 |
| `resolveInteraction` | 回应交互（权限/提问） |
| `switchModelConfig` / `switchCollaborationMode` | 切模型 / 协作模式 |
| `renameSession` / `deleteSession` | 会话管理 |

> **待实测**：内层是否直接接受 JSON 逻辑帧。9/16 首测死于第 2 层 checksum（数字），
> 第 4 层从未被真正试过。桌面建桥后主动发来的首帧是 6 字节二进制
> `04 01 06 c8 01 00`，提示通道层可能还有一层 varint-TLV 二进制封装
> （尺寸公式已逆向：varint 长度前缀、topic ≤204 字节、固定头 13B）。
> 若 JSON 帧被拒，按"排错"一节迭代。

---

## 三、文件清单

| 文件 | 作用 |
|---|---|
| `relay.mjs` | 假中继（零依赖纯 Node，默认 127.0.0.1:8899；HTTP API：`GET /status`、`POST /pair`、`POST /frame`、`POST /send`） |
| `terminal.mjs` | **控制端注入器**：建桥全流程 + 分片重组 + `sendText` 注入（`--send --session <id> --text "..."`；不带参数=观察模式） |
| `start-zcode-local.cmd` | 带两个官方环境变量启动桌面版（自动探测 ZCode.exe，`ZCODE_EXE` 可覆盖） |
| `send.mjs` | 命令行小工具：`status / pair / frame <json> / send <text>`（走中继 HTTP API） |
| `attach-controller.mjs` | 观察端：注册为控制端，dump 桌面发来的一切帧（调试用） |
| `selftest.mjs` | 不碰真实 ZCode 的闭环自测（模拟 device 握手 + 控制端转发） |

---

## 四、启用步骤（完整 walkthrough）

前置：Node ≥ 22（免依赖，直接跑）；ZCode 桌面版 ≥ 3.12。

```bash
# 1. 起中继（保持运行；不影响连官方云的桌面版）
node relay.mjs

# 2. 自测中继（可选，应输出 SELFTEST PASS）
node selftest.mjs

# 3. 重启桌面版进劫持模式：先手动退出 ZCode，再双击（或命令行）
start-zcode-local.cmd

# 4. 在桌面客户端里点开"手机远程控制"入口（触发桌面版连中继）
#    relay 控制台/hijack.log 应出现: device_register / auth_init / auth_response

# 5. 注入消息（观察模式先跑一遍，确认 bootstrap → list → bridge-ready 全通）
node terminal.mjs                                        # 观察模式
node terminal.mjs --send --session sess_xxx --text "测试"  # 注入
```

预期：第 5 步后**桌面客户端对应会话实时出现消息并开始处理**（与手机官方体验一致）。

环境变量：`HIJACK_PORT`（中继端口，默认 8899）、`HIJACK_RELAY`（terminal 连接地址）、
`HIJACK_SEND_DELAY_MS`（建桥后延迟注入毫秒数，默认 2500）。

---

## 五、验证与排错

**看两处日志**：`relay.mjs` 控制台/`hijack.log`（中继侧全量帧）；
`~/.zcode/v2/logs/` 搜 `[web-remote-control]`（桌面侧，含
`invalid external relay payload dropped` 及字段级报错）。

| 症状 | 含义 / 处理 |
|---|---|
| 桌面不连中继 | 环境变量没带上：必须经 `start-zcode-local.cmd` 启动；确认 ZCode 完全退出过（托盘也算） |
| `auth` 后一直 `waiting` | 配对未成立：控制端发 `controller_register`（`terminal.mjs` 自动做），或 `POST /pair` |
| `bridge-degraded: rpc-transport-fault` | 传输层 schema 拒收。先查 `checksum.value` 是不是 8 位小写 hex；再查字段多余/类型（全部 strict） |
| `bridge-degraded: rpc-frame-gap` | `seq`/`messageSeq` 不连续——每方向各自单调递增，不能重号 |
| 桥建成但注入无反应 | 内层格式不对：看桌面日志 `[web-remote-control]` 的具体字段报错，对照协议栈第 4 层修正 |
| 桌面升级后全挂 | 按第六节重新逆向 |

---

## 六、协议迭代指南（桌面版升级后）

1. **重新提取 schema**：`app.asar`（Electron 主包）与 `resources/glm/zcode.cjs`（CLI）里
   全部 schema 以明文 zod 存在，检索关键字：`webRemoteControl`、`zcode_type`、
   `rpc-frame`、`workspace-bridge-open`、`G0n`（命令表）、`conversation/`。
2. **对照日志修代码**：桌面对不认识的帧会记 `~/.zcode/v2/logs/`（字段级报错），
   中继侧 `hijack.log` 有全量原始帧，两端对齐着改。
3. **终极备选——抓真实样本**：把 `relay.mjs` 改成转发模式（上行原样转
   `wss://zcode.zai/ws`、双向落日志），真机扫码配对一次，即得完整合法帧序列照抄。

---

## 七、恢复官方通道

正常退出 ZCode 后直接重新打开（不带环境变量）即回官方云中继；`relay.mjs` 可停可留。
本方案不修改任何系统配置（不改 hosts、不装证书——全靠官方环境变量），卸载零残留。

## 八、边界与注意

- 启用期间**真机手机远程控制不可用**（桌面只能连一个中继）；退出劫持模式即恢复。
- 配对语义独占：同一 device_sid 一个控制端（本实现允许多控制端连接，均转发）。
- 本方案是本机自有软件的本地互操作（官方提供的环境变量开关 + 本机回环地址），
  不涉及云端或他人系统。
