import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';

const html = readFileSync(new URL('../src/sky_platform/assets/static/index.html', import.meta.url), 'utf8');
const cancelStart = html.indexOf('function canCancel(job)');
const cancelEnd = html.indexOf('function show(job)', cancelStart);
assert.ok(cancelStart >= 0 && cancelEnd > cancelStart);
const cancelContext = {};
runInNewContext(html.slice(cancelStart, cancelEnd), cancelContext);

test('deployment certificate separates recorded checks from unverified checks', async () => {
  const start = html.indexOf('const certificateLabels=');
  const end = html.indexOf("el('certificateSection').ontoggle", start);
  assert.ok(start >= 0 && end > start);
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent: ''});
    return elements.get(id);
  };
  const context = {
    selected: 'a'.repeat(16), certificateFor: null, el: element,
    api: async () => ({verification: [
      {name: 'deployment_http', status: 'passed'},
      {name: 'image_identity', status: 'unverified'},
    ], unverified: ['image_identity']}), JSON,
  };
  runInNewContext(html.slice(start, end), context);
  await context.loadCertificate();
  assert.match(element('certificateSummary').textContent, /배포 당시 HTTP/);
  assert.match(element('certificateSummary').textContent, /미검증: 실행 이미지 동일성/);
  assert.match(element('certificateJson').textContent, /"image_identity"/);
});

test('environment preview shows target differences before deployment', async () => {
  const start = html.indexOf('function selectedUploadBody()');
  const end = html.indexOf("el('deploy').onclick", start);
  assert.ok(start >= 0 && end > start);
  const file = {name: 'app.zip'};
  const elements = {file: {files: [file]}, folder: {files: []}, public: {checked: true},
                    compatibilityPreview: {}, compatibilityMap: {hidden: true, textContent: ''}};
  const calls = [];
  const context = {el: id => elements[id], Error, String,
    api: async (path, options) => {
      calls.push([path, options]);
      return {inspection: {requirements: ['sqlite'], evidence_files: ['server.js'], scanned_files: 2}, reports: [
        {target: 'local-docker', compatible: true, preview_eligible: true,
         access_mode: 'loopback', problems: [], configuration_reason: null},
        {target: 'aws-ecs-express', compatible: false, preview_eligible: false,
         access_mode: null, problems: ['공개 범위 미지원'], configuration_reason: null},
      ]};
    }};
  runInNewContext(html.slice(start, end), context);
  await elements.compatibilityPreview.onclick();
  assert.equal(calls[0][0], '/api/compatibility');
  assert.equal(calls[0][1].body, file);
  assert.match(elements.compatibilityMap.textContent, /Local Docker · 감지된 요구 기준 통과/);
  assert.match(elements.compatibilityMap.textContent, /탐지한 요구: sqlite · 근거 파일: server.js/);
  assert.match(elements.compatibilityMap.textContent, /AWS ECS Express · 현재 미지원/);
  assert.match(elements.compatibilityMap.textContent, /비용: 미산정/);
});

test('running deployment can be cancelled only before its first attempt', () => {
  assert.equal(cancelContext.canCancel({status: 'waiting_input'}), true);
  assert.equal(cancelContext.canCancel({status: 'running', attempts: 0}), true);
  assert.equal(cancelContext.canCancel({status: 'running', attempts: 0, cancel_requested: true}), false);
  assert.equal(cancelContext.canCancel({status: 'running', attempts: 1}), false);
  assert.equal(cancelContext.canCancel({status: 'succeeded', attempts: 0}), false);
});

test('dashboard counts active services and jobs needing attention', async () => {
  const start = html.indexOf('async function history()');
  const end = html.indexOf('async function loadRollbackTargets', start);
  assert.ok(start >= 0 && end > start);
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', children: [], replaceChildren() { this.children = []; },
      append(child) { this.children.push(child); },
    });
    return elements.get(id);
  };
  const jobs = [
    {id: 'one', status: 'succeeded', deployment_state: 'active', created_at: '2026-10-05T00:00:00Z'},
    {id: 'two', status: 'failed', deployment_state: 'active', created_at: '2026-10-05T00:00:00Z'},
    {id: 'three', status: 'interrupted', deployment_state: 'needs_attention', created_at: '2026-10-05T00:00:00Z'},
    {id: 'four', status: 'succeeded', deployment_state: 'deleted', created_at: '2026-10-05T00:00:00Z'},
  ];
  const context = {
    el: element, api: async () => jobs, busy: false, statuses: {}, open() {}, Date, String,
    document: {createElement: () => ({children: [], append(child) { this.children.push(child); }})},
  };
  runInNewContext(html.slice(start, end), context);
  await context.history();
  assert.equal(element('metricTotal').textContent, '4');
  assert.equal(element('metricActive').textContent, '1');
  assert.equal(element('metricAttention').textContent, '2');
  assert.equal(element('history').children.length, 4);
});

test('interrupted local deployment shows cleanup only for recorded attempts', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async path => ({ok: true, json: async () => path === '/api/config'
      ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []} : []}),
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  const job = {id: 'a'.repeat(16), mode: 'agent', target: 'local-docker',
    status: 'interrupted', attempts: 1, events: [], diagnosis: {
      summary: '작업이 중단됐습니다.', recommended_action: '상태를 재확인하세요.'}};
  context.show(job);
  assert.match(element('diagnosisSummary').textContent, /상태를 재확인하세요/);
  assert.equal(element('diagnosisSummary').hidden, false);
  assert.equal(element('retire').hidden, false);
  assert.match(element('retire').textContent, /로컬 시도 정리/);
  context.show({...job, attempts: 0});
  assert.equal(element('retire').hidden, true);
  context.show({...job, target: 'aws-ecs-express'});
  assert.equal(element('retire').hidden, true);
  context.show({...job, attempts: 0, steps: 0, changes: [],
    infrastructure_plan: {target: 'local-docker', planner: 'user', rationale: 'test', resources: []},
    source_digest: 'a'.repeat(64)});
  assert.equal(element('resumeUnstarted').hidden, false);
  context.show({...job, attempts: 0, steps: 1, changes: [],
    infrastructure_plan: {target: 'local-docker', planner: 'user', rationale: 'test', resources: []},
    source_digest: 'a'.repeat(64)});
  assert.equal(element('resumeUnstarted').hidden, true);
  const requests = [];
  context.fetch = async path => {
    requests.push(path);
    const body = path.endsWith('/resume-unstarted') ? {id: job.id, status: 'running'}
      : path === '/api/jobs/' + job.id ? {...job, status: 'waiting_input', attempts: 0, events: []}
      : [];
    return {ok: true, json: async () => body};
  };
  context.show({...job, attempts: 0, steps: 0, changes: [],
    infrastructure_plan: {target: 'local-docker', planner: 'user', rationale: 'test', resources: []},
    source_digest: 'a'.repeat(64)});
  await element('resumeUnstarted').onclick();
  assert.ok(requests.includes('/api/deployments/' + job.id + '/resume-unstarted'));
});

test('interrupted AWS migration shows a read-only task inspection result', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const job = {id: 'a'.repeat(16), mode: 'agent', target: 'aws-ecs-express',
    status: 'interrupted', attempts: 1, events: [],
    aws_migration_task_arn: 'owned-task', aws_migration_task_definition_arn: 'owned-definition',
    aws_migration_image: 'owned-image', aws_migration_image_digest: 'owned-digest'};
  const requests = [];
  let cleaned = false;
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async path => {
      requests.push(path);
      if (path.endsWith('/migration/cleanup')) cleaned = true;
      return {ok: true, json: async () => path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path.endsWith('/migration/inspect')
          ? {status: 'succeeded'}
          : path.endsWith('/migration/cleanup')
            ? {state: 'done'}
          : path === '/api/jobs/' + job.id
            ? {...job, aws_migration_inspection: {status: 'succeeded', checked_at: '2026-10-05T00:00:00Z',
                task_arn: 'owned-task', task_definition_arn: 'owned-definition'},
                aws_migration_cleanup_state: cleaned ? 'done' : undefined}
            : []};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  context.show(job);
  assert.equal(element('inspectMigration').hidden, false);
  await element('inspectMigration').onclick();
  assert.ok(requests.includes('/api/jobs/' + job.id + '/migration/inspect'));
  assert.match(element('migrationInfo').textContent, /SQL 태스크 성공/);
  assert.match(element('migrationInfo').textContent, /자동 재개하지 않습니다/);
  assert.equal(element('cleanupMigration').hidden, false);
  await element('cleanupMigration').onclick();
  assert.ok(requests.includes('/api/jobs/' + job.id + '/migration/cleanup'));
  assert.equal(element('cleanupMigration').hidden, true);
  assert.match(element('migrationCleanupInfo').textContent, /정리를 확인했습니다/);
  context.show({...job, status: 'running'});
  assert.equal(element('inspectMigration').hidden, true);
  context.show({...job, aws_migration_status: 'succeeded',
    aws_migration_result: {task_arn: 'owned-task', task_definition_arn: 'owned-definition',
      image: 'owned-image', image_digest: 'owned-digest'},
    aws_migration_inspection: {status: 'unknown', task_arn: 'owned-task',
      task_definition_arn: 'owned-definition'}});
  assert.equal(element('cleanupMigration').hidden, false);
  context.show({...job, status: 'succeeded', result: {migration: {cleanup_complete: false}},
    aws_migration_status: 'succeeded',
    aws_migration_result: {task_arn: 'owned-task', task_definition_arn: 'owned-definition',
      image: 'owned-image', image_digest: 'owned-digest'}});
  assert.equal(element('inspectMigration').hidden, true);
  assert.equal(element('cleanupMigration').hidden, false);
});
const start = html.indexOf('function postgresUploadHeaders(application)');
const end = html.indexOf("el('deploy').onclick=", start);
assert.ok(start >= 0 && end > start);
const source = html.slice(start, end);

function headers({target = 'aws-ecs-express', publicAccess = true, existing = true,
                  application = 'demo-app'} = {}) {
  const fields = {
    target: {value: target}, public: {checked: publicAccess},
    postgresExisting: {checked: existing},
  };
  const context = {el: id => fields[id] || (fields[id] = {}), Set, Error};
  runInNewContext(source, context);
  return context.postgresUploadHeaders(application);
}

test('existing RDS opt-in needs no manually entered network headers', () => {
  assert.deepEqual({...headers()}, {
    'X-Postgres-Existing': 'true',
  });
  assert.deepEqual({...headers({existing: false})}, {});
  assert.deepEqual({...headers({target: 'auto'})}, {...headers()});
});

test('database opt-in rejects unsupported target, private service and malformed app ID', () => {
  for (const options of [
    {target: 'cloud-run'}, {publicAccess: false},
    {application: 'demo--app'},
  ]) assert.throws(() => headers(options));
});

test('automatic target keeps existing RDS binding while hiding new-DB creation', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async path => ({ok: true, json: async () => path === '/api/config'
      ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
      : []}),
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('target').value = 'auto';
  element('target').onchange();
  assert.equal(element('postgresOptions').hidden, false);
  assert.equal(element('postgresCreationDetails').hidden, true);
  element('postgresExisting').checked = true;
  element('target').value = 'aws-ecs-express';
  element('target').onchange();
  assert.equal(element('postgresExisting').checked, true);
  assert.equal(element('postgresCreationDetails').hidden, false);
});

test('app network creation needs a reviewed plan and records the selected VPC', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/aws/default-network'
        ? {account: '123456789012', region: 'ap-northeast-2', vpc_id: 'vpc-12345678',
           subnet_ids: ['subnet-11111111', 'subnet-22222222'],
           availability_zones: ['ap-northeast-2a', 'ap-northeast-2c']}
        : path.endsWith('/network/plan')
        ? {plan_id: 'a'.repeat(32), account: '123456789012', region: 'ap-northeast-2',
           stack_name: 'sky-network-demo-app'}
        : path === '/api/jobs' ? []
        : {application_id: 'demo-app', vpc_id: 'vpc-12345678', status: 'succeeded',
           message: '완료', service_security_group: 'sg-33333333'};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  const script = html.split('<script>', 2)[1].split('</script>', 1)[0];
  runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('target').onchange();
  await element('networkDiscover').onclick();
  assert.equal(element('networkVpc').value, 'vpc-12345678');
  assert.equal(element('postgresPlanVpc').value, 'vpc-12345678');
  assert.equal(element('postgresPlanSubnets').value,
    'subnet-11111111,subnet-22222222');
  assert.equal(requests.filter(item => item.path.endsWith('/network/create')).length, 0);
  await element('networkPlan').onclick();
  assert.equal(element('networkCreate').hidden, false);
  assert.equal(requests.filter(item => item.path.endsWith('/network/create')).length, 0);
  element('networkVpc').value = 'vpc-87654321';
  await element('networkCreate').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/network/create')).length, 0);
  element('networkVpc').value = 'vpc-12345678';
  await element('networkCreate').onclick();
  const create = requests.find(item => item.path.endsWith('/network/create'));
  assert.equal(JSON.parse(create.options.body).plan_id, 'a'.repeat(32));
  assert.equal(element('postgresPlanVpc').value, 'vpc-12345678');
});

test('deploy button includes existing RDS headers in the upload request', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', dataset: {},
      replaceChildren() {}, querySelectorAll() { return []; },
      append() {}, setAttribute() {},
    });
    return elements.get(id);
  };
  const requests = [];
  const job = {id: '0123456789abcdef', application_id: 'demo-app',
    status: 'failed', target: 'aws-ecs-express', events: [], changes: []};
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/deployments' ? {id: job.id}
        : path === '/api/jobs' ? [] : job;
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  const script = html.split('<script>', 2)[1].split('</script>', 1)[0];
  runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  element('file').files = [{name: 'app.zip'}];
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('public').checked = true;
  element('postgresExisting').checked = true;
  await element('deploy').onclick();
  const upload = requests.find(request => request.path === '/api/deployments');
  assert.ok(upload, element('error').textContent);
  assert.equal(upload.options.headers['X-Postgres-Existing'], 'true');
  assert.equal(upload.options.headers['X-Postgres-Vpc-Id'], undefined);
  assert.equal(upload.options.headers['X-Postgres-Subnet-Ids'], undefined);
});

test('existing RDS lookup fills the network fields from the authenticated API', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/postgres/backups')
        ? {database_id: 'sky-demo-app', backup_retention_days: 7,
           latest_restorable_time: '2026-10-01T00:00:00Z', deletion_protection: true,
           manual_snapshot_count: 0, manual_snapshots: []}
        : path.endsWith('/snapshots/plan')
        ? {plan_id: 'a'.repeat(32), account: '123456789012', region: 'ap-northeast-2',
           database_id: 'sky-demo-app', snapshot_id: 'sky-demo-app-before-migration',
           manual_snapshot_count: 0, storage_cost_warning: '저장 비용이 발생할 수 있습니다.'}
        : path.endsWith('/snapshots/create')
        ? {application_id: 'demo-app', snapshot_id: 'sky-demo-app-before-migration',
           status: 'running', message: '생성 요청 중'}
        : path.endsWith('/snapshots/sky-demo-app-before-migration/operation')
        ? {application_id: 'demo-app', snapshot_id: 'sky-demo-app-before-migration',
           status: 'pending', message: '생성 중'}
        : path.endsWith('/snapshots/sky-demo-app-before-migration/reconcile')
        ? {application_id: 'demo-app', snapshot_id: 'sky-demo-app-before-migration',
           status: 'succeeded', message: '사용 가능'}
        : {database_id: 'sky-demo-app', account: '123456789012',
           region: 'ap-northeast-2', engine_version: '18.3',
           vpc_id: 'vpc-12345678', subnet_ids: ['subnet-11111111', 'subnet-22222222']};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('postgresExisting').checked = true;
  await element('postgresLookup').onclick();
  const lookup = requests.find(request => request.path === '/api/applications/demo-app/postgres');
  assert.ok(lookup);
  assert.equal(lookup.options.headers['X-Sky-Token'], '__TOKEN__');
  assert.equal(element('postgresVpc').value, 'vpc-12345678');
  assert.equal(element('postgresSubnets').value, 'subnet-11111111,subnet-22222222');
  await element('postgresBackups').onclick();
  assert.ok(requests.some(request => request.path === '/api/applications/demo-app/postgres/backups'));
  assert.match(element('postgresBackupInfo').textContent, /자동 백업 보존 7일/);
  assert.match(element('postgresBackupInfo').textContent, /수동 스냅샷 0개/);
  element('snapshotName').value = 'before-migration';
  await element('snapshotPlan').onclick();
  assert.ok(requests.some(request => request.path === '/api/applications/demo-app/snapshots/plan'));
  assert.equal(element('snapshotCreate').hidden, false);
  await element('snapshotCreate').onclick();
  const create = requests.find(request => request.path === '/api/applications/demo-app/snapshots/create');
  assert.equal(JSON.parse(create.options.body).plan_id, 'a'.repeat(32));
  await element('snapshotOperation').onclick();
  assert.equal(element('snapshotReconcile').hidden, false);
  await element('snapshotReconcile').onclick();
  assert.match(element('snapshotOperationInfo').textContent, /succeeded/);
});

test('new RDS plan button shows a read-only capacity quote', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : {plan_id: 'planned-token-123456789012', account: '123456789012', region: 'ap-northeast-2', engine_version: '18.3',
           instance_class: 'db.t4g.micro', storage_type: 'gp3', storage_gib: 20,
           pricing: {baseline_730h_usd: '20.87'}};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('postgresPlanVpc').value = 'vpc-12345678';
  element('postgresPlanSubnets').value = 'subnet-11111111,subnet-22222222';
  await element('postgresPlan').onclick();
  const plan = requests.find(request => request.path === '/api/applications/demo-app/postgres/plan');
  assert.ok(plan);
  assert.equal(plan.options.method, 'POST');
  assert.equal(JSON.parse(plan.options.body).subnet_ids.length, 2);
  assert.match(element('postgresPlanInfo').textContent, /20\.87 USD/);
  assert.match(element('postgresPlanInfo').textContent, /생성하지 않았습니다/);
});

test('reviewed RDS plan can start creation and fill the resulting DB connection', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/plan')
          ? {plan_id: 'planned-token-123456789012', account: '123456789012',
             region: 'ap-northeast-2', engine_version: '18.3', instance_class: 'db.t4g.micro',
             storage_type: 'gp3', storage_gib: 20, pricing: {baseline_730h_usd: '20.87'}}
          : {application_id: 'demo-app', database_id: 'sky-demo-app',
             status: 'succeeded', message: 'created', vpc_id: 'vpc-12345678',
             subnet_ids: ['subnet-11111111', 'subnet-22222222']};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('postgresPlanVpc').value = 'vpc-12345678';
  element('postgresPlanSubnets').value = 'subnet-11111111,subnet-22222222';
  await element('postgresPlan').onclick();
  assert.equal(element('postgresCreate').hidden, false);
  await element('postgresCreate').onclick();
  const create = requests.find(request => request.path === '/api/applications/demo-app/postgres/create');
  assert.ok(create);
  assert.equal(JSON.parse(create.options.body).plan_id, 'planned-token-123456789012');
  assert.equal(element('postgresExisting').checked, true);
  assert.equal(element('postgresVpc').value, 'vpc-12345678');
  assert.equal(element('postgresSubnets').value, 'subnet-11111111,subnet-22222222');
});

test('reviewed RDS plan and selected ZIP start one creation and deployment job', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const jobId = 'a'.repeat(16);
  let jobStatus = 'interrupted';
  let creationId = null;
  let operationStatus = 'needs_attention';
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/postgres/plan')
          ? {plan_id: 'planned-token-123456789012', account: '123456789012',
             region: 'ap-northeast-2', engine_version: '18.3', instance_class: 'db.t4g.micro',
             storage_type: 'gp3', storage_gib: 20, pricing: {baseline_730h_usd: '20.87'}}
        : path === '/api/deployments' ? {id: jobId, status: 'provisioning'}
        : path.endsWith('/resume-postgres')
          ? (jobStatus = 'waiting_input', {id: jobId, status: 'running'})
        : path.endsWith('/postgres/operation')
          ? {application_id: 'demo-app', status: operationStatus,
             database_id: 'sky-demo-app', vpc_id: 'vpc-12345678',
             subnet_ids: ['subnet-11111111', 'subnet-22222222'],
             message: 'CloudFormation 결과 확인 필요'}
        : {id: jobId, application_id: 'demo-app', status: jobStatus, events: [],
           postgres_creation_id: creationId, attempts: 0, steps: 0,
           target: 'aws-ecs-express', infrastructure_plan: {target: 'aws-ecs-express',
             planner: 'explicit', rationale: 'PostgreSQL app',
             resources: ['new RDS PostgreSQL'],
             database: {binding: 'create', database_id: 'sky-demo-app'}}};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('public').checked = true;
  element('postgresPlanVpc').value = 'vpc-12345678';
  element('postgresPlanSubnets').value = 'subnet-11111111,subnet-22222222';
  const file = {name: 'demo-app.zip', size: 100};
  element('file').files = [file];
  await element('postgresPlan').onclick();
  assert.equal(element('postgresCreateDeploy').hidden, false);
  await element('postgresCreateDeploy').onclick();
  const upload = requests.find(request => request.path === '/api/deployments');
  assert.ok(upload);
  assert.equal(upload.options.headers['X-Postgres-Create-Plan'], 'planned-token-123456789012');
  assert.equal(upload.options.body, file);
  assert.equal(requests.some(request => request.path.endsWith('/postgres/create')), false);
  assert.equal(requests.some(request => request.path.endsWith('/postgres/operation')), true);
  assert.equal(element('postgresRecovery').hidden, false);
  assert.equal(element('postgresReconcile').hidden, false);
  assert.equal(element('postgresCreationDetails').open, true);
  element('application').value = 'another-app';
  element('target').value = 'local-docker';
  element('postgresRecovery').hidden = true;
  element('postgresCreationDetails').open = false;
  await context.open(jobId);
  assert.equal(element('application').value, 'demo-app');
  assert.equal(element('target').value, 'aws-ecs-express');
  assert.equal(element('postgresRecovery').hidden, false);
  assert.equal(element('postgresCreationDetails').open, true);
  const operationReads = requests.filter(request => request.path.endsWith('/postgres/operation')).length;
  jobStatus = 'failed';
  await context.open(jobId);
  assert.equal(requests.filter(request => request.path.endsWith('/postgres/operation')).length,
    operationReads);
  assert.match(element('postgresOperationInfo').textContent, /가격 계획을 다시 확인/);
  assert.equal(element('postgresRecovery').hidden, true);
  assert.equal(element('postgresReconcile').hidden, true);
  jobStatus = 'interrupted';
  creationId = 'a'.repeat(16);
  operationStatus = 'succeeded';
  await context.open(jobId);
  assert.equal(element('resumePostgres').hidden, false);
  await element('resumePostgres').onclick();
  assert.equal(requests.some(request => request.path.endsWith('/resume-postgres')),
    true);
});

test('failed RDS creation requires the exact stack ARN before cleanup', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const stackId = 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/sky-db-demo-app/id';
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/failed-create/plan')
        ? {plan_id: 'a'.repeat(32), stack_id: stackId, stack_status: 'ROLLBACK_COMPLETE',
           message: '앱 DB 없음'}
        : path.endsWith('/failed-create/start')
        ? {application_id: 'demo-app', status: 'failed_cleaned', message: '정리 완료'}
        : {application_id: 'demo-app', status: 'needs_attention', message: 'ROLLBACK_COMPLETE'};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  await element('postgresOperation').onclick();
  assert.equal(element('postgresRecovery').hidden, false);
  await element('postgresRecoveryPlan').onclick();
  assert.equal(element('postgresRecoveryStart').hidden, false);
  element('postgresRecoveryConfirm').value = stackId + '-wrong';
  await element('postgresRecoveryStart').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/failed-create/start')).length, 0);
  element('postgresRecoveryConfirm').value = stackId;
  await element('postgresRecoveryStart').onclick();
  const started = requests.find(item => item.path.endsWith('/failed-create/start'));
  assert.equal(JSON.parse(started.options.body).confirm_stack_id, stackId);
  assert.equal(element('postgresRecovery').hidden, true);
});

test('RDS retirement requires reviewed plan and exact database ID', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/retirement/plan')
          ? {plan_id: 'a'.repeat(32), application_id: 'demo-app',
             account: '123456789012', region: 'ap-northeast-2',
             database_id: 'sky-demo-app'}
          : {application_id: 'demo-app', database_id: 'sky-demo-app',
             snapshot_id: 'sky-demo-app-final-abcdef123456',
             status: 'succeeded', stage: 'stack_deleted', message: 'done'};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('target').onchange();
  await element('retirementStart').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/retirement/start')).length, 0);
  await element('retirementPlan').onclick();
  assert.equal(element('retirementStart').hidden, false);
  element('retirementConfirm').value = 'wrong';
  await element('retirementStart').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/retirement/start')).length, 0);
  element('retirementConfirm').value = 'sky-demo-app';
  await element('retirementStart').onclick();
  const start = requests.find(item => item.path.endsWith('/retirement/start'));
  assert.equal(JSON.parse(start.options.body).confirm_database_id, 'sky-demo-app');
  assert.match(element('retirementOperationInfo').textContent, /final-abcdef123456/);
});
