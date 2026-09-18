// 自测：模拟 ZCode 桌面版(device)连上本地中继完成握手，再验证控制端转发
const BASE = 'ws://127.0.0.1:8899/ws';
const sleep = ms => new Promise(r => setTimeout(r, ms));

const device = new WebSocket(BASE);
const seen = [];
device.onmessage = e => { console.log('device<', String(e.data).slice(0, 200)); seen.push(String(e.data)); };
await new Promise(r => device.onopen = r);
console.log('device 已连接');

device.send(JSON.stringify({ type: 'device_register', device_mid: 'test-mid', pass_hash: 'x', meta: { name: 'fake-zcode' }, client_ts: Date.now() }));
await sleep(300);
device.send(JSON.stringify({ type: 'auth_init', device_sid: 'sid-from-ack', client_ts: Date.now() }));
await sleep(300);
device.send(JSON.stringify({ type: 'auth_response', device_sid: 'sid-from-ack', proof: 'whatever', client_ts: Date.now() }));
await sleep(300);
device.send(JSON.stringify({ type: 'pair_status_query', device_sid: 'sid-from-ack', client_ts: Date.now() }));
await sleep(300);

// 控制端接入
const controller = new WebSocket(BASE);
controller.onmessage = e => console.log('controller<', String(e.data).slice(0, 200));
await new Promise(r => controller.onopen = r);
controller.send(JSON.stringify({ type: 'controller_register' }));
await sleep(300);

// 控制端 → device 一条探测帧
controller.send(JSON.stringify({ type: 'data', payload: { type: 'probe', text: 'hello-from-controller' } }));
await sleep(300);

// HTTP API
const st = await (await fetch('http://127.0.0.1:8899/status')).json();
console.log('status:', st);
const fr = await fetch('http://127.0.0.1:8899/frame', { method: 'POST', body: JSON.stringify({ type: 'data', payload: { type: 'probe2' } }) });
console.log('frame:', fr.status, await fr.text());
await sleep(300);

const ok = seen.some(s => s.includes('device_register_ack')) && seen.some(s => s.includes('paired')) && seen.some(s => s.includes('probe'));
console.log(ok ? 'SELFTEST PASS' : 'SELFTEST INCOMPLETE', '| 收到帧数:', seen.length);
process.exit(ok ? 0 : 1);
