#!/usr/bin/env node
// hermes 侧发送工具：向本地假中继投递任意帧
//   node send.mjs status                 查看状态
//   node send.mjs pair                   标记 paired
//   node send.mjs frame '{"type":...}'   发送任意 JSON 帧（迭代协议用）
//   node send.mjs send "你好"            便捷发送（模板包装，首轮实测后修正）

const BASE = process.env.HIJACK_BASE || 'http://127.0.0.1:8899';
const [cmd, ...rest] = process.argv.slice(2);

const routes = {
  status: ['GET', '/status'],
  pair: ['POST', '/pair'],
};
if (cmd === 'frame' || cmd === 'send') {
  const body = cmd === 'frame' ? rest.join(' ') : JSON.stringify({ text: rest.join(' ') });
  const r = await fetch(BASE + '/' + cmd, { method: 'POST', body, headers: { 'content-type': 'application/json' } });
  console.log(r.status, await r.text());
} else if (routes[cmd]) {
  const [m, p] = routes[cmd];
  const r = await fetch(BASE + p, { method: m });
  console.log(r.status, await r.text());
} else {
  console.log('用法: node send.mjs status|pair|frame <json>|send <text>');
}
