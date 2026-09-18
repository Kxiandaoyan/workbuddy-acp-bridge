// 控制端接入 + 流量观察：注册为 controller，dump 桌面版发来的一切帧
const ws = new WebSocket('ws://127.0.0.1:8899/ws');
const t0 = Date.now();
ws.onopen = () => {
  console.log('[open] 已连接中继，注册控制端');
  ws.send(JSON.stringify({ type: 'controller_register' }));
};
ws.onmessage = e => {
  console.log(`[+${((Date.now() - t0) / 1000).toFixed(1)}s] ←`, String(e.data).slice(0, 1200));
};
ws.onclose = e => { console.log('[close]', e.code, e.reason); process.exit(0); };
ws.onerror = e => { console.log('[error]', e.message ?? e); };

// 保持运行直到被 Ctrl+C
setInterval(() => { try { ws.send(JSON.stringify({ type: 'controller_ping' })); } catch {} }, 25000);
console.log('观察中… Ctrl+C 退出');
