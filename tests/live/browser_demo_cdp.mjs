// Exercise the presentation path in real Chrome without browser packages.
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';

const [serverUrl, debuggingPort, archive, screenshot] = process.argv.slice(2);
assert.ok(serverUrl && debuggingPort && archive);
const tabs = await (await fetch(`http://127.0.0.1:${debuggingPort}/json`)).json();
const tab = tabs.find(item => item.type === 'page');
assert.ok(tab?.webSocketDebuggerUrl, 'Chrome has no debuggable page');
const socket = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, {once: true});
  socket.addEventListener('error', reject, {once: true});
});
let serial = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const response = JSON.parse(event.data);
  const waiter = pending.get(response.id);
  if (!waiter) return;
  pending.delete(response.id);
  if (response.error) waiter.reject(Error(response.error.message));
  else waiter.resolve(response.result);
});
function command(method, params = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(Error(`${method} timed out`));}, 20000);
    pending.set(id, {
      resolve: value => {clearTimeout(timer); resolve(value);},
      reject: error => {clearTimeout(timer); reject(error);},
    });
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (result.exceptionDetails) throw Error(result.exceptionDetails.text);
  return result.result.value;
}
async function until(check, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw Error('Browser presentation step timed out');
}
try {
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', {url: serverUrl});
  await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup')?.textContent.startsWith('AI 연결 설정됨')"), 30000);
  const document = await command('DOM.getDocument');
  const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
  assert.ok(input.nodeId);
  await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
  await evaluate("(() => {const e=id=>document.getElementById(id);e('application').value='sky-demo-browser';e('target').value='local-docker';e('target').onchange();e('deploy').click();return true;})()");
  const started = Date.now();
  const result = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);if(e('status').textContent==='배포 실패')throw Error(e('current').textContent);return e('status').textContent==='배포 완료'?{job:e('jobId').textContent,url:e('url').href,changes:e('changes').textContent,changeSummary:e('changeSummary').textContent,changeSummaryVisible:!e('changeSummary').hidden,logs:e('logs').textContent,access:e('infrastructure').textContent,resultVisible:!e('result').hidden}:null;})()"), 180000);
  assert.match(result.job, /작업 ([a-f0-9]{16})/);
  assert.ok(result.resultVisible, 'Deployment URL is hidden');
  assert.match(result.url, /^http:\/\/127\.0\.0\.1:\d+/);
  assert.match(result.changes, /package\.json|server\.js/);
  assert.ok(result.changeSummaryVisible, 'Source change summary is hidden');
  assert.match(result.changeSummary, /package\.json|server\.js/);
  assert.match(result.access, /로컬 PC에서만 접속/);
  const urlResponse = await fetch(result.url);
  assert.equal(urlResponse.status, 200);
  assert.equal((await urlResponse.json()).message, 'Original application is running');
  const detailsOpen = await evaluate("(() => {const changes=document.getElementById('changes');return changes.closest('details').open;})()");
  if (!detailsOpen) await evaluate("document.getElementById('changes').closest('details').open=true");
  if (screenshot) {
    await evaluate("document.getElementById('jobSection').scrollIntoView()");
    const capture = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
    writeFileSync(screenshot, Buffer.from(capture.data, 'base64'));
  }
  console.log(JSON.stringify({status: 'passed', job_id: result.job.match(/작업 ([a-f0-9]{16})/)[1],
    elapsed_seconds: Math.round((Date.now() - started) / 1000), url: result.url,
    details_open_by_default: detailsOpen, source_changes_visible_after_open: result.changes.length > 0,
    access: result.access}));
} finally {
  socket.close();
}
