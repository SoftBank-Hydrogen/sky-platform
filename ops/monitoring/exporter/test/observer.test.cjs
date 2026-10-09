'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { WebSocketServer } = require('ws');
const { validateTargets, collect, render } = require('../observer.cjs');

test('reject duplicate names and credential-bearing origins', () => {
  assert.throws(() => validateTargets([{ name: 'a', kind: 'sky', httpUrl: 'http://user:secret@example.com' }]));
  assert.throws(() => validateTargets([{ name:'a',kind:'sky',httpUrl:'http://localhost'}, {name:'a',kind:'sky',httpUrl:'http://localhost'}]));
});
test('Sky snapshots are gauges, auth failures clear stale samples', async t => {
  let authorized = true;
  const token = 'a'.repeat(43);
  const server = http.createServer((req,res) => {
    if(req.url === '/') return res.end(`const token='${token}';`);
    if(req.url === '/health') return res.end('{"status":"ok"}');
    if (!authorized || req.headers['x-sky-token'] !== token) { res.writeHead(403); return res.end('{}'); }
    res.end(JSON.stringify([{status:'succeeded'},{status:'failed'},{status:'running'}]));
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(() => new Promise(resolve=>server.close(resolve)));
  const target={name:'sky',kind:'sky',httpUrl:`http://127.0.0.1:${server.address().port}`};
  const good = await collect(target);
  assert.deepEqual(good.sky_observed_jobs, {succeeded:1,failed:1,other:1});
  assert.equal(good.sky_observed_collection_up,1);
  authorized = false;
  const bad = await collect(target);
  assert.equal(bad.sky_observed_collection_up,0);
  assert.equal(bad.sky_observed_http_up,1);
  assert.equal(bad.sky_observed_jobs,undefined);
  assert.equal(bad.sky_observed_collection_error,'auth');
  const text = render([target],new Map([['sky',good]]));
  assert.match(text, /# TYPE sky_observed_jobs gauge/);
  assert.ok(!text.includes(token));
});
test('game probe nonce and DB read are independently checked without joining', async t => {
  let wrongNonce=false;
  let joins=0;
  const server = http.createServer((req,res) => {
    res.end(JSON.stringify(req.url === '/stats' ? {connections:{players:0,others:0},totals:{messages:4,messagesRejected:1,tapsAccepted:2,tapsLimited:0}} : {rounds:3}));
  });
  const wss = new WebSocketServer({server});
  wss.on('connection',ws=>ws.on('message',raw=>{
    const msg=JSON.parse(raw); if(msg.type==='join') joins++;
    ws.send(JSON.stringify({type:'sky.probe.ack',nonce:wrongNonce?'other':msg.nonce}));
    if(wrongNonce) ws.close();
  }));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>{wss.close();return new Promise(resolve=>server.close(resolve));});
  const port=server.address().port;
  const target={name:'game',kind:'game',httpUrl:`http://127.0.0.1:${port}`,wsUrl:`ws://127.0.0.1:${port}/ws`};
  const good=await collect(target);
  assert.equal(good.sky_observed_websocket_up,1);
  assert.equal(good.sky_game_stored_rounds,3);
  assert.equal(good.sky_game_scoreboard_up,1);
  wrongNonce=true;
  const bad=await collect(target);
  assert.equal(bad.sky_observed_websocket_up,0);
  assert.equal(bad.sky_observed_http_up,1);
  assert.equal(joins,0);
});

test('malformed responses and unreadable credentials are classified without leaking values', async t => {
  const server=http.createServer((req,res)=>res.end('null'));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  const target={name:'sky',kind:'sky',httpUrl:`http://127.0.0.1:${server.address().port}`};
  const malformed=await collect(target);
  assert.equal(malformed.sky_observed_collection_up,0);
  assert.equal(malformed.sky_observed_collection_error,'schema');
  const missing=await collect({...target,cookieFile:'/missing/private-cookie'});
  assert.equal(missing.sky_observed_collection_error,'credentials');
  const metrics=render([target],new Map([['sky',missing]]));
  assert.match(metrics,/reason="credentials"} 1/);
  assert.ok(!metrics.includes('/missing/private-cookie'));
});

test('stats failure does not skip healthy scoreboard or WebSocket observations', async t => {
  let malformed = false, connections = 0;
  const server = http.createServer((req,res) => {
    if (req.url === '/stats') {
      if (!malformed) res.writeHead(503);
      return res.end(malformed ? '{"connections":{"players":0,"others":0},"totals":{"messages":1}}' : '{}');
    }
    res.end('{"rounds":4}');
  });
  const wss = new WebSocketServer({server});
  wss.on('connection',socket => {connections++; socket.on('message',raw => {
    const message = JSON.parse(raw);
    socket.send(JSON.stringify({type:'sky.probe.ack',nonce:message.nonce}));
  });});
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(() => {wss.close(); return new Promise(resolve => server.close(resolve));});
  const port = server.address().port;
  const target = {name:'game',kind:'game',httpUrl:`http://127.0.0.1:${port}`,wsUrl:`ws://127.0.0.1:${port}/ws`};
  for (const reason of ['http','schema']) {
    const snapshot = await collect(target);
    assert.equal(snapshot.sky_observed_collection_up,0);
    assert.equal(snapshot.sky_observed_collection_error,reason);
    assert.equal(snapshot.sky_observed_http_up,0);
    assert.equal(snapshot.sky_observed_websocket_up,1);
    assert.equal(snapshot.sky_game_scoreboard_up,1);
    assert.equal(snapshot.sky_game_stored_rounds,4);
    assert.equal(snapshot.sky_game_players,undefined);
    assert.equal(snapshot.sky_game_messages,undefined);
    assert.equal(snapshot.sky_observed_last_success_timestamp_seconds,undefined);
    malformed = true;
  }
  assert.equal(connections,2);
});

test('missing credentials report collection failure without a fictitious WS result', async () => {
  const snapshot = await collect({name:'game',kind:'game',httpUrl:'http://127.0.0.1:1',
    wsUrl:'ws://127.0.0.1:1/ws',cookieFile:'/missing/private-cookie'});
  assert.equal(snapshot.sky_observed_collection_up,0);
  assert.equal(snapshot.sky_observed_collection_error,'credentials');
  assert.equal(snapshot.sky_observed_websocket_up,undefined);
});
