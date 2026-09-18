#!/usr/bin/env node
// zcode_aps.mjs —— hermes → ZCode 真 GLM 通道（app-server 协议版，适用 ZCode ≥ 3.12）
// 原理：spawn zcode.cjs app-server --stdio（带桌面同款 provider 配置文件对），
//       用 provider/updateAccountConfig 推权益状态（桌面每次拉起子进程后干的事），
//       session/resume 激活会话，session/send 逐次带 modelSelection → 真 GLM-5.3。
//
// 用法: node zcode_aps.mjs <工作区目录> <sessionId> <消息>
//
// 所有路径自动发现（HOME/.zcode 为根）；特殊安装可用环境变量覆盖：
//   ZCODE_HOME     ZCode 数据根目录（默认 ~/.zcode）
//   ZCODE_CLI      zcode.cjs 的完整路径（默认自动搜索常见安装位置）
//   ZCODE_RUNTIME_DIR  runtime/provider 目录（默认 <ZCODE_HOME>/v2/runtime/provider/<平台>-<架构>）
//   HERMES_APS_API_KEY  覆盖自动读取的模型 API key（默认取 v2/config.json 的 bigmodel 条目）
//   HERMES_APS_MODEL / HERMES_APS_LEVEL / HERMES_APS_PROVIDER  模型/档位/provider
//   HERMES_APS_WAIT / HERMES_APS_BOOT_WAIT  等回复/等启动毫秒数
import { spawn } from 'node:child_process';
import { closeSync, existsSync, openSync, readdirSync, readFileSync, readSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const die = msg => { console.error('ERROR: ' + msg); process.exit(1); };

// ---------- 路径发现 ----------
const HOME = homedir();
const ZCODE_HOME = process.env.ZCODE_HOME || join(HOME, '.zcode');
const V2 = join(ZCODE_HOME, 'v2');

// 1) CLI bundle（zcode.cjs）
function findCli() {
  if (process.env.ZCODE_CLI) return process.env.ZCODE_CLI;
  const candidates = [
    // Windows 常见安装位置
    join(HOME, 'AppData/Local/Programs/ZCode/resources/glm/zcode.cjs'),
    'C:/Program Files/ZCode/resources/glm/zcode.cjs',
    'C:/Program Files (x86)/ZCode/resources/glm/zcode.cjs',
    // macOS / Linux 尽力而为
    '/Applications/ZCode.app/Contents/Resources/resources/glm/zcode.cjs',
    '/opt/ZCode/resources/glm/zcode.cjs',
    '/usr/lib/zcode/resources/glm/zcode.cjs',
    join(HOME, '.local/share/ZCode/resources/glm/zcode.cjs'),
  ];
  for (const c of candidates) if (existsSync(c)) return c;
  die('找不到 zcode.cjs，尝试过:\n  ' + candidates.join('\n  ') +
      '\n请用环境变量 ZCODE_CLI 指定完整路径。');
}
const CLI = findCli();

// 2) runtime builtin provider 目录（按版本倒序取最新一份 zcode-builtin.json）
function findRuntimeDir() {
  if (process.env.ZCODE_RUNTIME_DIR) return process.env.ZCODE_RUNTIME_DIR;
  const plat = process.platform === 'win32' ? 'windows' : process.platform;
  const arch = process.arch === 'x64' ? 'x86_64' : process.arch;
  const derived = join(V2, 'runtime/provider', `${plat}-${arch}`);
  if (existsSync(derived)) return derived;
  // 推导失败则扫 platform 目录（架构命名变化时兜底）
  const root = join(V2, 'runtime/provider');
  try {
    for (const d of readdirSync(root)) {
      const p = join(root, d);
      if (existsSync(join(p, 'zcode-builtin.json'))) return p;
      try {
        for (const v of readdirSync(p)) {
          const pv = join(p, v);
          if (existsSync(join(pv, 'zcode-builtin.json'))) return p;
        }
      } catch {}
    }
  } catch {}
  die('找不到 runtime provider 目录（' + derived + ' 不存在）。' +
      '\nZCode 桌面版至少运行过一次才会生成；或用 ZCODE_RUNTIME_DIR 指定。');
}
function findBuiltinFile() {
  const root = findRuntimeDir();
  // 结构：<root>/<版本>/<endpoint>/zcode-builtin.json，版本倒序取最新
  try {
    for (const ver of readdirSync(root).sort().reverse()) {
      const vd = join(root, ver);
      try {
        for (const ep of readdirSync(vd).sort().reverse()) {
          const f = join(vd, ep, 'zcode-builtin.json');
          if (existsSync(f)) return f;
        }
      } catch {}
      const f = join(vd, 'zcode-builtin.json');
      if (existsSync(f)) return f;
    }
  } catch {}
  die('在 ' + root + ' 下没找到 zcode-builtin.json（ZCode 桌面版需至少运行过一次）。');
}
const BUILTIN_FILE = findBuiltinFile();

// 3) personal provider 配置（桌面同款）
const PERSONAL_FILE = join(V2, 'provider_config.json');
if (!existsSync(PERSONAL_FILE)) die('缺少 ' + PERSONAL_FILE + '（ZCode 桌面版需至少运行过一次）。');

// 4) 模型 API key（runtime headers 反向调用要用）
function findApiKey() {
  if (process.env.HERMES_APS_API_KEY) return process.env.HERMES_APS_API_KEY;
  try {
    const cfg = JSON.parse(readFileSync(join(V2, 'config.json'), 'utf8'));
    const providers = cfg.provider ?? {};
    const bm = providers['builtin:bigmodel-coding-plan'];
    if (bm?.options?.apiKey) return bm.options.apiKey;
    const any = Object.values(providers).find(p => p?.enabled && p?.options?.apiKey);
    if (any) return any.options.apiKey;
  } catch {}
  die('读不到模型 API key：' + join(V2, 'config.json') + ' 里没有启用的 provider。' +
      '\n在 ZCode 桌面版登录/配置一次，或用 HERMES_APS_API_KEY 指定。');
}
const API_KEY = findApiKey();

// 5) 桌面格式 builtin revision：zcode-builtin:<rev>:<hash>（hash 算法未知，
//    从 CLI 日志最新一条 provider_registry.ready 的 configRevision[0] 提取）
function findBuiltinRevision() {
  try {
    const logDir = join(ZCODE_HOME, 'cli/log');
    const files = readdirSync(logDir).filter(f => f.endsWith('.jsonl')).sort().reverse();
    for (const f of files.slice(0, 2)) {
      const lines = readFileSync(join(logDir, f), 'utf8').split('\n').reverse();
      for (const line of lines) {
        try {
          const d = JSON.parse(line);
          if (d.event === 'zcode_protocol.provider_registry.ready' && d.context?.configRevision) {
            let cr = d.context.configRevision;
            if (typeof cr === 'string') { try { cr = JSON.parse(cr); } catch {} }
            const rev0 = Array.isArray(cr) ? cr[0] : cr;
            if (typeof rev0 === 'string' && rev0.startsWith('zcode-builtin:')) return rev0;
          }
        } catch {}
      }
    }
  } catch {}
  try { return String(JSON.parse(readFileSync(BUILTIN_FILE, 'utf8')).revision ?? 'unknown'); } catch { return 'unknown'; }
}
const BUILTIN_REVISION = findBuiltinRevision();

// ---------- 参数 ----------
// ---------- 参数 ----------
const argv = process.argv.slice(2);
const flags = { check: false, waitIdle: 0, queueBusy: false };
const positional = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--check') flags.check = true;
  else if (a === '--wait-idle') flags.waitIdle = Number(argv[++i]) || 0;
  else if (a === '--queue-busy' || a === '--force-busy') flags.queueBusy = true;
  else positional.push(a);
}
const [dir, sessionId, message] = positional;
if (!dir || !sessionId || (!message && !flags.check)) {
  console.error('用法: node zcode_aps.mjs <工作区目录> <sessionId> <消息> [--check] [--wait-idle 秒] [--queue-busy]');
  console.error('  --check        只查忙闲（IDLE|... / BUSY|...，退出码 0/4），不发送');
  console.error('  --wait-idle N  忙时最多等 N 秒直到空闲再发');
  console.error('  --queue-busy   忙时也发（进入会话队列，当前轮结束后被消化）');
  process.exit(2);
}
const MODEL = process.env.HERMES_APS_MODEL || 'GLM-5.3';
const LEVEL = process.env.HERMES_APS_LEVEL || 'max';
const PROVIDER = process.env.HERMES_APS_PROVIDER || 'account:bigmodel-individual-coding-plan';
const WAIT_MS = Number(process.env.HERMES_APS_WAIT || 60000);
const extraModels = (process.env.HERMES_APS_MODELS || 'GLM-5.3-Flash').split(',').map(x => x.trim()).filter(Boolean);
const MODEL_IDS = [MODEL, ...extraModels].filter((v, i, a) => a.indexOf(v) === i);

// ---------- 忙闲检验（纯共享库查询，秒回，与 zcode_send.py 同一信号） ----------
// 尾部 assistant 消息的 time 缺 completed = 轮次进行中；尾部是未应答的 user 消息 = 排队中。
const wait2 = ms => new Promise(r => setTimeout(r, ms));
async function sessionBusy(sid) {
  try {
    const { DatabaseSync } = await import('node:sqlite');
    const db = new DatabaseSync(join(ZCODE_HOME, 'cli/db/db.sqlite'), { readOnly: true });
    const rows = db.prepare(
      'select CAST(data as TEXT) as d from message where session_id=? order by sequence desc limit 3'
    ).all(sid);
    db.close();
    for (const r of rows) {
      let d; try { d = JSON.parse(r.d); } catch { continue; }
      const t = d.time ?? {};
      if (d.role === 'assistant') return !('completed' in t);
      if (d.role === 'user') return true; // 已排队未应答
    }
    return false;
  } catch (e) {
    console.error('[busy] 查询失败，按空闲处理:', e.message);
    return false;
  }
}

if (flags.check) {
  const busy = await sessionBusy(sessionId);
  if (busy) { console.log(`BUSY|现在会话正忙，请稍后再发。|session=${sessionId}`); process.exit(4); }
  console.log(`IDLE|会话空闲，可以发送。|session=${sessionId}`);
  process.exit(0);
}

if (!flags.queueBusy) {
  let busy = await sessionBusy(sessionId);
  const deadline = Date.now() + flags.waitIdle * 1000;
  while (busy && Date.now() < deadline) {
    console.error('waiting: session busy...');
    await wait2(2000);
    busy = await sessionBusy(sessionId);
  }
  if (busy) {
    console.log(`BUSY|现在会话正忙，请稍后再发。|session=${sessionId}`);
    process.exit(4);
  }
}


// ---------- app-server 子进程 ----------
const proc = spawn(process.execPath, [CLI, 'app-server', '--stdio', '--cwd', resolve(dir)], {
  stdio: ['pipe', 'pipe', 'pipe'],
  env: {
    ...process.env,
    ZCODE_BUILTIN_PROVIDER_CONFIG_FILE: BUILTIN_FILE,
    ZCODE_PERSONAL_PROVIDER_CONFIG_FILE: PERSONAL_FILE,
  },
});
console.error(`[paths] CLI=${CLI}`);
console.error(`[paths] builtin=${BUILTIN_FILE}`);
console.error(`[paths] personal=${PERSONAL_FILE}`);

let buf = '';
const pending = new Map();
let nextId = 1;
const texts = [];
let turnState = 'idle'; // idle | running | done | failed
const RE_TEXT = /"text":"((?:[^"\\]|\\.)*)"/g;

proc.stdout.on('data', d => {
  buf += d;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    let m; try { m = JSON.parse(line); } catch { continue; }
    if (m.id !== undefined && m.method !== undefined) {
      // 服务器反向请求：按方法给对应应答
      let result;
      if (m.method === 'interaction/requestProviderRuntimeHeaders') {
        result = { headersApplied: true, requestAuth: { apiKey: API_KEY } };
      } else if (m.method === 'session/requestRuntimePreferences') {
        result = { nativeSearchEnhancementsEnabled: false, memoryEnabled: false, askUserQuestionAutoResolutionEnabled: true };
      } else {
        result = {}; // 其他 back-call 给空对象（超时也不致命）
      }
      proc.stdin.write(JSON.stringify({ id: m.id, result }) + '\n');
      continue;
    }
    if (m.id !== undefined && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); continue; }
    // 通知：跟踪轮次事件 + 收集助手文本
    if (m.method === 'computer-use/operation-event') {
      const kind = m.params?.kind;
      if (kind === 'turn-started') { turnState = 'running'; console.error('[turn] started'); }
      if (kind === 'turn-completed') { turnState = 'done'; console.error('[turn] completed'); }
      if (kind === 'turn-failed') { turnState = 'failed'; console.error('[turn] FAILED'); }
    }
    if (m.method === 'state.updated' && m.params?.reason === 'prompt_failed') { turnState = 'failed'; console.error('[turn] prompt_failed'); }
    const s = JSON.stringify(m.params ?? {});
    let mm; RE_TEXT.lastIndex = 0;
    while ((mm = RE_TEXT.exec(s))) texts.push(mm[1]);
  }
});
proc.stderr.on('data', d => process.stderr.write('[aps] ' + d.toString().split('\n').slice(-1).join('')));

const call = (method, params) => new Promise(res => {
  const id = nextId++;
  pending.set(id, res);
  proc.stdin.write(JSON.stringify({ id, method, params }) + '\n');
});
const wait = ms => new Promise(r => setTimeout(r, ms));

const accountPush = () => call('provider/updateAccountConfig', {
  revision: 'hermes-' + Date.now(),
  basedOnZCodeBuiltinRevision: BUILTIN_REVISION,
  providers: {
    [PROVIDER]: {
      builtinModelIds: MODEL_IDS,
      access: { type: 'zhipu-account', entitled: true },
    },
  },
  states: { [PROVIDER]: { availability: 'available', entitled: true, current: true } },
});

// 1) 等注册表真正就绪：盯子进程写入的共享日志，等 spawn 之后出现
//    provider_registry.ready 事件（固定等待是竞态根源：早推会被默认快照覆盖，
//    且 resume 时会话用坏注册表物化后就修不好了）
async function waitRegistryReady(timeoutMs = 90000) {
  const logDir = join(ZCODE_HOME, 'cli/log');
  const t0 = Date.now();
  try {
    const files = readdirSync(logDir).filter(f => f.endsWith('.jsonl')).sort().reverse();
    for (const f of files.slice(0, 2)) {
      const p = join(logDir, f);
      let offset = 0;
      try { offset = Math.max(0, statSync(p).size - 200000); } catch {}
      while (Date.now() - t0 < timeoutMs) {
        let chunk = '';
        try {
          const fd = openSync(p, 'r');
          const buf = Buffer.alloc(statSync(p).size - offset);
          readSync(fd, buf, 0, buf.length, offset);
          closeSync(fd);
          chunk = buf.toString('utf8');
          offset += buf.length;
        } catch { /* 文件轮转等，忽略 */ }
        for (const line of chunk.split('\n')) {
          try {
            const d = JSON.parse(line);
            if (d.event === 'zcode_protocol.provider_registry.ready' &&
                Date.parse(d.timestamp ?? '') > t0 - 5000) return true;
          } catch {}
        }
        await wait(1000);
      }
    }
  } catch {}
  return false; // 超时：按旧时序继续（回退行为）
}
console.error('等待注册表就绪（盯日志 provider_registry.ready）…');
const ready = await waitRegistryReady();
console.error(ready ? '注册表已就绪' : '（超时，按固定等待回退）');
if (!ready) await wait(Number(process.env.HERMES_APS_BOOT_WAIT || 35000));

const push = await accountPush();
console.error('builtinRevision:', BUILTIN_REVISION);
console.error('accountConfig:', JSON.stringify(push.result ?? push.error));
if (push.error) { proc.kill(); process.exit(1); }
await wait(2000);

// 2) 激活会话（就绪后推送 → 立刻 resume，会话物化用到的就是修正过的注册表；
//    之后再给 MCP 启动留 ~15 秒）
const resume = await call('session/resume', { sessionId });
console.error('resume:', JSON.stringify(resume.result ? 'ok' : (resume.error?.message ?? 'ok')).slice(0, 150));
if (resume.error && resume.error.code !== -32004) { /* 记录但继续 */ }
await wait(15000);

// 2.5) 防御性重推（若启动就绪晚于首次推送会被默认快照覆盖）
const push2 = await accountPush();
console.error('re-push:', JSON.stringify(push2.result ?? push2.error).slice(0, 120));

// 3) 发消息（带模型选择）
const send = await call('session/send', {
  sessionId,
  content: message,
  modelSelection: { providerId: PROVIDER, modelId: MODEL, options: { reasoningLevel: LEVEL } },
});
if (send.error) {
  console.error('send 失败:', JSON.stringify(send.error).slice(0, 300));
  proc.kill(); process.exit(1);
}
console.error('send 已受理:', JSON.stringify(send.result ?? {}).slice(0, 200));

// 4) 等轮次结束（看事件，不看文本增长）；失败自愈：重推 + 重新物化 + 触发出队
const sendTime = Date.now();
const t0 = Date.now();
while (Date.now() - t0 < WAIT_MS && (turnState === 'idle' || turnState === 'running')) {
  await wait(2000);
  if (turnState === 'failed') {
    console.error('[recovery] 轮次失败，重推权益 + 重新物化会话后触发出队…');
    await accountPush();
    await call('session/resume', { sessionId });
    await wait(12000);
    const retry = await call('session/send', {
      sessionId,
      content: '（继续：请处理上一条消息）',
      modelSelection: { providerId: PROVIDER, modelId: MODEL, options: { reasoningLevel: LEVEL } },
    });
    console.error('[recovery] 触发出队:', JSON.stringify(retry.result ?? retry.error).slice(0, 150));
    turnState = 'idle';
  }
}

// 5) 取助手回复：优先共享库（用户输入的 text 片段 time.start==end，助手的有时长，以此区分）
let reply = null;
try {
  const { DatabaseSync } = await import('node:sqlite');
  const db = new DatabaseSync(join(ZCODE_HOME, 'cli/db/db.sqlite'), { readOnly: true });
  const rows = db.prepare(
    "select CAST(data as TEXT) as d, time_created from part where session_id=? and data like '%\"type\":\"text\"%' and time_created > ? order by time_created asc"
  ).all(sessionId, sendTime - 15000);
  db.close();
  for (const r of rows) {
    try {
      const j = JSON.parse(r.d);
      if (j.type === 'text' && j.time && j.time.start !== j.time.end && j.text) reply = j.text;
    } catch {}
  }
} catch {}
if (reply) {
  console.log(reply.slice(-2000));
} else {
  const all = texts.join('');
  console.log(all ? all.slice(-2000) : '(未收到回复)');
}
proc.kill();
process.exit(0);
