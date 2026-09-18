#!/usr/bin/env node
// ZCode 远程控制"控制端"客户端（走本地假中继 relay.mjs）
// 链路: controller_register → bootstrap-request → workspace-list-request
//       → workspace-bridge-open → (桥上) rpc-frame 分片双向流
// 桥上重组后的内层消息 = conversation topic 协议:
//   - 传输层 rpc-frame: checksum.value 必须是 8 位小写十六进制字符串!!
//     (旧版发数字 → 桌面 zod 校验失败 → bridge-degraded rpc-transport-fault)
//   - 内层尝试 JSON TopicWireFrame:
//     {wireVersion:3, kind:"complete", deliveryKind:"initial",
//      logicalFrameId, logicalFrameOrdinal, topic:"conversation/<sessionId>",
//      subscriptionId, frame:{commandId,clientId,sessionId,type:"sendText",
//      payload:{text, requestedDelivery:"queue"}, issuedAt}}
//
// 用法:
//   node terminal.mjs                          # 观察模式:建桥+打印桌面发来的一切
//   node terminal.mjs --send --session <sessId> --text "hi" [--workspace <关键字>]
//   环境变量 HIJACK_RELAY 默认 ws://127.0.0.1:8899/ws
import { randomUUID } from 'node:crypto';

const args = process.argv.slice(2);
const flag = n => args.includes('--' + n);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 ? args[i + 1] : d; };

const RELAY = process.env.HIJACK_RELAY || 'ws://127.0.0.1:8899/ws';
const WANT_SEND = flag('send');
const SESSION_ID = opt('session', '');
const TEXT = opt('text', '（桥接实测）请只回复四个字：桌面可见');
const WS_FILTER = opt('workspace', '');

const identity = { bridgeSessionId: 'bridge-' + randomUUID(), bridgeGeneration: 1, recoveryId: randomUUID() };
let nextRequest = 1, outSeq = 1, outMsgSeq = 1, bridgeOrdinal = 0;
let bridged = false;

// ── crc32（输出 8 位小写十六进制字符串，桌面 schema: /^[0-9a-f]{8}$/）──
const CRC_TABLE = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
const crc32 = buf => { let c = 0xFFFFFFFF; for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; };
const crcHex = buf => crc32(buf).toString(16).padStart(8, '0');

// ── rpc-frame 编码（分片 + 重组侧校验）──
function frameMessage(innerObj) {
  const bytes = Buffer.from(JSON.stringify(innerObj), 'utf8');
  const checksum = { algorithm: 'crc32', value: crcHex(bytes) };
  const MAX_PHYS = 256 * 1024;
  const budget = Math.max(64, MAX_PHYS - 512);
  const n = Math.max(1, Math.ceil(bytes.length / budget));
  const frames = [];
  for (let i = 0; i < n; i++) {
    const frag = bytes.subarray(i * budget, Math.min(bytes.length, (i + 1) * budget));
    frames.push({
      zcode_type: 'rpc-frame', ...identity,
      seq: outSeq++, messageSeq: outMsgSeq,
      fragmentIndex: i, fragmentCount: n,
      messageBytes: bytes.length, checksum,
      dataBase64: frag.toString('base64'),
    });
  }
  outMsgSeq++;
  return frames;
}

// conversation topic 命令信封（schema 见 zcode.cjs G0n/dWi）
function sendTextEnvelope(sessId, text) {
  return {
    commandId: randomUUID(), clientId: randomUUID(),
    sessionId: sessId, type: 'sendText',
    payload: { text, requestedDelivery: 'queue' },
    issuedAt: Date.now(),
  };
}
// TopicWireFrame（complete、单帧）
function topicFrame(sessId, envelope) {
  bridgeOrdinal += 1;
  return {
    wireVersion: 3, kind: 'complete', deliveryKind: 'initial',
    logicalFrameId: randomUUID(), logicalFrameOrdinal: bridgeOrdinal,
    topic: 'conversation/' + sessId, subscriptionId: identity.bridgeSessionId,
    frame: envelope,
  };
}

const ws = new WebSocket(RELAY);
const t0 = Date.now();
const ts = () => `[+${((Date.now() - t0) / 1000).toFixed(1)}s]`;
const send = o => ws.send(JSON.stringify(o));
const sendData = payload => send({ type: 'data', payload });

// ── 分片重组（桌面 → 我们）──
const assembling = new Map();
const pickIdentity = p => ({ bridgeSessionId: p.bridgeSessionId, ...(p.bridgeGeneration !== undefined ? { bridgeGeneration: p.bridgeGeneration } : {}), ...(p.recoveryId !== undefined ? { recoveryId: p.recoveryId } : {}) });
function handleRpcFrame(p) {
  const key = p.messageSeq;
  if (!assembling.has(key)) assembling.set(key, { frames: new Map(), count: p.fragmentCount, messageBytes: p.messageBytes, checksum: p.checksum });
  const a = assembling.get(key);
  a.frames.set(p.fragmentIndex, p.dataBase64);
  sendData({ zcode_type: 'rpc-frame-ack', ...pickIdentity(p), ackMessageSeq: p.messageSeq });
  if (a.frames.size < a.count) return;
  const bytes = Buffer.concat([...a.frames.entries()].sort((x, y) => x[0] - y[0]).map(([, b64]) => Buffer.from(b64, 'base64')));
  assembling.delete(key);
  const ok = bytes.length === a.messageBytes && crcHex(bytes) === a.checksum?.value;
  const raw = bytes.toString('utf8');
  let pretty = raw;
  try { pretty = JSON.stringify(JSON.parse(raw), null, 1); } catch { pretty = '(非JSON) hex=' + bytes.toString('hex'); }
  console.log(`${ts()} ◀ 内层消息 seq=${key} ${ok ? 'CRC-OK' : 'CRC-BAD!'} ${bytes.length}B:\n${pretty.slice(0, 2500)}`);

  // 若内层是我们发的 sendText 的回执/状态，原样展示即可
  if (WANT_SEND && /commandId|"type":"(state|reply|ack|accepted)/.test(raw) && !sent) {
    // 收到任何命令回执都算通路打通
    console.log(ts, '✓ 命令已被桌面接受（收到回执）');
  }
}

let sent = false;
function attemptSendText() {
  if (sent || !SESSION_ID) return;
  sent = true;
  console.log(ts, `发送 sendText → conversation/${SESSION_ID}: "${TEXT.slice(0, 60)}"`);
  for (const f of frameMessage(topicFrame(SESSION_ID, sendTextEnvelope(SESSION_ID, TEXT)))) sendData(f);
}

ws.onopen = () => { console.log(ts, '已连中继'); send({ type: 'controller_register' }); };
ws.onmessage = e => {
  const raw = String(e.data);
  let m; try { m = JSON.parse(raw); } catch { return console.log(ts, '◀非JSON', raw.slice(0, 200)); }
  if (m.type === 'controller_ack') {
    console.log(ts, '配对成立，发 bootstrap-request');
    setTimeout(() => sendData({ zcode_type: 'bootstrap-request', requestId: 'req-' + nextRequest++ }), 300);
    return;
  }
  if (m.type === 'data' || m.payload) {
    const p = m.payload ?? m;
    if (p.zcode_type === 'rpc-frame') return handleRpcFrame(p);
    if (p.zcode_type === 'rpc-frame-ack') return console.log(ts, '◀ ack messageSeq=', p.ackMessageSeq);
    if (p.zcode_type === 'workspace-bridge-ready') {
      console.log(ts, '◀ bridge-ready（checksum 已修正为 hex，等待桌面首个内层帧…）');
      bridged = true;
      if (WANT_SEND) setTimeout(attemptSendText, Number(process.env.HIJACK_SEND_DELAY_MS || 2500));
      return;
    }
    if (p.zcode_type === 'bridge-degraded') {
      console.log(ts, '✗ bridge-degraded:', p.reason, '（帧被桌面丢弃；对照 ~/.zcode/v2/logs 里 [web-remote-control] 日志修正格式）');
      return;
    }
    let s = JSON.stringify(p);
    console.log(ts, '◀', s.length > 700 ? s.slice(0, 700) + '…' : s);
    if (p.zcode_type === 'bootstrap-response') {
      console.log(ts, '发 workspace-list-request');
      setTimeout(() => sendData({ zcode_type: 'workspace-list-request', requestId: 'req-' + nextRequest++ }), 200);
      return;
    }
    if (p.zcode_type === 'workspace-list-response') {
      const list = p.result?.workspaces ?? [];
      console.log(ts, '工作区列表:', list.map(w => w.workspacePath).join(' | '));
      const target = list.find(w => String(w.workspacePath).includes(WS_FILTER || 'workspace\\default')) ?? list.find(w => w.kind === 'local') ?? list[0];
      const key = target?.workspacePath;
      if (!key) { console.log(ts, '没有可用工作区'); return; }
      console.log(ts, `对 [${key}] 发 workspace-bridge-open`);
      setTimeout(() => sendData({ zcode_type: 'workspace-bridge-open', requestId: 'req-' + nextRequest++, ...identity, workspaceKey: key, ...(SESSION_ID ? { taskId: SESSION_ID } : {}) }), 300);
      return;
    }
    return;
  }
  console.log(ts, '◀', raw.slice(0, 300));
};
ws.onclose = e => { console.log(ts, '断开', e.code, e.reason); process.exit(0); };
ws.onerror = () => console.log(ts, 'ws错误');
setInterval(() => { try { send({ type: 'controller_ping' }); } catch {} }, 25000);
console.log(bridged ? '' : '终端客户端运行中… Ctrl+C 退出' + (WANT_SEND ? `（将向 ${SESSION_ID || '未指定!'} 发送消息）` : '（观察模式）'));
