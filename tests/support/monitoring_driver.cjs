'use strict';
const http = require('node:http');
const readline = require('node:readline');
const { WebSocketServer } = require('../../ops/monitoring/exporter/node_modules/ws');
const { collect, collectSource, render } = require('../../ops/monitoring/exporter/observer.cjs');
async function game() {
  const state = {down:false, probes:0, joins:0, cookies:0};
  const server = http.createServer((req,res) => {
    if (req.headers.cookie) state.cookies++;
    if (state.down) {res.writeHead(503); return res.end('{}');}
    res.end(JSON.stringify(req.url === '/stats' ?
      {connections:{players:0,others:0},totals:{messages:1,messagesRejected:0,tapsAccepted:0,tapsLimited:0}} :
      {rounds:2}));
  });
  const wss = new WebSocketServer({server});
  wss.on('connection',socket => socket.on('message', raw => {
    const msg = JSON.parse(raw);
    if (msg.type === 'join') state.joins++;
    if (state.down) return socket.close();
    if (msg.type === 'sky.probe') {
      state.probes++;
      socket.send(JSON.stringify({type:'sky.probe.ack',nonce:msg.nonce}));
    }
  }));
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  return {state,server,wss,url:'http://127.0.0.1:' + server.address().port};
}
(async () => {
  const games = [await game(),await game()], discovered = new Map();
  console.log(JSON.stringify({games:games.map(item => item.url)}));
  for await (const line of readline.createInterface({input:process.stdin})) {
    const command = JSON.parse(line);
    if (command.action === 'close') break;
    for (const item of games) item.state.down = Boolean(command.gameDown);
    const target = command.target;
    const snapshot = await collectSource(target,discovered,19);
    const dynamic = discovered.get(target.name) || [];
    const snapshots = new Map([[target.name,snapshot]]);
    for (const item of dynamic) snapshots.set(item.name,await collect(item));
    console.log(JSON.stringify({snapshot,targets:dynamic,metrics:render([target,...dynamic],snapshots),
      gameStates:games.map(item => item.state)}));
  }
  for (const item of games) {
    for (const socket of item.wss.clients) socket.terminate();
    item.wss.close(); item.server.close();
  }
})().catch(error => {console.error(error.name);process.exitCode=1;});
