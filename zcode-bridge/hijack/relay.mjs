#!/usr/bin/env node
// ZCode 本地假中继（fake relay）
// 用途：接管 ZCode 桌面版"网页远程控制"的 WebSocket 通道，使外部程序（如 hermes）
//       可以在完全不经过云端（wss://zcode.z.ai/ws）的情况下向客户端会话注入消息。
//
// 启动： node relay.mjs            （默认监听 127.0.0.1:8899）
// 配合： start-zcode-local.cmd     （带环境变量启动 ZCode 桌面版）
//
// ── 协议（device 侧，从 app.asar 逆向）──────────────────────────────
//   桌面版(device) → 中继:  {type:"device_register", device_mid, pass_hash, meta, client_ts}
//   中继 → 桌面版:         {type:"device_register_ack", device_sid}
//   桌面版 → 中继:         {type:"auth_init", device_sid, client_ts}   (已注册过时直接走这步)
//   中继 → 桌面版:         {type:"auth_challenge", nonce}
//   桌面版 → 中继:         {type:"auth_response", device_sid, proof, client_ts}
//   中继 → 桌面版:         {type:"auth_ack", pair_status:"waiting"|"paired"}
//   桌面版 → 中继(心跳):   {type:"pair_status_query", device_sid, client_ts}
//   中继 → 桌面版:         {type:"pair_status_ack", pair_status, device_sid}
//   双向数据:              {type:"data", ...}  ← 内层格式运行时观察/迭代
//
// ── 控制端（我们自己定义，因为中继是我们的）────────────────────────
//   任意 WebSocket 连到 /ws 后发 {type:"controller_register"} 即成为控制端；
//   之后它发的一切原始帧原样转发给桌面版。
//
// ── HTTP 控制 API（给 hermes / 脚本用）──────────────────────────────
//   GET  /status          连接与配对状态
//   POST /pair            标记 paired 并向桌面推 pair_status_ack
//   POST /frame  {…json…} 把任意 JSON 帧原样发给桌面版（迭代内层协议用）
//   POST /send   {text}   便捷接口：按当前猜测模板包装成 data 帧发给桌面版

import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const PORT = Number(process.env.HIJACK_PORT || 8899);
const LOG_FILE = join(dirname(fileURLToPath(import.meta.url)), 'hijack.log');
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const MAX_FRAME = 32 * 1024 * 1024;

const log = (...a) => {
  const line = `[${new Date().toISOString()}] ` + a.map(x =>
    typeof x === 'string' ? x : JSON.stringify(x)).join(' ');
  console.log(line);
  try { fs.appendFileSync(LOG_FILE, line + '\n'); } catch {}
};

// ── 极简 RFC6455 WebSocket 连接 ─────────────────────────────────────
class WSConn {
  constructor(sock) {
    this.sock = sock;
    this.buf = Buffer.alloc(0);
    this.closed = false;
    this.label = 'unknown';
    sock.on('data', c => this.feed(c));
    const end = () => { this.closed = true; onDisconnect(this); };
    sock.on('close', end); sock.on('error', end); sock.on('end', end);
  }
  feed(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    for (;;) {
      const f = this.tryParse();
      if (!f) return;
      if (f.opcode === 0x8) { // close
        this.sendRaw(Buffer.alloc(0), 0x8); this.sock.destroy(); this.closed = true; onDisconnect(this); return;
      }
      if (f.opcode === 0x9) { this.sendRaw(f.payload, 0xA); continue; } // ping→pong
      if (f.opcode === 0x1 || f.opcode === 0x2) onMessage(this, f);
    }
  }
  tryParse() {
    const b = this.buf;
    if (b.length < 2) return null;
    const opcode = b[0] & 0x0f, masked = (b[1] & 0x80) !== 0;
    let len = b[1] & 0x7f, off = 2;
    if (len === 126) { if (b.length < 4) return null; len = b.readUInt16BE(2); off = 4; }
    else if (len === 127) { if (b.length < 10) return null; len = Number(b.readBigUInt64BE(2)); off = 10; }
    if (len > MAX_FRAME) { this.sock.destroy(); return null; }
    let mask = null;
    if (masked) { if (b.length < off + 4) return null; mask = b.subarray(off, off + 4); off += 4; }
    if (b.length < off + len) return null;
    let payload = b.subarray(off, off + len);
    if (mask) { const out = Buffer.alloc(len); for (let i = 0; i < len; i++) out[i] = payload[i] ^ mask[i & 3]; payload = out; }
    this.buf = b.subarray(off + len);
    return { opcode, payload };
  }
  send(obj) { this.sendText(typeof obj === 'string' ? obj : JSON.stringify(obj)); }
  sendText(s) { this.sendRaw(Buffer.from(s), 0x1); }
  sendRaw(payload, opcode = 0x1) {
    if (this.closed) return;
    const len = payload.length; let header;
    if (len < 126) header = Buffer.from([0x80 | opcode, len]);
    else if (len < 65536) { header = Buffer.alloc(4); header[0] = 0x80 | opcode; header[1] = 126; header.writeUInt16BE(len, 2); }
    else { header = Buffer.alloc(10); header[0] = 0x80 | opcode; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2); }
    this.sock.write(Buffer.concat([header, payload]));
  }
}

// ── 中继状态 ────────────────────────────────────────────────────────
const state = {
  device: null,        // WSConn：ZCode 桌面版
  controllers: new Set(), // WSConn：控制端（hermes / 手机）
  paired: false,
  deviceSid: null,
  deviceInfo: null,
};

function broadcastToControllers(text) { for (const c of state.controllers) if (!c.closed) c.sendText(text); }
function notifyPairStatus() {
  if (state.device && !state.device.closed)
    // 注意：让桌面版真正进入 paired 的值是 "matched"（applyPairStatus 只认 waiting/matched）
    state.device.send({ type: 'pair_status_ack', pair_status: state.paired ? 'matched' : 'waiting', device_sid: state.deviceSid });
}

// ── device 侧消息处理 ───────────────────────────────────────────────
function onDeviceMessage(conn, text, raw) {
  let msg = null;
  try { msg = JSON.parse(text); } catch {}
  if (!msg || typeof msg.type !== 'string') {
    log('device< 非JSON帧', raw.subarray(0, 120).toString('hex'));
    return;
  }
  switch (msg.type) {
    case 'device_register':
      state.deviceSid = msg.device_sid || ('local-' + crypto.randomBytes(8).toString('hex'));
      state.deviceInfo = { device_mid: msg.device_mid, meta: msg.meta };
      log('device> device_register sid=', state.deviceSid);
      conn.send({ type: 'device_register_ack', device_sid: state.deviceSid });
      break;
    case 'auth_init':
      log('device> auth_init sid=', msg.device_sid);
      state.deviceSid = msg.device_sid || state.deviceSid;
      conn.send({ type: 'auth_challenge', nonce: crypto.randomBytes(16).toString('hex') });
      break;
    case 'auth_response':
      // 我们是服务器，直接放行（proof 无需校验）
      log('device> auth_response 通过（本地放行）');
      conn.send({ type: 'auth_ack', pair_status: state.paired ? 'paired' : 'waiting' });
      break;
    case 'pair_status_query':
      conn.send({ type: 'pair_status_ack', pair_status: state.paired ? 'matched' : 'waiting', device_sid: state.deviceSid });
      break;
    case 'data':
      log('device> data 帧', text.length > 800 ? text.slice(0, 800) + '…' : text);
      broadcastToControllers(text); // 原样转发给控制端
      break;
    default:
      log('device>', msg.type, text.length > 400 ? text.slice(0, 400) + '…' : text);
  }
}

function onMessage(conn, frame) {
  const text = frame.payload.toString('utf8');
  if (conn === state.device) return onDeviceMessage(conn, text, frame.payload);
  // 控制端：controller_register 之外的一切原样转发给桌面版
  let msg = null; try { msg = JSON.parse(text); } catch {}
  if (msg?.type === 'controller_register') {
    state.controllers.add(conn);
    state.paired = true;
    log('controller+ 注册（客户端数=' + state.controllers.size + '），标记 paired');
    notifyPairStatus();
    conn.send({ type: 'controller_ack', paired: true });
    return;
  }
  if (state.controllers.has(conn)) {
    log('controller> →device', text.length > 800 ? text.slice(0, 800) + '…' : text);
    if (state.device && !state.device.closed) state.device.sendText(text);
  } else {
    // 未注册的连接：当作潜在 device 处理不了，提示
    conn.send({ type: 'relay_hello', hint: 'send {"type":"controller_register"} or act as device' });
  }
}

function onDisconnect(conn) {
  if (conn === state.device) { log('device× 断开'); state.device = null; state.paired = false; }
  else if (state.controllers.delete(conn)) log('controller× 断开（剩余=' + state.controllers.size + '）');
}

// ── HTTP 服务 + WS 升级 ─────────────────────────────────────────────
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const json = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

  if (req.method === 'GET' && url.pathname === '/status') {
    return json(200, {
      device_connected: !!(state.device && !state.device.closed),
      device_sid: state.deviceSid,
      paired: state.paired,
      controllers: state.controllers.size,
    });
  }
  if (req.method === 'POST' && url.pathname === '/pair') {
    state.paired = true; notifyPairStatus();
    return json(200, { ok: true, paired: true });
  }
  if (req.method === 'POST' && (url.pathname === '/frame' || url.pathname === '/send')) {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      if (!state.device || state.device.closed) return json(503, { error: 'device 未连接' });
      let obj; try { obj = JSON.parse(body); } catch { return json(400, { error: 'body 不是 JSON' }); }
      let frame = obj;
      if (url.pathname === '/send') {
        // 便捷模板：按当前猜测的内层格式包装。首轮实测后按 hijack.log 里的真实格式修正。
        frame = { type: 'data', payload: { type: 'user_message', text: obj.text ?? String(body) } };
      }
      log('http> →device', JSON.stringify(frame).slice(0, 800));
      state.device.send(frame);
      json(200, { ok: true, sent: frame });
    });
    return;
  }
  if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/controller')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end('<meta charset="utf-8"><body style="font-family:system-ui;padding:40px"><h3>ZCode 本地中继已运行</h3><p>桌面版远程控制已被重定向到这里。控制端无需网页——用 WebSocket 连 <code>ws://127.0.0.1:' + PORT + '/ws</code> 并发送 <code>{"type":"controller_register"}</code> 即可。</p></body>');
    return;
  }
  json(404, { error: 'not found' });
});

server.on('upgrade', (req, sock) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname !== '/ws') return sock.destroy();
  const key = req.headers['sec-websocket-key'];
  if (!key) return sock.destroy();
  const accept = crypto.createHash('sha1').update(key + WS_GUID).digest('base64');
  sock.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  sock.setNoDelay(true);
  const conn = new WSConn(sock);
  // 第一个连上并开始说 device 协议的当作 ZCode；后续默认当控制端
  if (!state.device || state.device.closed) { state.device = conn; conn.label = 'device'; }
  else { conn.label = 'controller-candidate'; }
  log('ws+ 新连接 →', conn.label);
});

server.listen(PORT, '127.0.0.1', () => log(`ZCode 本地假中继已启动 ws://127.0.0.1:${PORT}/ws  (日志: ${LOG_FILE})`));
