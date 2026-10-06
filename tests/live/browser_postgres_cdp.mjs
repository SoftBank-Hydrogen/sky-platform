// Drive the served Sky UI in real Chrome; no third-party browser package required.
import assert from 'node:assert/strict';

const [serverUrl, debuggingPort, archive, mode = 'apply', snapshotName = 'browser-read-only', previousJobId] = process.argv.slice(2);
const probeKey = process.env.SKY_BROWSER_PROBE_KEY;
assert.ok(serverUrl && debuggingPort && archive && ['apply', 'read-only', 'snapshot-apply'].includes(mode));
if (mode === 'apply') assert.ok(probeKey);
let socket;
let serial = 0;
let pending = new Map();
async function connect() {
  const tabs = await (await fetch(`http://127.0.0.1:${debuggingPort}/json`)).json();
  const tab = tabs.find(item => item.type === 'page');
  assert.ok(tab?.webSocketDebuggerUrl, 'Chrome has no debuggable page');
  const connection = new WebSocket(tab.webSocketDebuggerUrl);
  const waiters = new Map();
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('Chrome debugging connection timed out')), 10000);
    connection.addEventListener('open', () => {clearTimeout(timer); resolve();}, {once: true});
    connection.addEventListener('error', () => {clearTimeout(timer); reject(Error('Chrome debugging connection closed'));}, {once: true});
  });
  socket = connection;
  pending = waiters;
  connection.addEventListener('message', event => {
    const response = JSON.parse(event.data);
    const waiter = waiters.get(response.id);
    if (!waiter) return;
    waiters.delete(response.id);
    if (response.error) waiter.reject(Error(response.error.message));
    else waiter.resolve(response.result);
  });
  connection.addEventListener('close', () => {
    for (const waiter of waiters.values()) waiter.reject(Error('Chrome debugging connection closed'));
    waiters.clear();
  });
  await command('Page.enable');
  await command('Runtime.enable');
}
function command(method, params = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    const waiters = pending;
    const timer = setTimeout(() => {
      waiters.delete(id);
      reject(Error('Chrome debugging command timed out'));
    }, 20000);
    waiters.set(id, {
      resolve: value => {clearTimeout(timer); resolve(value);},
      reject: error => {clearTimeout(timer); reject(error);},
    });
    try {
      if (socket?.readyState !== WebSocket.OPEN) throw Error('Chrome debugging connection closed');
      socket.send(JSON.stringify({id, method, params}));
    } catch (error) {
      waiters.get(id)?.reject(error);
      waiters.delete(id);
    }
  });
}
async function evaluate(expression) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const result = await command('Runtime.evaluate', {
        expression, awaitPromise: true, returnByValue: true,
      });
      if (result.exceptionDetails) throw Error(result.exceptionDetails.text);
      return result.result.value;
    } catch (error) {
      if (!String(error.message).startsWith('Chrome debugging') || attempt === 2) throw error;
      await connect();
      console.log('Chrome debugging connection restored');
    }
  }
}
async function until(check, timeoutMs, intervalMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw Error('Browser step timed out');
}
try {
  await connect();
  await command('Page.navigate', {url: serverUrl});
  await until(() => evaluate("document.readyState === 'complete' && document.getElementById('deploy') && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨')"), 30000);
  await evaluate("(() => {const e = id => document.getElementById(id); e('target').value = 'aws-ecs-express'; e('target').onchange(); e('networkDiscover').click();})()");
  const discovered = await until(() => evaluate("(() => {const e = id => document.getElementById(id); const text = e('networkDiscoverInfo').textContent; if (text && !text.includes('서브넷을 채웠습니다')) throw Error(text); return text.includes('서브넷을 채웠습니다') && e('networkVpc').value && e('postgresPlanVpc').value === e('networkVpc').value && e('postgresPlanSubnets').value.split(',').length >= 2;})()"), 60000);
  assert.ok(discovered);
  console.log('PASS: browser filled default VPC and subnets from AWS');
  await evaluate("(() => {const e = id => document.getElementById(id); e('application').value = 'demo-app'; e('target').value = 'aws-ecs-express'; e('target').onchange(); e('public').checked = true; e('postgresExisting').checked = true; e('postgresExisting').onchange(); e('postgresLookup').click();})()");
  const lookup = await until(() => evaluate("(() => {const e = id => document.getElementById(id); const text = e('postgresLookupInfo').textContent; if (text && !text.includes('검증 완료')) throw Error(text); return text.includes('검증 완료') && e('postgresVpc').value && e('postgresSubnets').value && e('postgresVpc').value === e('networkVpc').value;})()"), 60000);
  assert.ok(lookup);
  console.log('PASS: browser RDS lookup filled the verified network');
  if (mode === 'read-only' || mode === 'snapshot-apply') {
    await evaluate("document.getElementById('postgresBackups').click(); true");
    const backup = await until(() => evaluate("(() => {const text = document.getElementById('postgresBackupInfo').textContent; if (text && !text.includes('조회만 수행했습니다')) throw Error(text); return text.includes('자동 백업 보존') && text.includes('수동 스냅샷');})()"), 60000);
    assert.ok(backup);
    console.log('PASS: browser displayed the RDS backup and protection status');
    await evaluate(`(() => {const e = id => document.getElementById(id); e('snapshotName').value = ${JSON.stringify(snapshotName)}; e('snapshotPlan').click(); return true;})()`);
    const planned = await until(() => evaluate(`(() => {const e = id => document.getElementById(id); const text = e('snapshotPlanInfo').textContent; if (text && !text.includes('이 조회는 스냅샷을 생성하지 않았습니다')) throw Error(text); return text.includes(${JSON.stringify('sky-demo-app-' + snapshotName)}) && text.includes('저장 비용') && !e('snapshotCreate').hidden;})()`), 60000);
    assert.ok(planned);
    console.log('PASS: browser showed a read-only snapshot plan and separate create action');
    if (mode === 'read-only') {
      await evaluate("document.getElementById('retirementPlan').click(); true");
      const retirement = await until(() => evaluate("(() => {const e = id => document.getElementById(id); const text = e('retirementPlanInfo').textContent; if (text && !text.includes('DB ID를 입력해야')) throw Error(text); return text.includes('sky-demo-app') && text.includes('삭제 보호 켜짐') && !e('retirementStart').hidden;})()"), 60000);
      assert.ok(retirement);
      await evaluate("(() => {const e = id => document.getElementById(id); e('retirementConfirm').value = 'wrong-db'; e('retirementStart').click(); return true;})()");
      const refused = await evaluate("document.getElementById('retirementOperationInfo').textContent.includes('DB ID를 정확히 입력하세요')");
      assert.ok(refused);
      console.log('PASS: browser showed read-only retirement plan and rejected wrong DB ID');
    }
    if (mode === 'snapshot-apply') {
      await evaluate("document.getElementById('snapshotCreate').click(); true");
      await until(() => evaluate("document.getElementById('snapshotOperationInfo').textContent.includes(' · running · ')"), 60000);
      console.log('PASS: browser submitted the planned snapshot creation');
      const completed = await until(async () => {
        const state = await evaluate("(() => {const e = id => document.getElementById(id); return {text: e('snapshotOperationInfo').textContent, reconcile: !e('snapshotReconcile').hidden};})()");
        if (state.text.includes(' · succeeded · ')) return true;
        if (state.text.includes(' · needs_attention · ')) throw Error(state.text);
        if (state.reconcile) await evaluate("document.getElementById('snapshotReconcile').click(); true");
        else await evaluate("document.getElementById('snapshotOperation').click(); true");
        return false;
      }, 15 * 60 * 1000, 5000);
      assert.ok(completed);
      console.log('PASS: browser reconciled an available owned snapshot');
    }
  }
  if (mode === 'apply') {
  const document = await command('DOM.getDocument');
  const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
  assert.ok(input.nodeId);
  await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
  await evaluate("document.getElementById('deploy').click(); true");
  const jobId = await until(() => evaluate("document.getElementById('jobId').textContent.match(/작업 ([a-f0-9]{16})/)?.[1] || ''"), 120000);
  if (previousJobId) assert.notEqual(jobId, previousJobId, 'Browser reused the previous deployment job');
  console.log('Browser deployment job:', jobId);
  const deadline = Date.now() + 35 * 60 * 1000;
  let resumed = false;
  let reopened = false;
  while (Date.now() < deadline) {
    const state = await evaluate(`(() => {const e = id => document.getElementById(id); return {shown: e('jobId').textContent.includes(${JSON.stringify(jobId)}), status: e('status').textContent, error: e('error').textContent, waiting: !e('inputSection').hidden, resumeReady: !e('resume').disabled, names: [...e('envInputs').querySelectorAll('input')].map(x => x.dataset.name)};})()`);
    if (!state.shown && !reopened) {
      await evaluate(`open(${JSON.stringify(jobId)}); true`);
      reopened = true;
      console.log('Browser reopened the accepted deployment job');
      await new Promise(resolve => setTimeout(resolve, 2000));
      continue;
    }
    if (state.error) throw Error(state.error);
    if (state.status === '배포 완료') {
      assert.ok(resumed, 'Environment prompt was not resumed in the browser');
      if (previousJobId) {
        await until(() => evaluate(`[...document.querySelectorAll('#history button')].some(button => button.textContent.includes(${JSON.stringify(previousJobId)}) && button.textContent.includes('이전 릴리스'))`), 30000);
        console.log('PASS: browser history marked the previous release superseded');
      }
      console.log('PASS: browser upload, environment resume, and AWS deployment completed');
      process.exitCode = 0;
      break;
    }
    if (state.status.includes('실패') || state.status.includes('중단')) throw Error(state.status);
    if (state.waiting && state.resumeReady && !resumed) {
      assert.deepEqual(state.names, ['PROBE_KEY']);
      await evaluate(`(() => {const input = document.querySelector('#envInputs input'); input.value = ${JSON.stringify(probeKey)}; document.getElementById('resume').click(); return true;})()`);
      resumed = true;
      console.log('PASS: browser supplied the requested environment value');
    }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  if (Date.now() >= deadline) throw Error('Browser deployment exceeded 35 minutes');
  }
} finally {
  socket?.close();
}
