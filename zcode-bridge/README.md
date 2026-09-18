# ZCode 桥接（hermes → ZCode 指定会话）

让外部 agent（hermes / Claude / Codex / 任何进程）给 ZCode 桌面端的**指定会话**发消息，
借用该会话的完整上下文继续干活，把回复拿回来。

## 两种版本，按 ZCode 版本选用

| | `zcode_aps.mjs`（**Node.js，新版**） | `zcode_send.py`（**Python，旧版**） |
|---|---|---|
| 适用 ZCode | **≥ 3.12（升级后，2026-09-17 起）** | **≤ 0.16.5（升级前）** |
| 模型 | **真 GLM-5.3**（账号套餐，可切 Flash/档位） | 默认落到可解析的个人 provider（如本地 aeon） |
| 原理 | app-server 协议 + 账号权益推送 + 逐次模型选择 | headless CLI `--resume` + 环境变量模型配置 |
| 单次耗时 | ~90-120 秒（进程启动 + 会话物化） | ~10-30 秒 |
| 在新版 ZCode 上的限制 | 无（就是为此设计的） | 桌面创建的会话 `--resume` 报 `Model creation failed`（账号 provider 解析不了），只能开新会话或接受 aeon 改写 |
| 依赖 | Node ≥ 22（用 `node:sqlite`） | Python 3 标准库 |

> 经验法则：**ZCode 升级后一律用 Node 版**；Python 版留着兼容旧安装/快速场景。

---

## 新版：`zcode_aps.mjs`（Node.js，适用 ZCode ≥ 3.12）

```bash
node zcode_aps.mjs <工作区目录> <sessionId> "消息"
# 例：
node zcode_aps.mjs D:\hermes\code sess_35fd7fb5-6a61-4a72-8479-e104be06227c "继续刚才的分析"
# 忙闲检验（与 WorkBuddy/Hermes/旧版 ZCode 桥同一约定，秒回、不发送）：
node zcode_aps.mjs <目录> <sessionId> x --check            # IDLE|... 退出 0 / BUSY|... 退出 4
# 发送默认带忙闸：目标会话正在跑轮次时拒发（BUSY 退出 4），不会打断也不会排队；
node zcode_aps.mjs <目录> <sessionId> "..." --wait-idle 60  # 忙时最多等 60 秒直到空闲
node zcode_aps.mjs <目录> <sessionId> "..." --queue-busy    # 忙时也发：进入会话队列，
                                                            # 当前轮结束后被下一轮消化
# 可选环境变量：
#   HERMES_APS_MODEL=GLM-5.3-Flash   换模型（默认 GLM-5.3）
#   HERMES_APS_LEVEL=high            思考档位（默认 max）
#   HERMES_APS_WAIT=90000            等回复时长 ms（默认 60000）
```

**忙闲信号**（与 `zcode_send.py` 同源）：共享库 `message` 表尾部——assistant 消息的
`time` 缺 `completed` = 轮次进行中；尾部是未应答的 user 消息 = 有排队。忙闲输出
约定与三桥一致：

| 退出码 | 输出 | 含义 |
|---|---|---|
| 0 | `IDLE\|会话空闲，可以发送。\|session=...` | 空闲，可发 |
| 4 | `BUSY\|现在会话正忙，请稍后再发。\|session=...` | 正忙，被拒发 |

回复打印到 stdout，进度日志走 stderr（开头会打印 `[paths]` 三行，核对自动发现的路径）。
sessionId 查法：`~/.zcode/cli/db/db.sqlite` 的 `session` 表按 `directory` 过滤，或旧版工具
`python ../zcode-bridge/zcode_send.py --list`。

### 路径全部自动发现，无需改代码

脚本不含任何绝对路径，按 `~/.zcode` 为根自动找 CLI 安装位置（Windows/macOS/Linux
常见路径逐一探测）、runtime builtin 配置（版本倒序取最新）、personal provider 配置、
模型 API key（v2/config.json 的 bigmodel 条目，找不到则取任一启用 provider）、
builtin revision（从 CLI 日志提取）。**唯一前提：ZCode 桌面版在这台机器上至少
运行过一次**（否则上述文件尚未生成）。

特殊安装可用环境变量覆盖：

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `ZCODE_HOME` | ZCode 数据根目录 | `~/.zcode` |
| `ZCODE_CLI` | zcode.cjs 完整路径 | 自动搜索常见安装位置 |
| `ZCODE_RUNTIME_DIR` | runtime/provider 目录 | `<ZCODE_HOME>/v2/runtime/provider/<平台>-<架构>` |
| `HERMES_APS_API_KEY` | 模型 API key | v2/config.json 自动提取 |
| `HERMES_APS_MODEL` / `HERMES_APS_LEVEL` | 模型 / 思考档位 | `GLM-5.3` / `max` |
| `HERMES_APS_PROVIDER` | 账号 provider id | `account:bigmodel-individual-coding-plan` |
| `HERMES_APS_MODELS` | 注册表声明的模型列表（逗号分隔） | `GLM-5.3-Flash` |
| `HERMES_APS_WAIT` / `HERMES_APS_BOOT_WAIT` | 等回复 / 等启动毫秒 | `60000` / `35000` |

找不到路径时脚本会报错并列出尝试过的位置，按提示设对应环境变量即可。

### 原理（复刻桌面版驱动 CLI 的官方姿势）

spawn `zcode.cjs app-server --stdio`（带桌面同款的两份 provider 配置文件
环境变量）→ **盯子进程日志等 `provider_registry.ready` 真信号**（不是固定秒数——
这是曾导致"消息 accepted 但不处理"的竞态根源）→ `provider/updateAccountConfig`
推送账号权益状态（桌面每次拉起子进程后都做这一步）→ `session/resume` 激活会话
（此时物化用到的已是修正过的注册表）→ 等会话物化（~15 秒）→ `session/send`
逐次携带模型选择 → 通过反向调用 `interaction/requestProviderRuntimeHeaders`
为每轮 API 请求提供鉴权。

轮次状态从 `turn-started / turn-completed / turn-failed` 事件跟踪（stderr 打印
`[turn] ...`）；**失败自愈**：轮次失败时自动重推权益 + 重新物化会话 + 发一条
出队触发（失败轮次的输入仍留在共享队列里，会被下一轮成功一起消化）。助手回复
从共享库读取 text 片段（用户输入的片段 `time.start == time.end`，助手的有时长，
以此区分——修掉了早期"打印出自己发的消息"的提取错误）。

协议要点（ZCode Protocol，与 MCP/JSON-RPC 不同）：

- 帧是**纯 `{id, method, params}` NDJSON，不能带 `jsonrpc` 字段**（带了会被
  zod 静默拒收，表象是"发什么都没响应"）；
- `basedOnZCodeBuiltinRevision` 必须是完整格式 `zcode-builtin:<rev>:<hash>`
  （工具自动从 `~/.zcode/cli/log/*.jsonl` 最新一条 `provider_registry.ready`
  提取；格式错会 fail-closed 清空账号 provider）；
- 时序坑：注册表就绪前推送会被默认快照覆盖，且 resume 时会话用坏注册表物化
  之后就修不好——所以必须"就绪 → 推送 → resume"这个顺序；
- 反向调用必须应答：`interaction/requestProviderRuntimeHeaders` →
  `{headersApplied:true, requestAuth:{apiKey}}`（bigmodel key 自动取自
  `~/.zcode/v2/config.json`）；`session/requestRuntimePreferences` → 最小偏好。

ZCode 升级若改协议，按 stderr 的 zod 报错对照上述要点逐项排查。

### 已知限制

- 依赖 ZCode ≥3.12.x 的 CLI 与 Node ≥22（`node:sqlite` 实验特性）；
- 目标会话必须空闲；会话忙时发送会被排队（上一轮 accepted 未跑完的消息会在
  下一次成功轮次里一起被消化）。失败后不要盲目重发，先查
  `~/.zcode/cli/db/db.sqlite` 的 `model_usage` 表最后一行的
  `status` / `error_message`。

---

## 旧版：`../zcode-bridge/zcode_send.py`（Python，适用 ZCode ≤ 0.16.5）

```bash
python ../zcode-bridge/zcode_send.py --list                       # 列会话（含忙闲）
python ../zcode-bridge/zcode_send.py --cwd "D:\project" --msg "..."             # 续接该目录最新会话
python ../zcode-bridge/zcode_send.py --session-id sess_xxx --msg "..."          # 精确会话
python ../zcode-bridge/zcode_send.py --session-id sess_xxx --check              # 只查忙闲
python ../zcode-bridge/zcode_send.py --cwd "..." --msg "..." --new              # 强制新会话
```

模型凭据自动读 `~/.zcode/v2/config.json`；`--resume` 在原会话上追加（无 fork、
上下文完整）。忙闲输出与 WorkBuddy/Hermes 桥一致（`IDLE|...` / `BUSY|...`，退出码 0/4）。

**在新版 ZCode（≥3.12）上的已知退化**：环境变量模型配置基本失效（新会话静默落到
本地 aeon），桌面创建的会话 `--resume` 直接 `Model creation failed` —— 这些场景
请改用 Node 版。

---

## 附：本目录其他工具

- `hermes_send.py` —— Hermes Desktop 桥接（WS JSON-RPC `prompt.submit`，消息实时
  显示在桌面端界面）。用法：`python hermes_send.py --list / --check / --msg`，
  详细文档见 git 历史（2026-09-16 版 README）。
