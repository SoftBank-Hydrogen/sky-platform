// Drive the real Sky network creation UI without a browser dependency.
import assert from 'node:assert/strict';

const [serverUrl, debuggingPort, application] = process.argv.slice(2);
assert.match(application, /^netprobe-[a-f0-9]{8}$/);
const tabs = await (await fetch(`http://127.0.0.1:${debuggingPort}/json`)).json();
const tab = tabs.find(item => item.type === 'page');
assert.ok(tab?.webSocketDebuggerUrl);
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
  response.error ? waiter.reject(Error(response.error.message)) : waiter.resolve(response.result);
});
function command(method, params = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (result.exceptionDetails) throw Error(result.exceptionDetails.text);
  return result.result.value;
}
async function until(check, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw Error('Browser network step timed out');
}
try {
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', {url: serverUrl});
  await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨')"), 30000);
  await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('networkDiscover').click();return true;})()`);
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkDiscoverInfo').textContent;if(text&&!text.includes('서브넷을 채웠습니다'))throw Error(text);return text.includes('서브넷을 채웠습니다')&&e('networkVpc').value;})()"), 60000);
  await evaluate("document.getElementById('networkPlan').click();true");
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkPlanInfo').textContent;if(text&&!text.includes('생성하지 않았습니다'))throw Error(text);return text.includes('생성하지 않았습니다')&&!e('networkCreate').hidden;})()"), 60000);
  console.log('PASS: browser reviewed the read-only app network plan');
  await evaluate("document.getElementById('networkCreate').click();true");
  const result = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkOperationInfo').textContent;if(text.includes('needs_attention'))throw Error(text);if(!text.includes('succeeded'))return null;return {text,vpc:e('postgresPlanVpc').value,group:text.match(/sg-[a-f0-9]{8,17}/)?.[0]};})()"), 15 * 60 * 1000);
  assert.ok(result.group);
  assert.match(result.vpc, /^vpc-[a-f0-9]{8,17}$/);
  console.log('PASS: browser created the app network and displayed its verified group');
} finally {
  socket.close();
}
