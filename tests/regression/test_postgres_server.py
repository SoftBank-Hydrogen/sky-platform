import io
import json
import tempfile
import time
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.openai_wire_fixture import ResponsesWireFixture
from planning.agent import OpenAIDeployAgent
from planning.analysis import AISettings
from deployment.aws import AwsSettings
from database.postgres import PostgresRequest
from api.server import App, handler_for, postgres_request_from_job
from tests.live.smoke_aws_postgres_api import archive as probe_archive


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
GROUP = 'sg-33333333'


def archive(include_migration=True, database=True):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as bundle:
        bundle.writestr('package.json', json.dumps({
            'scripts': {'start': 'node server.js'},
            'dependencies': {'pg': '8.23.0'} if database else {}}))
        bundle.writestr('server.js', 'const {Pool} = require("pg");' if database
                        else 'require("node:http").createServer().listen(3000);')
        if include_migration:
            bundle.writestr('migrations/0001_init.sql', 'CREATE TABLE demo (id int);')
    return content.getvalue()


class PostgresServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    service_security_group=GROUP)
        self.app = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0,
                       agent_factory=lambda _: object())

    def test_retirement_plan_is_authenticated_and_read_only(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/retirement/plan'
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app.postgres_retirement_operations, 'plan',
                          return_value={'plan_id': 'token', 'database_id': 'sky-demo-app'}) as planned:
            handler.do_POST()
        planned.assert_called_once_with('demo-app')
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        handler.headers = {'Content-Length': '0'}
        handler.json_response.reset_mock()
        with patch.object(self.app.postgres_retirement_operations, 'plan') as planned:
            handler.do_POST()
        planned.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_failed_create_cleanup_routes_require_auth_and_exact_confirmation(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/failed-create/plan'
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app.postgres_operations, 'cleanup_plan',
                          return_value={'plan_id': 'a' * 32}) as planned:
            handler.do_POST()
        planned.assert_called_once_with('demo-app')
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        handler.path = '/api/applications/demo-app/postgres/failed-create/start'
        payload = json.dumps({'plan_id': 'a' * 32,
            'confirm_stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/sky-db-demo-app/id'}).encode()
        handler.headers = {'X-Sky-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.postgres_operations, 'cleanup_start',
                          return_value={'status': 'recovering'}) as started:
            handler.do_POST()
        started.assert_called_once_with('demo-app', 'a' * 32,
            'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/sky-db-demo-app/id')
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        handler.headers = {'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.postgres_operations, 'cleanup_start') as started:
            handler.do_POST()
        started.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_retirement_start_requires_plan_and_exact_database_id(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/retirement/start'
        payload = json.dumps({'plan_id': 'a' * 32,
                              'confirm_database_id': 'sky-demo-app'}).encode()
        handler.headers = {'X-Sky-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch.object(self.app.postgres_retirement_operations, 'start',
                          return_value={'status': 'running'}) as started:
            handler.do_POST()
        started.assert_called_once_with('demo-app', 'a' * 32, 'sky-demo-app')
        self.assertEqual(handler.json_response.call_args.args[0], 202)

    def test_retirement_record_blocks_new_aws_deployment(self):
        with patch.object(self.app.postgres_retirement_operations,
                          'blocks_deployment', return_value=True):
            with self.assertRaisesRegex(ValueError, '폐기 기록'):
                self.app.ensure_application_available('demo-app', 'aws-ecs-express')

    def upload(self, content, *, postgres=True, target='aws-ecs-express', public=True,
               network_headers=True, partial_network=False, create_plan_id=None):
        handler_class = handler_for(self.app)
        handler = handler_class.__new__(handler_class)
        handler.path = '/api/deployments'
        handler.headers = {'X-Sky-Token': self.app.token,
                           'X-Deploy-Target': target,
                           'X-Public-Access': str(public).lower(), 'X-Application-Id': 'demo-app',
                           'Content-Length': str(len(content))}
        if postgres:
            handler.headers['X-Postgres-Existing'] = 'true'
            if network_headers:
                handler.headers['X-Postgres-Vpc-Id'] = 'vpc-12345678'
                if not partial_network:
                    handler.headers['X-Postgres-Subnet-Ids'] = 'subnet-11111111,subnet-22222222'
        if create_plan_id is not None:
            handler.headers['X-Postgres-Create-Plan'] = create_plan_id
        handler.rfile = io.BytesIO(content)
        handler.json_response = Mock()
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None), \
                patch('api.server.threading.Thread.start'):
            handler.do_POST()
        return handler.json_response.call_args.args

    def test_lookup_existing_database_returns_only_verified_network(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres'
        handler.headers = {'X-Sky-Token': self.app.token}
        handler.json_response = Mock()
        database = {'database_id': 'sky-demo-app', 'account': ACCOUNT,
                    'region': REGION, 'vpc_id': 'vpc-12345678',
                    'subnet_ids': ['subnet-11111111', 'subnet-22222222'],
                    'status': 'available'}
        with patch('api.server.discover_existing_postgres', return_value=database) as discover:
            handler.do_GET()
        discover.assert_called_once_with('demo-app', self.settings)
        self.assertEqual(handler.json_response.call_args.args, (200, database))

    def test_lookup_requires_session_token(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres'
        handler.headers = {}
        handler.json_response = Mock()
        with patch('api.server.discover_existing_postgres') as discover:
            handler.do_GET()
        discover.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_backup_status_requires_session_and_returns_verified_summary(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/backups'
        handler.headers = {}
        handler.json_response = Mock()
        with patch('api.server.inspect_postgres_backup_status') as inspect:
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 403)
        inspect.assert_not_called()
        handler.headers = {'X-Sky-Token': self.app.token}
        summary = {'database_id': 'sky-demo-app', 'backup_retention_days': 7,
                   'manual_snapshot_count': 0, 'manual_snapshots': []}
        with patch('api.server.inspect_postgres_backup_status',
                   return_value=summary) as inspect:
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args, (200, summary))
        inspect.assert_called_once_with('demo-app', self.settings)

    def test_manual_snapshot_plan_create_and_reconcile_routes(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.json_response = Mock()
        handler.path = '/api/applications/demo-app/snapshots/plan'
        payload = json.dumps({'snapshot_id': 'sky-demo-app-before-migration'}).encode()
        handler.headers = {'X-Sky-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.snapshot_operations, 'plan',
                          return_value={'plan_id': 'a' * 32}) as plan:
            handler.do_POST()
        plan.assert_called_once_with('demo-app', 'sky-demo-app-before-migration')
        self.assertEqual(handler.json_response.call_args.args[0], 200)

        handler.path = '/api/applications/demo-app/snapshots/create'
        payload = json.dumps({'plan_id': 'a' * 32}).encode()
        handler.headers['Content-Length'] = str(len(payload))
        handler.rfile = io.BytesIO(payload)
        with patch.object(self.app.snapshot_operations, 'start',
                          return_value={'status': 'running'}) as start:
            handler.do_POST()
        start.assert_called_once_with('demo-app', 'a' * 32)
        self.assertEqual(handler.json_response.call_args.args[0], 202)

        handler.path = '/api/applications/demo-app/snapshots/sky-demo-app-before-migration/reconcile'
        handler.headers['Content-Length'] = '0'
        with patch.object(self.app.snapshot_operations, 'reconcile',
                          return_value={'status': 'succeeded'}) as reconcile:
            handler.do_POST()
        reconcile.assert_called_once_with('demo-app', 'sky-demo-app-before-migration')
        self.assertEqual(handler.json_response.call_args.args[0], 200)

        handler.path = '/api/applications/demo-app/snapshots/sky-demo-app-before-migration/operation'
        with patch.object(self.app.snapshot_operations, 'get',
                          return_value={'status': 'succeeded'}) as get:
            handler.do_GET()
        get.assert_called_once_with('demo-app', 'sky-demo-app-before-migration')
        self.assertEqual(handler.json_response.call_args.args[0], 200)

    def test_manual_snapshot_create_requires_session(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/snapshots/create'
        handler.headers = {'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app.snapshot_operations, 'start') as start:
            handler.do_POST()
        start.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)

    def test_creation_plan_runs_read_only_preflight(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-12345678',
                              'subnet_ids': ['subnet-11111111', 'subnet-22222222']}).encode()
        handler.headers = {'X-Sky-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        plan = {'account': ACCOUNT, 'pricing': {'baseline_730h_usd': '20.87'}}
        with patch('api.server.AwsPostgresProvisioner.preflight',
                   return_value=plan) as preflight, \
                patch('api.server.AwsPostgresProvisioner.assert_stack_available'), \
                patch('api.server.AwsPostgresProvisioner.create') as create:
            handler.do_POST()
        status, result = handler.json_response.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result['pricing'], plan['pricing'])
        self.assertTrue(result['plan_id'])
        preflight.assert_called_once_with()
        create.assert_not_called()

    def test_configured_rds_quote_cap_rejects_plan_through_api(self):
        with patch.dict('os.environ', {'SKY_MAX_RDS_730H_USD': '20.00'}):
            app = App(self.root / 'bounded', AISettings('fixture-key', 'fixture-model'),
                      aws_settings=self.settings, monitor_interval=0,
                      agent_factory=lambda _: object())
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-12345678',
                              'subnet_ids': ['subnet-11111111', 'subnet-22222222']}).encode()
        handler.headers = {'X-Sky-Token': app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch('database.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value={'account': ACCOUNT,
                                 'pricing': {'baseline_730h_usd': '20.87'}}), \
                patch('database.postgres_operations.AwsPostgresProvisioner.create') as create:
            handler.do_POST()
        status, response = handler.json_response.call_args.args
        self.assertEqual(status, 400)
        self.assertIn('서버 상한', response['error'])
        self.assertFalse(app.postgres_operations.plans)
        create.assert_not_called()

    def test_plan_resolves_verified_application_network_without_global_group(self):
        self.app.aws_settings = AwsSettings(REGION, expected_account=ACCOUNT)
        self.app.postgres_operations.settings = self.app.aws_settings
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-12345678',
                              'subnet_ids': ['subnet-11111111', 'subnet-22222222']}).encode()
        handler.headers = {'X-Sky-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch('database.postgres.AwsServiceNetworkProvisioner.inspect_current',
                   return_value={'service_security_group': GROUP}) as network, \
                patch('database.postgres_operations.AwsPostgresProvisioner.preflight',
                      return_value={'account': ACCOUNT}) as preflight, \
                patch('database.postgres_operations.AwsPostgresProvisioner.assert_stack_available'):
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        self.assertEqual(preflight.call_args.args, ())
        self.assertEqual(self.app.postgres_operations.plans['demo-app']['request'].service_security_group, GROUP)
        self.assertEqual(network.call_count, 1)

    def test_upload_uses_verified_application_network_in_persisted_aws_settings(self):
        self.app.aws_settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('database.postgres.AwsServiceNetworkProvisioner.inspect_current',
                   return_value={'service_security_group': GROUP}), \
                patch('api.server.AwsPostgresProvisioner.inspect_current',
                      return_value={'database_id': 'sky-demo-app'}):
            status, payload = self.upload(archive())
        self.assertEqual(status, 202)
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['aws']['service_security_group'], GROUP)
        self.assertEqual(postgres_request_from_job(job).service_security_group, GROUP)
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        self.assertFalse(restored.recovery_warnings)
        self.assertEqual(restored.jobs[payload['id']]['aws']['service_security_group'], GROUP)

    def test_upload_rejects_missing_application_network_before_job(self):
        self.app.aws_settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('database.postgres.AwsServiceNetworkProvisioner.inspect_current',
                   side_effect=RuntimeError('앱 네트워크 스택 없음')):
            status, payload = self.upload(archive())
        self.assertEqual(status, 400)
        self.assertIn('앱 네트워크 스택 없음', payload['error'])
        self.assertFalse(self.app.jobs)

    def test_creation_requires_preview_then_journals_async_operation(self):
        plan = {'account': ACCOUNT, 'pricing': {'baseline_730h_usd': '20.87'}}
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
            ('subnet-11111111', 'subnet-22222222'), GROUP)
        with patch('database.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=plan), \
                patch('database.postgres_operations.AwsPostgresProvisioner.assert_stack_available'), \
                patch('database.postgres_operations.threading.Thread.start'):
            planned = self.app.postgres_operations.plan(request)
            payload = json.dumps({'plan_id': planned['plan_id']}).encode()
            handler = handler_for(self.app).__new__(handler_for(self.app))
            handler.path = '/api/applications/demo-app/postgres/create'
            handler.headers = {'X-Sky-Token': self.app.token,
                               'Content-Length': str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler.json_response = Mock()
            handler.do_POST()
        status, operation = handler.json_response.call_args.args
        self.assertEqual(status, 202)
        self.assertEqual(operation['status'], 'running')
        self.assertTrue((self.root / 'database-operations' / 'demo-app.json').is_file())
        handler.path = '/api/applications/demo-app/postgres/operation'
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[1]['status'], 'running')

    def test_creation_plan_rejects_invalid_network_before_aws(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/postgres/plan'
        payload = json.dumps({'vpc_id': 'vpc-invalid', 'subnet_ids': []}).encode()
        handler.headers = {'X-Sky-Token': self.app.token,
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = Mock()
        with patch('api.server.AwsPostgresProvisioner.preflight') as preflight:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        preflight.assert_not_called()

    def reviewed_create_plan(self):
        token = 'a' * 32
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
            ('subnet-11111111', 'subnet-22222222'), GROUP)
        self.app.postgres_operations.plans['demo-app'] = {
            'id': token, 'request': request, 'result': {}, 'expires': time.monotonic() + 900}
        return token

    def created_operation(self, status, database_id=None):
        request = self.app.postgres_operations.plans['demo-app']['request']
        return {'application_id': 'demo-app', 'status': status,
                'creation_id': 'a' * 16,
                'request': asdict(request), 'expected_plan': {
                    'pricing': {'baseline_730h_usd': '20.87'}},
                'database_id': database_id, 'created_at': '2026-10-03T00:00:00+00:00',
                'message': 'test operation'}

    def test_reviewed_rds_plan_upload_creates_one_provisioning_job(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}) as start, \
                patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect:
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual((status, payload['status']), (202, 'provisioning'))
        start.assert_called_once_with('demo-app', token)
        inspect.assert_not_called()
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['infrastructure_plan']['database'],
                         {'binding': 'create', 'database_id': 'sky-demo-app'})
        self.assertEqual(job['status'], 'provisioning')
        self.assertEqual(job['postgres_creation_id'], 'a' * 16)
        with self.assertRaisesRegex(ValueError, '이미 진행 중'):
            self.app.ensure_application_available('demo-app', 'aws-ecs-express')

    def test_reviewed_rds_upload_validates_source_before_creation(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start') as start:
            status, payload = self.upload(archive(include_migration=False),
                                          postgres=False, create_plan_id=token)
        self.assertEqual(status, 400)
        self.assertIn('migrations/', payload['error'])
        start.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_changed_rds_plan_fails_before_creation_without_agent_start(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          side_effect=ValueError('생성 계획이 변경됐습니다.')) as start, \
                patch.object(self.app, 'run_agent') as deploy:
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual((status, payload['status']), (202, 'failed'))
        start.assert_called_once_with('demo-app', token)
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'failed')
        self.assertIn('가격 계획을 다시 확인', self.app.jobs[payload['id']]['events'][-1]['message'])
        self.assertEqual(self.app.jobs[payload['id']]['events'][-1]['stage'], 'database_plan_rejected')

    def test_recorded_creation_start_failure_stays_interrupted(self):
        token = self.reviewed_create_plan()
        def record_then_fail(*_args):
            self.app.postgres_operations.operations['demo-app'] = self.created_operation('running')
            raise RuntimeError('worker start failed')
        with patch.object(self.app.postgres_operations, 'start', side_effect=record_then_fail), \
                patch.object(self.app, 'run_agent') as deploy:
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual((status, payload['status']), (202, 'interrupted'))
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['events'][-1]['stage'], 'database_attention')

    def test_create_plan_rejects_existing_database_selection(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start') as start:
            status, payload = self.upload(archive(), create_plan_id=token)
        self.assertEqual(status, 400)
        self.assertIn('동시에', payload['error'])
        start.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_creation_result_controls_same_job_deployment(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual(status, 202)
        job_id = payload['id']
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'sky-demo-app')
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('api.server.DeploymentAgent.run',
                      return_value={'url': 'https://example.test', 'target': 'aws-ecs-express'}) as deploy:
            self.app.run_postgres_then_agent(job_id)
        deploy.assert_called_once_with()
        self.assertEqual(self.app.jobs[job_id]['status'], 'succeeded')
        self.assertEqual(self.app.jobs[job_id]['result']['url'], 'https://example.test')
        self.assertIn('database_ready', [event['stage'] for event in self.app.jobs[job_id]['events']])

    def test_one_action_runs_source_repair_and_managed_db_deployment_tools(self):
        content = io.BytesIO()
        original = ("const {Pool} = require('pg');\n"
                    "require('node:http').createServer((_req, res) => res.end('ok'))"
                    ".listen(3000, '127.0.0.1');\n")
        with zipfile.ZipFile(content, 'w') as bundle:
            bundle.writestr('package.json', json.dumps({
                'scripts': {'start': 'node server.js'}, 'dependencies': {'pg': '8.23.0'}}))
            bundle.writestr('server.js', original)
            bundle.writestr('migrations/0001_init.sql', 'CREATE TABLE demo (id int);')
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            status, payload = self.upload(content.getvalue(), postgres=False, create_plan_id=token)
        self.assertEqual(status, 202)
        job_id = payload['id']
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'sky-demo-app')
        actions = [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', {'path': 'server.js', 'old_text': "'127.0.0.1'",
                                     'new_text': "'0.0.0.0'"}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None,
                                      'port': 3000, 'health_path': '/',
                                      'required_env': ['PGHOST', 'PGPASSWORD']}),
            ('deploy_application', {}),
        ]
        wire = ResponsesWireFixture(actions, expected_target='aws-ecs-express',
                                    planner_requests=0, managed_postgres=True)
        self.app.ai_settings = AISettings('wire-fixture-key', 'wire-fixture-model')
        self.app.agent_factory = OpenAIDeployAgent
        calls = []
        class Adapter:
            def __init__(self, event, settings, existing=None, checkpoint=None):
                pass
            def deploy(self, project, plan, attempt_id, environment,
                       postgres=None, migrations=None):
                calls.append((project, plan, attempt_id, environment, postgres, migrations))
                assert "'0.0.0.0'" in (project / 'server.js').read_text()
                assert 'FROM node:22' in plan.dockerfile
                return {'url': 'https://example.test', 'target': 'aws-ecs-express',
                        'database': {'database_id': 'sky-demo-app'}}
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('urllib.request.build_opener', return_value=wire), \
                patch('api.server.AwsExpressAdapter', Adapter):
            self.app.run_postgres_then_agent(job_id)
        wire.assert_complete()
        job = self.app.jobs[job_id]
        self.assertEqual(job['status'], 'succeeded', job['events'][-1])
        self.assertEqual(job['steps'], 4)
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['changes'][0]['path'], 'server.js')
        self.assertEqual(Path(job['project'], 'server.js').read_text(), original)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], {})
        self.assertEqual(calls[0][4], self.app.postgres_operations.plans['demo-app']['request'])
        self.assertEqual(calls[0][5].migrations[0].name, '0001_init.sql')

    def test_one_action_repairs_python_database_connection_before_deployment(self):
        original = ('from flask import Flask\nimport psycopg\napp = Flask(__name__)\n'
                    '@app.get("/health")\ndef health():\n    with psycopg.connect(host="localhost"):\n'
                    '        return {"ok": True}\n')
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as bundle:
            bundle.writestr('app.py', original)
            bundle.writestr('requirements.txt', 'flask==3.1.1\ngunicorn==23.0.0\npsycopg[binary]==3.3.6\n')
            bundle.writestr('migrations/0001_init.sql', 'CREATE TABLE demo (id int);')
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            status, payload = self.upload(content.getvalue(), postgres=False, create_plan_id=token)
        self.assertEqual(status, 202)
        job_id = payload['id']
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'sky-demo-app')
        actions = [
            ('read_project_files', {'paths': ['app.py', 'requirements.txt']}),
            ('apply_project_patch', {'path': 'app.py', 'old_text': 'host="localhost"',
                                     'new_text': 'connect_timeout=5'}),
            ('configure_deployment', {'start_script': 'wsgi:app.py', 'build_script': None,
                                      'port': 3000, 'health_path': '/health',
                                      'required_env': ['PGHOST', 'PGPASSWORD']}),
            ('deploy_application', {}),
        ]
        wire = ResponsesWireFixture(actions, expected_target='aws-ecs-express',
                                    planner_requests=0, managed_postgres=True)
        self.app.ai_settings = AISettings('wire-fixture-key', 'wire-fixture-model')
        self.app.agent_factory = OpenAIDeployAgent
        calls = []
        class Adapter:
            def __init__(self, event, settings, existing=None, checkpoint=None):
                pass
            def deploy(self, project, plan, attempt_id, environment,
                       postgres=None, migrations=None):
                calls.append((project, plan, environment, postgres, migrations))
                assert 'psycopg.connect(connect_timeout=5)' in (project / 'app.py').read_text()
                assert plan.runtime == 'python-wsgi'
                assert 'gunicorn' in plan.dockerfile
                return {'url': 'https://example.test', 'target': 'aws-ecs-express',
                        'database': {'database_id': 'sky-demo-app'}}
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('urllib.request.build_opener', return_value=wire), \
                patch('api.server.AwsExpressAdapter', Adapter):
            self.app.run_postgres_then_agent(job_id)
        wire.assert_complete()
        job = self.app.jobs[job_id]
        self.assertEqual(job['status'], 'succeeded', job['events'][-1])
        self.assertEqual(job['steps'], 4)
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(job['changes'][0]['path'], 'app.py')
        self.assertEqual((Path(job['project']) / 'app.py').read_text(), original)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], {})
        self.assertEqual(calls[0][3], self.app.postgres_operations.plans['demo-app']['request'])
        self.assertEqual(calls[0][4].migrations[0].name, '0001_init.sql')

    def test_provisioning_waits_for_confirmed_database_before_agent(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        operation = self.created_operation('running')
        self.app.postgres_operations.operations['demo-app'] = operation
        def finish_create(_seconds):
            operation['status'] = 'succeeded'
            operation['database_id'] = 'sky-demo-app'
        with patch('api.server.time.sleep', side_effect=finish_create) as sleep, \
                patch('api.server.AwsPostgresProvisioner.inspect_current',
                      return_value={'database_id': 'sky-demo-app'}), \
                patch('api.server.DeploymentAgent.run',
                      return_value={'url': 'https://example.test', 'target': 'aws-ecs-express'}):
            self.app.run_postgres_then_agent(payload['id'])
        sleep.assert_called_once_with(3)
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'succeeded')

    def test_creation_does_not_auto_deploy_changed_source(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'sky-demo-app')
        project_file = Path(self.app.jobs[payload['id']]['project']) / 'server.js'
        project_file.write_text(project_file.read_text() + '\n// changed during DB creation')
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect, \
                patch.object(self.app, 'run_agent') as deploy:
            self.app.run_postgres_then_agent(payload['id'])
        inspect.assert_not_called()
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'interrupted')
        self.assertIn('앱 소스가 변경', self.app.jobs[payload['id']]['events'][-1]['message'])

    def test_old_job_does_not_deploy_from_a_later_creation_attempt(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        operation = self.created_operation('succeeded', 'sky-demo-app')
        operation['creation_id'] = 'b' * 16
        self.app.postgres_operations.operations['demo-app'] = operation
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect, \
                patch.object(self.app, 'run_agent') as deploy:
            self.app.run_postgres_then_agent(payload['id'])
        inspect.assert_not_called()
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[payload['id']]['status'], 'interrupted')
        self.assertIn('다릅니다', self.app.jobs[payload['id']]['events'][-1]['message'])

    def test_uncertain_creation_never_starts_agent_and_restart_does_not_retry(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            status, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        self.assertEqual(status, 202)
        job_id = payload['id']
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.jobs[job_id]['status'], 'interrupted')
        self.app.postgres_operations.operations['demo-app'] = self.created_operation('needs_attention')
        with patch.object(self.app, 'run_agent') as deploy:
            self.app.run_postgres_then_agent(job_id)
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[job_id]['status'], 'interrupted')

    def test_confirmed_creation_can_manually_resume_untouched_app_job(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        job_id = payload['id']
        self.app.jobs[job_id]['status'] = 'interrupted'
        self.app.save(job_id)
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'sky-demo-app')
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}) as inspect, \
                patch('api.server.threading.Thread.start') as thread, \
                patch.object(self.app.postgres_operations, 'start') as create:
            result = self.app.resume_postgres_deployment(job_id)
        self.assertEqual(result, {'id': job_id, 'status': 'running'})
        self.assertEqual(self.app.jobs[job_id]['status'], 'running')
        self.assertEqual(self.app.jobs[job_id]['events'][-1]['stage'], 'database_manual_resume')
        inspect.assert_called_once_with()
        thread.assert_called_once_with()
        create.assert_not_called()
        with self.assertRaisesRegex(ValueError, '재개할 수 없는'):
            self.app.resume_postgres_deployment(job_id)

    def test_restart_preserves_manual_resume_without_recreating_rds(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        operation = self.created_operation('succeeded', 'sky-demo-app')
        self.app.postgres_operations._save(operation)
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.jobs[payload['id']]['status'], 'interrupted')
        self.assertEqual(restored.postgres_operations.get('demo-app')['creation_id'], 'a' * 16)
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('api.server.threading.Thread.start') as thread, \
                patch.object(restored.postgres_operations, 'start') as create:
            result = restored.resume_postgres_deployment(payload['id'])
        self.assertEqual(result['status'], 'running')
        thread.assert_called_once_with()
        create.assert_not_called()

    def test_manual_resume_rejects_unconfirmed_or_touched_job(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        job_id = payload['id']
        self.app.jobs[job_id]['status'] = 'interrupted'
        operation = self.created_operation('needs_attention')
        self.app.postgres_operations.operations['demo-app'] = operation
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect:
            with self.assertRaisesRegex(ValueError, '성공 기록'):
                self.app.resume_postgres_deployment(job_id)
        inspect.assert_not_called()
        operation.update(status='succeeded', database_id='sky-demo-app',
                         creation_id='b' * 16)
        with self.assertRaisesRegex(ValueError, '성공 기록'):
            self.app.resume_postgres_deployment(job_id)
        operation['creation_id'] = 'a' * 16
        self.app.jobs[job_id]['steps'] = 1
        with self.assertRaisesRegex(ValueError, '재개할 수 없는'):
            self.app.resume_postgres_deployment(job_id)

    def test_manual_resume_rejects_changed_source_or_creation_request(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        job_id = payload['id']
        job = self.app.jobs[job_id]
        job['status'] = 'interrupted'
        operation = self.created_operation('succeeded', 'sky-demo-app')
        self.app.postgres_operations.operations['demo-app'] = operation
        project_file = Path(job['project']) / 'server.js'
        original = project_file.read_text()
        project_file.write_text(original + '\n// edited after upload')
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect:
            with self.assertRaisesRegex(ValueError, '앱 소스가 변경'):
                self.app.resume_postgres_deployment(job_id)
        inspect.assert_not_called()
        project_file.write_text(original)
        operation['request']['service_security_group'] = 'sg-44444444'
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect:
            with self.assertRaisesRegex(ValueError, 'DB 설정이 배포 작업과 다릅니다'):
                self.app.resume_postgres_deployment(job_id)
        inspect.assert_not_called()
        self.assertEqual(job['status'], 'interrupted')

    def test_manual_resume_does_not_replace_a_newer_active_release(self):
        token = self.reviewed_create_plan()
        with patch.object(self.app.postgres_operations, 'start',
                          return_value={'status': 'running', 'creation_id': 'a' * 16}):
            _, payload = self.upload(archive(), postgres=False, create_plan_id=token)
        job_id = payload['id']
        self.app.jobs[job_id]['status'] = 'interrupted'
        self.app.postgres_operations.operations['demo-app'] = self.created_operation(
            'succeeded', 'sky-demo-app')
        self.app.jobs['b' * 16] = {'id': 'b' * 16, 'application_id': 'demo-app',
                                   'target': 'aws-ecs-express', 'status': 'succeeded',
                                   'deployment_state': 'active'}
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('api.server.threading.Thread.start') as thread:
            with self.assertRaisesRegex(ValueError, '다른 AWS 릴리스'):
                self.app.resume_postgres_deployment(job_id)
        thread.assert_not_called()
        self.assertEqual(self.app.jobs[job_id]['status'], 'interrupted')

    def test_manual_postgres_resume_route_requires_authentication(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/deployments/' + 'a' * 16 + '/resume-postgres'
        handler.headers = {'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app, 'resume_postgres_deployment') as resume:
            handler.do_POST()
        resume.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)
        handler.headers['X-Sky-Token'] = self.app.token
        with patch.object(self.app, 'resume_postgres_deployment',
                          return_value={'id': 'a' * 16, 'status': 'running'}) as resume:
            handler.do_POST()
        resume.assert_called_once_with('a' * 16)
        self.assertEqual(handler.json_response.call_args.args[0], 202)

    def test_interrupted_migration_inspection_records_owned_task_without_redeploy(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            _, payload = self.upload(archive())
        job_id = payload['id']
        attempt = job_id + '-a1'
        definition = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-migrate-{attempt}:1'
        task_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'a' * 32
        job = self.app.jobs[job_id]
        job.update(status='interrupted', attempts=1, steps=4,
                   aws_migration_status='running', aws_migration_task_arn=task_arn,
                   aws_migration_task_definition_arn=definition)
        self.app.save(job_id)
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        def aws(_adapter, args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecs', 'describe-tasks']:
                return json.dumps({'tasks': [{'taskArn': task_arn,
                    'taskDefinitionArn': definition,
                    'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                    'launchType': 'FARGATE', 'lastStatus': 'STOPPED',
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-app', 'value': 'demo-app'},
                             {'key': 'sky-attempt', 'value': attempt}],
                    'containers': [{'name': 'migration', 'exitCode': 0}]}]})
            raise AssertionError(args)
        with patch('api.server.AwsExpressAdapter.aws', autospec=True, side_effect=aws) as called, \
                patch.object(restored, 'run_agent') as deploy:
            result = restored.inspect_interrupted_aws_migration(job_id)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(called.call_count, 2)
        deploy.assert_not_called()
        self.assertEqual(restored.jobs[job_id]['status'], 'interrupted')
        self.assertEqual(restored.jobs[job_id]['aws_migration_status'], 'running')
        self.assertEqual(restored.jobs[job_id]['aws_migration_inspection']['status'], 'succeeded')
        self.assertEqual(json.loads((self.root / job_id / 'job.json').read_text())
                         ['aws_migration_inspection']['status'], 'succeeded')
        with patch('api.server.AwsExpressAdapter.aws', autospec=True,
                   return_value=json.dumps({'Account': '000000000000'})) as wrong_account:
            with self.assertRaisesRegex(Exception, '현재 AWS 계정'):
                restored.inspect_interrupted_aws_migration(job_id)
        wrong_account.assert_called_once()
        restored.jobs[job_id]['attempts'] = 0
        with patch('api.server.AwsExpressAdapter.aws') as no_aws:
            with self.assertRaisesRegex(ValueError, '중단된 AWS SQL'):
                restored.inspect_interrupted_aws_migration(job_id)
        no_aws.assert_not_called()

    def test_interrupted_migration_inspection_route_requires_token_and_empty_body(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/jobs/' + 'a' * 16 + '/migration/inspect'
        handler.headers = {'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app, 'inspect_interrupted_aws_migration') as inspect:
            handler.do_POST()
        inspect.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)
        handler.headers['X-Sky-Token'] = self.app.token
        handler.headers['Content-Length'] = '2'
        with patch.object(self.app, 'inspect_interrupted_aws_migration') as inspect:
            handler.do_POST()
        inspect.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        handler.headers['Content-Length'] = '0'
        with patch.object(self.app, 'inspect_interrupted_aws_migration',
                          return_value={'status': 'succeeded'}) as inspect:
            handler.do_POST()
        inspect.assert_called_once_with('a' * 16)
        self.assertEqual(handler.json_response.call_args.args, (200, {'status': 'succeeded'}))

    def test_interrupted_migration_cleanup_journals_progress_without_redeploy(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            _, payload = self.upload(archive())
        job_id = payload['id']
        attempt = job_id + '-a1'
        definition = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-migrate-{attempt}:1'
        task_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'a' * 32
        image = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:{attempt}-db'
        job = self.app.jobs[job_id]
        job.update(status='interrupted', attempts=1, steps=4,
                   aws_migration_task_arn=task_arn,
                   aws_migration_task_definition_arn=definition,
                   aws_migration_image=image, aws_migration_image_digest='sha256:' + 'b' * 64,
                   aws_migration_inspection={'status': 'succeeded', 'task_arn': task_arn,
                                             'task_definition_arn': definition})
        self.app.save(job_id)
        def cleanup(_adapter, _request, _attempt, _task, _definition, _image, _digest,
                    *, definition_inactive, verified_outcome, checkpoint):
            self.assertEqual(self.app.jobs[job_id]['aws_migration_cleanup_state'], 'running')
            self.assertFalse(definition_inactive)
            self.assertFalse(verified_outcome)
            checkpoint()
            self.assertTrue(json.loads((self.root / job_id / 'job.json').read_text())
                            ['aws_migration_cleanup_definition_inactive'])
            return {'state': 'done', 'image_deleted': True,
                    'task_definition_arn': definition, 'image': image}
        with patch('api.server.AwsExpressAdapter.aws', autospec=True,
                   return_value=json.dumps({'Account': ACCOUNT})) as aws, \
                patch('deployment.aws_migrations.cleanup_interrupted_migration',
                      side_effect=cleanup) as cleaner, \
                patch.object(self.app, 'run_agent') as deploy:
            result = self.app.cleanup_interrupted_aws_migration(job_id)
        self.assertEqual(result['state'], 'done')
        self.assertEqual(aws.call_count, 1)
        self.assertEqual(cleaner.call_args.args[2:7],
                         (attempt, task_arn, definition, image, 'sha256:' + 'b' * 64))
        deploy.assert_not_called()
        self.assertEqual(job['status'], 'interrupted')
        self.assertEqual(job['aws_migration_cleanup_state'], 'done')
        self.assertTrue(job['aws_migration_cleanup_image_deleted'])
        with self.assertRaisesRegex(ValueError, '정리할 수 있는'):
            self.app.cleanup_interrupted_aws_migration(job_id)

    def test_interrupted_migration_cleanup_restores_failed_retry_state(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            _, payload = self.upload(archive())
        job_id = payload['id']
        job = self.app.jobs[job_id]
        task_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'a' * 32
        definition = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-migrate-{job_id}-a1:1'
        job.update(status='interrupted', attempts=1, aws_migration_task_arn=task_arn,
                   aws_migration_task_definition_arn=definition,
                   aws_migration_image=f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:{job_id}-a1-db',
                   aws_migration_image_digest='sha256:' + 'b' * 64,
                   aws_migration_inspection={'status': 'succeeded', 'task_arn': task_arn,
                                             'task_definition_arn': definition},
                   aws_migration_cleanup_state='running',
                   aws_migration_cleanup_definition_inactive=True)
        self.app.save(job_id)
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.jobs[job_id]['aws_migration_cleanup_state'], 'failed')
        with patch('api.server.AwsExpressAdapter.aws', autospec=True,
                   return_value=json.dumps({'Account': ACCOUNT})), \
                patch('deployment.aws_migrations.cleanup_interrupted_migration',
                      side_effect=RuntimeError('cleanup failed')) as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                restored.cleanup_interrupted_aws_migration(job_id)
        self.assertTrue(cleanup.call_args.kwargs['definition_inactive'])
        self.assertEqual(restored.jobs[job_id]['aws_migration_cleanup_state'], 'failed')
        self.assertEqual(restored.jobs[job_id]['aws_migration_cleanup_error'], 'cleanup failed')

    def test_interrupted_migration_cleanup_uses_journaled_success_after_task_expires(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            _, payload = self.upload(archive())
        job_id = payload['id']
        attempt = job_id + '-a1'
        task_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'a' * 32
        definition = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-migrate-{attempt}:1'
        image = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:{attempt}-db'
        digest = 'sha256:' + 'b' * 64
        job = self.app.jobs[job_id]
        job.update(status='interrupted', attempts=1, aws_migration_status='succeeded',
                   aws_migration_task_arn=task_arn, aws_migration_task_definition_arn=definition,
                   aws_migration_image=image, aws_migration_image_digest=digest,
                   aws_migration_result={'task_arn': task_arn, 'task_definition_arn': definition,
                                         'image': image, 'image_digest': digest},
                   aws_migration_inspection={'status': 'unknown', 'task_arn': task_arn,
                                             'task_definition_arn': definition},
                   aws_migration_cleanup_definition_inactive=True)
        self.app.save(job_id)
        with patch('api.server.AwsExpressAdapter.aws', autospec=True,
                   return_value=json.dumps({'Account': ACCOUNT})), \
                patch('deployment.aws_migrations.cleanup_interrupted_migration',
                      return_value={'state': 'done', 'image_deleted': False}) as cleanup:
            self.app.cleanup_interrupted_aws_migration(job_id)
        self.assertTrue(cleanup.call_args.kwargs['definition_inactive'])
        self.assertTrue(cleanup.call_args.kwargs['verified_outcome'])
        self.assertEqual(job['aws_migration_cleanup_state'], 'done')

    def test_successful_deployment_can_finish_failed_migration_cleanup(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            _, payload = self.upload(archive())
        job_id = payload['id']
        attempt = job_id + '-a1'
        task_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'a' * 32
        definition = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-migrate-{attempt}:1'
        image = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:{attempt}-db'
        digest = 'sha256:' + 'b' * 64
        job = self.app.jobs[job_id]
        job.update(status='succeeded', attempts=1,
                   result={'migration': {'cleanup_complete': False}},
                   aws_migration_status='succeeded', aws_migration_task_arn=task_arn,
                   aws_migration_task_definition_arn=definition,
                   aws_migration_image=image, aws_migration_image_digest=digest,
                   aws_migration_result={'task_arn': task_arn, 'task_definition_arn': definition,
                                         'image': image, 'image_digest': digest},
                   aws_migration_cleanup_definition_inactive=True)
        self.app.save(job_id)
        with patch('api.server.AwsExpressAdapter.aws', autospec=True,
                   return_value=json.dumps({'Account': ACCOUNT})), \
                patch('deployment.aws_migrations.cleanup_interrupted_migration',
                      return_value={'state': 'done', 'image_deleted': True}) as cleanup, \
                patch.object(self.app, 'run_agent') as deploy:
            self.app.cleanup_interrupted_aws_migration(job_id)
        deploy.assert_not_called()
        self.assertTrue(cleanup.call_args.kwargs['definition_inactive'])
        self.assertTrue(cleanup.call_args.kwargs['verified_outcome'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertTrue(job['result']['migration']['cleanup_complete'])
        self.assertEqual(job['aws_migration_cleanup_state'], 'done')
        with self.assertRaisesRegex(ValueError, '정리할 수 있는'):
            self.app.cleanup_interrupted_aws_migration(job_id)

    def test_interrupted_migration_cleanup_route_requires_token_and_empty_body(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/jobs/' + 'a' * 16 + '/migration/cleanup'
        handler.headers = {'Content-Length': '0'}
        handler.json_response = Mock()
        with patch.object(self.app, 'cleanup_interrupted_aws_migration') as cleanup:
            handler.do_POST()
        cleanup.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 403)
        handler.headers['X-Sky-Token'] = self.app.token
        handler.headers['Content-Length'] = '2'
        with patch.object(self.app, 'cleanup_interrupted_aws_migration') as cleanup:
            handler.do_POST()
        cleanup.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        handler.headers['Content-Length'] = '0'
        with patch.object(self.app, 'cleanup_interrupted_aws_migration',
                          return_value={'state': 'done'}) as cleanup:
            handler.do_POST()
        cleanup.assert_called_once_with('a' * 16)
        self.assertEqual(handler.json_response.call_args.args, (200, {'state': 'done'}))

    def test_opt_in_upload_persists_and_restores_postgres_request(self):
        database = {'database_id': 'sky-demo-app'}
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database) as inspect:
            status, payload = self.upload(archive())
        self.assertEqual(status, 202)
        inspect.assert_called_once_with()
        job_id = payload['id']
        job = self.app.jobs[job_id]
        self.assertEqual(job['postgres']['application_id'], 'demo-app')
        self.assertEqual(job['postgres']['subnet_ids'], ['subnet-11111111', 'subnet-22222222'])
        infrastructure = job['infrastructure_plan']
        self.assertEqual(infrastructure['workload'], 'postgresql-http')
        self.assertEqual(infrastructure['database'],
                         {'binding': 'existing', 'database_id': 'sky-demo-app'})
        self.assertIn('existing RDS PostgreSQL', infrastructure['resources'])
        self.assertIn('one-off SQL migration task', infrastructure['resources'])
        self.assertIn('package.json', infrastructure['detected_files'])
        self.assertTrue(infrastructure['compatibility']['compatible'])
        self.assertTrue(infrastructure['compatibility']['postgres_binding'])
        self.assertEqual(infrastructure['compatibility']['database_engines'], ['postgresql'])
        self.assertNotIn('password', json.dumps(job).lower())
        with patch('api.server.DeploymentTools') as tools, \
                patch('api.server.DeploymentAgent') as agent:
            agent.return_value.run.return_value = {'url': 'https://example.test'}
            self.app.run_agent(job_id)
        self.assertEqual(tools.call_args.kwargs['postgres_request'].application_id, 'demo-app')
        restored = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.recovery_warnings, [])
        self.assertEqual(postgres_request_from_job(restored.jobs[job_id]).subnet_ids,
                         ('subnet-11111111', 'subnet-22222222'))

    def test_python_postgres_zip_upload_records_existing_database_binding(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            status, payload = self.upload(probe_archive('python'))
        self.assertEqual(status, 202)
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['target'], 'aws-ecs-express')
        self.assertEqual(job['infrastructure_plan']['workload'], 'postgresql-http')
        self.assertIn('app.py', job['infrastructure_plan']['detected_files'])
        self.assertEqual(job['infrastructure_plan']['database'],
                         {'binding': 'existing', 'database_id': 'sky-demo-app'})
        self.assertTrue((Path(job['project']) / 'app.py').is_file())
        self.assertFalse((Path(job['project']) / 'Dockerfile').exists())

    def test_python_postgres_update_upload_reuses_owned_release_and_database(self):
        database = {'database_id': 'sky-demo-app'}
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database):
            first_status, first_payload = self.upload(probe_archive('python', 'v1'))
            self.assertEqual(first_status, 202)
            first = self.app.jobs[first_payload['id']]
            first['status'] = 'succeeded'
            first['result'] = {'database': database, 'url': 'https://example.test',
                               'service_arn': 'arn:aws:ecs:ap-northeast-2:123456789012:service/default/probe',
                               'image': 'example:v1'}
            self.app.save(first['id'])
            second_status, second_payload = self.upload(probe_archive('python', 'v2'))
        self.assertEqual(second_status, 202)
        second = self.app.jobs[second_payload['id']]
        self.assertEqual(second['replaces_job_id'], first['id'])
        self.assertEqual(second['prior_result'], first['result'])
        self.assertEqual(second['postgres']['application_id'], 'demo-app')
        self.assertIn("version='v1'", (Path(first['project']) / 'app.py').read_text())
        self.assertIn("version='v2'", (Path(second['project']) / 'app.py').read_text())

    def test_auto_target_uses_only_supported_owned_postgres_path(self):
        database = {'database_id': 'sky-demo-app'}
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database), \
                patch.object(self.app, 'infrastructure_planner_factory') as planner:
            status, payload = self.upload(archive(), target='auto')
        self.assertEqual(status, 202)
        planner.assert_not_called()
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['requested_target'], 'auto')
        self.assertEqual(job['target'], 'aws-ecs-express')
        self.assertEqual(job['infrastructure_plan']['planner'], 'policy')
        self.assertEqual(job['infrastructure_plan']['database']['binding'], 'existing')

    def test_single_action_existing_rds_upload_resolves_owned_network(self):
        database = {'database_id': 'sky-demo-app', 'account': ACCOUNT,
                    'region': REGION, 'vpc_id': 'vpc-12345678',
                    'subnet_ids': ['subnet-11111111', 'subnet-22222222']}
        with patch('api.server.discover_existing_postgres',
                   return_value=database) as discover, \
                patch('api.server.AwsPostgresProvisioner.inspect_current',
                      return_value=database) as inspect:
            status, payload = self.upload(archive(), target='auto', network_headers=False)
        self.assertEqual(status, 202)
        discover.assert_called_once_with('demo-app', self.settings)
        inspect.assert_called_once_with()
        job = self.app.jobs[payload['id']]
        self.assertEqual(job['postgres']['vpc_id'], 'vpc-12345678')
        self.assertEqual(job['postgres']['subnet_ids'], database['subnet_ids'])
        self.assertEqual(job['target'], 'aws-ecs-express')

    def test_upload_waits_for_confirmed_database_creation(self):
        database = {'database_id': 'sky-demo-app'}
        operation = {'status': 'running', 'database_id': 'sky-demo-app'}
        self.app.postgres_operations.operations['demo-app'] = operation
        for status_name in ('running', 'needs_attention', 'recovering'):
            with self.subTest(status=status_name), patch(
                    'api.server.AwsPostgresProvisioner.inspect_current',
                    return_value=database):
                operation['status'] = status_name
                status, payload = self.upload(archive())
                self.assertEqual(status, 400)
                self.assertIn('재확인', payload['error'])
                self.assertFalse(self.app.jobs)

        operation['status'] = 'succeeded'
        operation['database_id'] = 'sky-other-app'
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database):
            status, payload = self.upload(archive())
        self.assertEqual(status, 400)
        self.assertIn('식별자', payload['error'])
        self.assertFalse(self.app.jobs)

        operation['database_id'] = database['database_id']
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value=database):
            status, payload = self.upload(archive())
        self.assertEqual(status, 202)
        self.assertEqual(self.app.jobs[payload['id']]['infrastructure_plan']['database']['database_id'],
                         database['database_id'])

    def test_upload_rejects_untrusted_creation_journal(self):
        self.app.postgres_operations.untrusted_applications.add('demo-app')
        with patch('api.server.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}):
            status, payload = self.upload(archive())
        self.assertEqual(status, 400)
        self.assertIn('기록', payload['error'])
        self.assertFalse(self.app.jobs)

    def test_partial_postgres_network_headers_are_rejected_before_discovery(self):
        with patch('api.server.discover_existing_postgres') as discover:
            status, payload = self.upload(archive(), partial_network=True)
        self.assertEqual(status, 400)
        self.assertIn('함께', payload['error'])
        discover.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_auto_target_without_explicit_binding_still_rejects_postgres(self):
        status, payload = self.upload(archive(), postgres=False, target='auto')
        self.assertEqual(status, 400)
        self.assertIn('데이터베이스', payload['error'])
        self.assertFalse(self.app.jobs)

    def test_auto_postgres_requires_public_aws_path_before_db_inspection(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect:
            status, payload = self.upload(archive(), target='auto', public=False)
        self.assertEqual(status, 400)
        self.assertIn('공개 AWS', payload['error'])
        inspect.assert_not_called()

    def test_missing_sql_bundle_rejects_before_db_inspection(self):
        with patch('api.server.AwsPostgresProvisioner.inspect_current') as inspect:
            status, payload = self.upload(archive(include_migration=False))
        self.assertEqual(status, 400)
        self.assertIn('migrations/', payload['error'])
        inspect.assert_not_called()
        self.assertFalse(self.app.jobs)

    def test_existing_database_release_cannot_be_replaced_without_binding(self):
        prior_id = 'f' * 16
        self.app.jobs[prior_id] = {'id': prior_id, 'application_id': 'demo-app',
            'target': 'aws-ecs-express', 'status': 'succeeded',
            'deployment_state': 'active', 'created_at': '2026-01-01T00:00:00+00:00',
            'result': {'database': {'database_id': 'sky-demo-app'}}}
        status, payload = self.upload(archive(include_migration=False, database=False),
                                      postgres=False)
        self.assertEqual(status, 400)
        self.assertIn('동일한 DB 연결', payload['error'])
        self.assertEqual(list(self.app.jobs), [prior_id])


if __name__ == '__main__':
    unittest.main()
