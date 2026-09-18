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
import { existsSync, readdirSync, readFileSync } from 'node:fs';
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
const [dir, sessionId, message] = process.argv.slice(2);
if (!dir || !sessionId || !message) {
  console.error('用法: node zcode_aps.mjs <工作区目录> <sessionId> <消息>');
  process.exit(2);
}
const MODEL = process.env.HERMES_APS_MODEL || 'GLM-5.3';
const LEVEL = process.env.HERMES_APS_LEVEL || 'max';
const PROVIDER = process.env.HERMES_APS_PROVIDER || 'account:bigmodel-individual-coding-plan';
const WAIT_MS = Number(process.env.HERMES_APS_WAIT || 60000);
const extraModels = (process.env.HERMES_APS_MODELS || 'GLM-5.3-Flash').split(',').map(x => x.trim()).filter(Boolean);
const MODEL_IDS = [MODEL, ...extraModels].filter((v, i, a) => a.indexOf(v) === i);

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
    // 通知：收集助手文本
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

// 1) 等注册表启动完成（约 25-40 秒）再推权益，否则会被持久化的 entitled:false 快照覆盖
await wait(Number(process.env.HERMES_APS_BOOT_WAIT || 35000));
const push = await accountPush();
console.error('builtinRevision:', BUILTIN_REVISION);
console.error('accountConfig:', JSON.stringify(push.result ?? push.error));
if (push.error) { proc.kill(); process.exit(1); }

// 2) 激活会话（resume 后给会话物化留足时间：MCP 启动 + 注册表应用，约 15-25 秒）
const resume = await call('session/resume', { sessionId });
console.error('resume:', JSON.stringify(resume.result ? 'ok' : (resume.error?.message ?? 'ok')).slice(0, 150));
if (resume.error && resume.error.code !== -32004) { /* 记录但继续 */ }
await wait(25000);

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

// 4) 等回复流结束（等固定时长或文本停止增长）
let lastLen = -1, stable = 0;
const t0 = Date.now();
while (Date.now() - t0 < WAIT_MS) {
  await wait(2000);
  if (texts.join('').length === lastLen) { stable += 2; if (stable >= 8) break; } else stable = 0;
  lastLen = texts.join('').length;
}
const all = texts.join('');
if (all) {
  console.log(all.slice(-2000));
} else {
  // 通知流未订阅时从共享库读最新文本（可靠兜底）
  try {
    const { DatabaseSync } = await import('node:sqlite');
    const db = new DatabaseSync(join(ZCODE_HOME, 'cli/db/db.sqlite'), { readOnly: true });
    const row = db.prepare(
      "select CAST(data as TEXT) as d from part where session_id=? and data like '%\"type\":\"text\"%' order by time_created desc limit 1"
    ).get(sessionId);
    db.close();
    console.log(row ? (JSON.parse(row.d).text ?? JSON.stringify(JSON.parse(row.d)).slice(0, 500)) : '(未收到文本)');
  } catch (e) { console.log('(未收到文本:', e.message, ')'); }
}
proc.kill();
process.exit(0);
