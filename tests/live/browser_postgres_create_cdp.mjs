// Exercise the real network -> PostgreSQL creation controls in headless Chrome.
import assert from 'node:assert/strict';

const [serverUrl, debuggingPort, application, stage = 'create', archive] = process.argv.slice(2);
assert.match(application, /^dbdrill-[a-f0-9]{8}$/);
assert.ok(['create', 'network-only', 'one-action-deploy', 'one-action-submit-only-local', 'one-action-submit-for-manual-resume', 'one-action-resume-existing', 'verify', 'deploy', 'verify-deploy', 'retire', 'verify-retire', 'recover', 'auto-existing-plan', 'one-action-local', 'one-action-failed-local', 'one-action-plan-rejected-local', 'one-action-manual-resume-local'].includes(stage));
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
socket.addEventListener('close', () => {
  for (const waiter of pending.values()) waiter.reject(Error('Chrome debugging socket closed'));
  pending.clear();
});
function command(method, params = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(Error(`Chrome ${method} did not answer within 120 seconds`));
    }, 120000);
    pending.set(id, {
      resolve: value => { clearTimeout(timer); resolve(value); },
      reject: error => { clearTimeout(timer); reject(error); },
    });
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const response = await command('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (response.exceptionDetails) throw Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
  return response.result.value;
}
async function until(check, timeoutMs, intervalMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw Error('Browser PostgreSQL creation step timed out');
}
try {
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', {url: serverUrl});
  await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨')"), 30000);
  if (['one-action-local', 'one-action-failed-local', 'one-action-plan-rejected-local', 'one-action-manual-resume-local'].includes(stage)) {
    assert.ok(archive);
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('public').checked=true;e('postgresPlanVpc').value='vpc-12345678';e('postgresPlanSubnets').value='subnet-11111111,subnet-22222222';return true;})()`);
    const document = await command('DOM.getDocument');
    const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
    assert.ok(input.nodeId);
    await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
    await evaluate("document.getElementById('postgresPlan').click();true");
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('postgresPlanInfo').textContent&&!e('postgresPlanInfo').textContent.includes('730시간'))throw Error(e('postgresPlanInfo').textContent);return e('postgresPlanInfo').textContent.includes('730시간')&&!e('postgresCreateDeploy').hidden;})()"), 30000);
    await evaluate("document.getElementById('postgresCreateDeploy').click();true");
    if (stage === 'one-action-local') {
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent==='배포 완료'&&e('infrastructure').textContent.includes('new RDS PostgreSQL');})()"), 30000);
      console.log('PASS: browser reviewed RDS price, uploaded ZIP, and completed one local simulated creation-to-deployment job');
    } else if (stage === 'one-action-failed-local') {
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent==='작업 중단·결과 확인 필요'&&e('postgresOperationInfo').textContent.includes('needs_attention')&&!e('postgresRecovery').hidden&&e('postgresCreationDetails').open;})()"), 30000);
      await command('Page.navigate', {url: serverUrl});
      await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨') && document.querySelector('#history button')"), 30000);
      await evaluate(`(() => {const button=[...document.querySelectorAll('#history button')].find(item=>item.textContent.includes(${JSON.stringify(application)}));if(!button)throw Error('Creation job missing from history');button.click();return true;})()`);
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('application').value==='dbdrill-1234abcd'&&e('status').textContent==='작업 중단·결과 확인 필요'&&e('postgresOperationInfo').textContent.includes('needs_attention')&&!e('postgresRecovery').hidden&&e('postgresCreationDetails').open;})()"), 30000);
      console.log('PASS: browser reopened failed job from history and showed RDS recovery controls');
    } else if (stage === 'one-action-manual-resume-local') {
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent==='작업 중단·결과 확인 필요'&&e('postgresOperationInfo').textContent.includes('succeeded')&&!e('resumePostgres').hidden;})()"), 30000);
      await command('Page.navigate', {url: serverUrl});
      await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨') && document.querySelector('#history button')"), 30000);
      await evaluate(`(() => {const button=[...document.querySelectorAll('#history button')].find(item=>item.textContent.includes(${JSON.stringify(application)}));if(!button)throw Error('Interrupted job missing from history');button.click();return true;})()`);
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return !e('resumePostgres').hidden&&e('postgresOperationInfo').textContent.includes('succeeded');})()"), 30000);
      await evaluate("document.getElementById('resumePostgres').click();true");
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const info=e('resumePostgresInfo').textContent;if(e('error').textContent||(info&&!info.includes('생성 작업을 재확인해')))throw Error(e('error').textContent||info);return e('status').textContent==='배포 완료';})()"), 30000);
      console.log('PASS: browser manually resumed app deployment after confirming the original RDS creation');
    } else {
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent==='배포 실패'&&e('postgresOperationInfo').textContent.includes('가격 계획을 다시 확인')&&e('postgresRecovery').hidden&&e('postgresReconcile').hidden;})()"), 30000);
      await command('Page.navigate', {url: serverUrl});
      await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨') && document.querySelector('#history button')"), 30000);
      await evaluate(`(() => {const button=[...document.querySelectorAll('#history button')].find(item=>item.textContent.includes(${JSON.stringify(application)}));if(!button)throw Error('Rejected job missing from history');button.click();return true;})()`);
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent==='배포 실패'&&e('postgresOperationInfo').textContent.includes('가격 계획을 다시 확인')&&e('postgresRecovery').hidden&&e('postgresReconcile').hidden;})()"), 30000);
      console.log('PASS: browser reopened rejected plan without showing AWS recovery controls');
    }
  } else if (stage === 'one-action-resume-existing') {
    assert.ok(process.env.SKY_BROWSER_PROBE_KEY);
    await until(() => evaluate(`!![...document.querySelectorAll('#history button')].find(item=>item.textContent.includes(${JSON.stringify(application)}))`), 30000);
    await evaluate(`(() => {const button=[...document.querySelectorAll('#history button')].find(item=>item.textContent.includes(${JSON.stringify(application)}));button.click();return true;})()`);
    const state = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);if(e('status').textContent==='배포 실패')throw Error(e('current').textContent);return ['배포 완료','필수 값 입력 대기','작업 중단·결과 확인 필요'].includes(e('status').textContent)?e('status').textContent:null;})()"), 60 * 60 * 1000);
    if (state === '작업 중단·결과 확인 필요') {
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return !e('resumePostgres').hidden&&!e('resumePostgres').disabled&&e('postgresOperationInfo').textContent.includes('succeeded');})()"), 30000).catch(() => {throw Error('RDS 생성 성공을 확인하지 못해 중단 작업을 재개하지 않았습니다.');});
      await evaluate("document.getElementById('resumePostgres').click();true");
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);if(e('resumePostgresInfo').textContent&&!e('resumePostgresInfo').textContent.includes('생성 작업을 재확인해'))throw Error(e('resumePostgresInfo').textContent);return ['배포 완료','필수 값 입력 대기'].includes(e('status').textContent);})()"), 30 * 60 * 1000);
    }
    if (await evaluate("document.getElementById('status').textContent==='필수 값 입력 대기'")) {
      await until(() => evaluate("!document.getElementById('resume').disabled"), 30000);
      const names = await evaluate("[...document.querySelectorAll('#envInputs input')].map(item=>item.dataset.name)");
      assert.deepEqual(names, ['PROBE_KEY']);
      await evaluate(`(() => {const input=document.querySelector('#envInputs input');input.value=${JSON.stringify(process.env.SKY_BROWSER_PROBE_KEY)};document.getElementById('resume').click();return true;})()`);
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent==='배포 완료';})()"), 30 * 60 * 1000);
    }
    console.log('PASS: browser reconnected to the existing one-action job without another upload or RDS creation');
  } else if (['one-action-deploy', 'one-action-submit-only-local', 'one-action-submit-for-manual-resume'].includes(stage)) {
    assert.ok(archive && process.env.SKY_BROWSER_PROBE_KEY);
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('public').checked=true;e('networkDiscover').click();return true;})()`);
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('networkDiscoverInfo').textContent&&!e('networkDiscoverInfo').textContent.includes('서브넷을 채웠습니다'))throw Error(e('networkDiscoverInfo').textContent);return e('postgresPlanVpc').value&&e('postgresPlanSubnets').value.split(',').length>=2;})()"), 60000);
    const document = await command('DOM.getDocument');
    const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
    assert.ok(input.nodeId);
    await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
    await evaluate("document.getElementById('postgresPlan').click();true");
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresPlanInfo').textContent;if(text&&!text.includes('730시간'))throw Error(text);return text.includes('730시간')&&!e('postgresCreateDeploy').hidden;})()"), 120000);
    await evaluate("document.getElementById('postgresCreateDeploy').click();true");
    const jobId = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('jobId').textContent.match(/작업 ([a-f0-9]{16})/)?.[1] || '';})()"), 120000);
    console.log('PASS: browser submitted one reviewed RDS creation and app deployment job', jobId);
    if (stage === 'one-action-submit-for-manual-resume') {
      console.log('PASS: browser submitted one-action job for post-RDS manual resume');
    } else if (stage === 'one-action-submit-only-local') {
      await until(() => evaluate("document.getElementById('status').textContent==='필수 값 입력 대기'"), 30000);
      console.log('PASS: first Chrome connection stopped at required environment input');
    } else {
    const names = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);if(e('status').textContent==='작업 중단·결과 확인 필요'||e('status').textContent==='배포 실패')throw Error(e('current').textContent);return !e('inputSection').hidden&&!e('resume').disabled?[...e('envInputs').querySelectorAll('input')].map(x=>x.dataset.name):null;})()"), 60 * 60 * 1000);
    assert.deepEqual(names, ['PROBE_KEY']);
    await evaluate(`(() => {const input=document.querySelector('#envInputs input');input.value=${JSON.stringify(process.env.SKY_BROWSER_PROBE_KEY)};document.getElementById('resume').click();return true;})()`);
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('status').textContent!=='필수 값 입력 대기';})()"), 30000);
    console.log('PASS: browser resumed the one-action deployment with the required environment value');
    }
  } else if (stage === 'auto-existing-plan') {
    assert.ok(archive);
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='auto';e('target').onchange();e('public').checked=true;e('postgresExisting').checked=true;e('postgresExisting').onchange();return true;})()`);
    const document = await command('DOM.getDocument');
    const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
    assert.ok(input.nodeId);
    await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
    assert.equal(await evaluate("document.getElementById('postgresVpc').value"), '');
    await evaluate("document.getElementById('deploy').click();true");
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return e('jobId').textContent.includes('작업 ')&&e('infrastructure').textContent.includes('지원 경로 자동 선택')&&e('infrastructure').textContent.includes('existing RDS PostgreSQL');})()"), 30000);
    console.log('PASS: browser uploaded PostgreSQL app without VPC/subnet inputs and recorded AWS policy plan');
  } else if (stage === 'recover') {
    const recoveryNetwork = archive ? JSON.parse(archive) : {
      vpc_id: 'vpc-12345678', subnet_ids: ['subnet-11111111', 'subnet-22222222'],
    };
    assert.match(recoveryNetwork.vpc_id, /^vpc-[a-f0-9]+$/);
    assert.ok(Array.isArray(recoveryNetwork.subnet_ids) && recoveryNetwork.subnet_ids.length >= 2);
    for (const subnet of recoveryNetwork.subnet_ids) assert.match(subnet, /^subnet-[a-f0-9]+$/);
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('postgresOperation').click();return true;})()`);
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);return e('postgresOperationInfo').textContent.includes(' · needs_attention · ')&&!e('postgresRecovery').hidden;})()"), 30000);
    await evaluate("document.getElementById('postgresRecoveryPlan').click();true");
    const stackId = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresRecoveryInfo').textContent;if(text&&!text.includes('ROLLBACK_COMPLETE'))throw Error(text);return !e('postgresRecoveryStart').hidden?text.match(/arn:aws:cloudformation:[^ ]+/)?.[0]:null;})()"), 30000);
    assert.ok(stackId);
    await evaluate("(() => {const e=id=>document.getElementById(id);e('postgresRecoveryConfirm').value='wrong-stack';e('postgresRecoveryStart').click();return true;})()");
    assert.ok(await evaluate("document.getElementById('postgresRecoveryInfo').textContent.includes('정확히 입력하세요')"));
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('postgresRecoveryConfirm').value=${JSON.stringify(stackId)};e('postgresRecoveryStart').click();return true;})()`);
    await until(() => evaluate("document.getElementById('postgresOperationInfo').textContent.includes(' · failed_cleaned · ')"), 120000);
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('postgresPlanVpc').value=${JSON.stringify(recoveryNetwork.vpc_id)};e('postgresPlanSubnets').value=${JSON.stringify(recoveryNetwork.subnet_ids.join(','))};e('postgresPlan').click();return true;})()`);
    await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresPlanInfo').textContent;if(text&&!text.includes('생성하지 않았습니다'))throw Error(text);return text.includes('생성하지 않았습니다')&&!e('postgresCreate').hidden;})()"), 120000);
    console.log('PASS: browser recovered failed stack and reopened same-ID RDS plan');
  } else if (stage === 'retire') {
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('retirementPlan').click();return true;})()`);
    await until(() => evaluate(`(() => {const e=id=>document.getElementById(id);const text=e('retirementPlanInfo').textContent;if(text&&!text.includes('DB ID를 입력해야'))throw Error(text);return text.includes(${JSON.stringify('sky-' + application)})&&!e('retirementStart').hidden;})()`), 120000);
    console.log('PASS: browser reviewed the owned RDS retirement plan');
    await evaluate("(() => {const e=id=>document.getElementById(id);e('retirementConfirm').value='wrong-db';e('retirementStart').click();return true;})()");
    assert.ok(await evaluate("document.getElementById('retirementOperationInfo').textContent.includes('DB ID를 정확히 입력하세요')"));
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('retirementConfirm').value=${JSON.stringify('sky-' + application)};e('retirementStart').click();return true;})()`);
    await until(() => evaluate("(() => {const text=document.getElementById('retirementOperationInfo').textContent;if(text.includes('needs_attention'))throw Error(text);return text.includes(' · running · ');})()"), 120000);
    console.log('PASS: browser submitted the confirmed RDS retirement');
  } else if (stage === 'verify-retire') {
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('retirementOperation').click();return true;})()`);
    await until(() => evaluate("(() => {const text=document.getElementById('retirementOperationInfo').textContent;if(text.includes('needs_attention'))throw Error(text);return text.includes(' · succeeded · stack_deleted · ');})()"), 60000);
    await evaluate("document.getElementById('postgresOperation').click();true");
    await until(() => evaluate("document.getElementById('postgresOperationInfo').textContent.includes(' · retired · ')"), 60000);
    assert.equal(await evaluate("document.getElementById('postgresExisting').checked"), false);
    console.log('PASS: browser showed retirement completion without reselecting the deleted RDS');
  } else if (stage === 'verify' || stage === 'deploy') {
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('postgresOperation').click();return true;})()`);
    const completed = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresOperationInfo').textContent;if(text.includes(' · needs_attention · '))throw Error(text);return text.includes(' · succeeded · ')&&e('postgresExisting').checked&&e('postgresVpc').value&&e('postgresSubnets').value.split(',').length>=2;})()"), 60000);
    assert.ok(completed);
    console.log('PASS: browser loaded completed RDS operation and filled existing-DB inputs');
    if (stage === 'deploy') {
      assert.ok(archive && process.env.SKY_BROWSER_PROBE_KEY);
      await evaluate("(() => {const e=id=>document.getElementById(id);e('public').checked=true;e('postgresLookup').click();return true;})()");
      await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresLookupInfo').textContent;if(text&&!text.includes('검증 완료'))throw Error(text);return text.includes('검증 완료')&&e('postgresVpc').value&&e('postgresSubnets').value;})()"), 60000);
      const document = await command('DOM.getDocument');
      const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
      assert.ok(input.nodeId);
      await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
      await evaluate("document.getElementById('deploy').click();true");
      const jobId = await until(() => evaluate("document.getElementById('jobId').textContent.match(/작업 ([a-f0-9]{16})/)?.[1] || ''"), 120000);
      console.log('PASS: browser uploaded PostgreSQL app as job', jobId);
      const names = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);if(e('error').textContent)throw Error(e('error').textContent);return !e('inputSection').hidden&&!e('resume').disabled?[...e('envInputs').querySelectorAll('input')].map(x=>x.dataset.name):null;})()"), 180000);
      assert.deepEqual(names, ['PROBE_KEY']);
      await evaluate(`(() => {const input=document.querySelector('#envInputs input');input.value=${JSON.stringify(process.env.SKY_BROWSER_PROBE_KEY)};document.getElementById('resume').click();return true;})()`);
      console.log('PASS: browser resumed deployment with the requested environment value');
    }
  } else if (stage === 'verify-deploy') {
    const status = await until(() => evaluate(`(() => {const button=[...document.querySelectorAll('#history button')].find(b=>b.textContent.includes(${JSON.stringify(application)}));if(!button)return null;button.click();return document.getElementById('status').textContent;})()`), 60000);
    assert.equal(status, '배포 완료');
    console.log('PASS: browser history shows the completed AWS deployment');
  } else {
  await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('networkDiscover').click();return true;})()`);
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkDiscoverInfo').textContent;if(text&&!text.includes('서브넷을 채웠습니다'))throw Error(text);return text.includes('서브넷을 채웠습니다')&&e('networkVpc').value&&e('postgresPlanVpc').value===e('networkVpc').value&&e('postgresPlanSubnets').value.split(',').length>=2;})()"), 60000);
  await evaluate("document.getElementById('networkPlan').click();true");
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkPlanInfo').textContent;if(text&&!text.includes('생성하지 않았습니다'))throw Error(text);return text.includes('생성하지 않았습니다')&&!e('networkCreate').hidden;})()"), 60000);
  console.log('PASS: browser reviewed the read-only network plan');
  await evaluate("document.getElementById('networkCreate').click();true");
  await until(() => evaluate("(() => {const text=document.getElementById('networkOperationInfo').textContent;if(text.includes('needs_attention'))throw Error(text);return text.includes(' · succeeded · ');})()"), 15 * 60 * 1000);
  console.log('PASS: browser created the app-owned network');
  if (stage !== 'network-only') {
  await evaluate("document.getElementById('postgresPlan').click();true");
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresPlanInfo').textContent;if(text&&!text.includes('이 조회는 DB를 생성하지 않았습니다'))throw Error(text);return text.includes('이 조회는 DB를 생성하지 않았습니다')&&text.includes('730시간')&&!e('postgresCreate').hidden;})()"), 120000);
  console.log('PASS: browser reviewed the read-only RDS plan and price');
  await evaluate("document.getElementById('postgresCreate').click();true");
  await until(() => evaluate("document.getElementById('postgresOperationInfo').textContent.includes(' · running · ')"), 120000);
  console.log('PASS: browser submitted the RDS create operation');
  }
  }
} finally {
  socket.close();
}
