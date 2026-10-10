'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { collect, collectSource, validateTargets } = require('../observer.cjs');

async function fixture(t, route) {
  const server = http.createServer(route);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sky-contract-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const cookieFile = path.join(dir, 'cookie');
  fs.writeFileSync(cookieFile, 'session=test');
  return { name: 'sky', kind: 'sky', authMode: 'hosted', cookieFile,
    httpUrl: 'http://127.0.0.1:' + server.address().port };
}
test('hosted authentication uses only session cookies and traverses encoded cursors', async t => {
  const paths = [];
  const target = await fixture(t, (req, res) => {
    paths.push(req.url);
    assert.equal(req.headers.cookie, 'session=test');
    assert.equal(req.headers['x-sky-token'], undefined);
    const body = req.url === '/health' ? {status:'ok'} :
      req.url === '/api/config' ? {ai_available:false, targets:[]} :
      req.url === '/api/jobs' ? {items:[{status:'succeeded'}],next_cursor:'opaque+/='} :
      {items:[{status:'failed'}],next_cursor:null};
    res.end(JSON.stringify(body));
  });
  assert.equal(validateTargets([target]).length, 1);
  const result = await collect(target);
  assert.equal(result.sky_observed_collection_up, 1);
  assert.deepEqual(result.sky_observed_jobs, {succeeded:1,failed:1,other:0});
  assert.deepEqual(paths, ['/health','/api/config','/api/jobs','/api/jobs?cursor=opaque%2B%2F%3D']);
});
test('failed later pages preserve existing discovery and omit partial counts', async t => {
  let fail = false;
  const target = await fixture(t, (req, res) => {
    if (req.url === '/health') return res.end('{"status":"ok"}');
    if (req.url === '/api/config') return res.end('{"ai_available":false,"targets":[]}');
    if (req.url === '/api/jobs') return res.end('{"items":[],"next_cursor":"next"}');
    if (fail) { res.writeHead(403); return res.end('{}'); }
    res.end('{"items":[],"next_cursor":null}');
  });
  target.discovery = {allowedHosts:['example.com']};
  const discovered = new Map();
  assert.equal((await collectSource(target, discovered, 19)).sky_discovery_up, 1);
  const previous = [{name:'existing-game'}];
  discovered.set('sky', previous);
  fail = true;
  const result = await collectSource(target, discovered, 19);
  assert.equal(result.sky_discovery_up, 0);
  assert.equal(result.sky_observed_collection_error, 'auth');
  assert.equal(result.sky_observed_jobs, undefined);
  assert.equal(discovered.get('sky'), previous);
});
test('cursor loops, missing cursors, excessive pages and login HTML fail closed', async t => {
  let mode = 'loop', requests = 0;
  const target = await fixture(t, (req, res) => {
    if (req.url === '/health') return res.end('{"status":"ok"}');
    if (req.url === '/api/config') return res.end(mode === 'html' ? '<html>Login</html>' :
      '{"ai_available":false,"targets":[]}');
    requests++;
    res.end(JSON.stringify({items:[], ...(mode === 'missing' ? {} :
      {next_cursor:mode === 'loop' ? 'repeat' : String(requests)})}));
  });
  for (mode of ['loop','missing','pages','html']) {
    requests = 0;
    const result = await collect(target);
    assert.equal(result.sky_observed_collection_up, 0);
    assert.equal(result.sky_observed_collection_error, 'schema');
    assert.ok(requests <= 100);
  }
});
test('reject invalid or incomplete hosted target configuration', () => {
  for (const authMode of ['typo','hosted']) {
    assert.throws(() => validateTargets([{name:'sky',kind:'sky',httpUrl:'https://example.com',authMode}]));
  }
});

test('duplicate job IDs and oversized job counts reject incomplete observations', async t => {
  let oversized = false;
  const target = await fixture(t, (req, res) => {
    if (req.url === '/health') return res.end('{"status":"ok"}');
    if (req.url === '/api/config') return res.end('{"ai_available":false,"targets":[]}');
    res.end(JSON.stringify(oversized ? Array.from({length:10001}, () => ({status:'running'})) :
      [{id:'same',status:'succeeded'},{id:'same',status:'succeeded'}]));
  });
  for (oversized of [false,true]) {
    const result = await collect(target);
    assert.equal(result.sky_observed_collection_error, 'schema');
    assert.equal(result.sky_observed_jobs, undefined);
  }
});

test('hosted discovery registers games on later pages and retains them on detail failure', async t => {
  let failed = false;
  const detail = {id:'a'.repeat(16),status:'succeeded',deployment_state:'active',application_id:'demo-app',
    target:'aws-ecs-express',result:{url:'https://game.example.test/'},
    application_ir:{hypotheses:[{kind:'sky-probe-protocol'}]}};
  const target = await fixture(t, (req, res) => {
    assert.equal(req.headers.cookie, 'session=test');
    assert.equal(req.headers['x-sky-token'], undefined);
    if (req.url === '/health') return res.end('{"status":"ok"}');
    if (req.url === '/api/config') return res.end('{"ai_available":false,"targets":[]}');
    if (req.url === '/api/jobs') return res.end('{"items":[],"next_cursor":"second"}');
    if (req.url.includes('?')) return res.end(JSON.stringify({items:[detail],next_cursor:null}));
    if (failed) {res.writeHead(503); return res.end('{}');}
    res.end(JSON.stringify(detail));
  });
  target.discovery = {allowedHosts:['*.example.test']};
  const discovered = new Map();
  assert.equal((await collectSource(target, discovered, 19)).sky_discovery_up, 1);
  const previous = discovered.get('sky');
  assert.equal(previous[0].httpUrl, 'https://game.example.test');
  assert.equal(previous[0].cookieFile, undefined);
  failed = true;
  assert.equal((await collectSource(target, discovered, 19)).sky_discovery_up, 0);
  assert.equal(discovered.get('sky'), previous);
});
