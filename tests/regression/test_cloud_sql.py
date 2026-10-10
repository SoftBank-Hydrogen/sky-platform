import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.database.migrations import collect_sql_migrations, stage_migrator_context
from adapters.gcp.cloud_run import CloudConfigurationError, CloudRunAdapter, CloudRunSettings
from adapters.gcp.postgres import CloudSqlRequest
from application.agent import DeploymentTools
from application.analysis import AISettings
from application.consistency import check_database_consistency, check_sqlite_migration_consistency
from application.deployment_core import analyze
from application.infrastructure import inspect_infrastructure, preflight_sqlite_conversion
from application.state_recovery import postgres_request_from_job
from engine.compatibility import explicit_infrastructure_plan, infrastructure_compatibility
from engine.deployment_policy import deployment_policy
from interfaces.http.server import App, handler_for
from tests.regression.test_local_sqlite import archive, sqlite_project


REQUEST = CloudSqlRequest('demo-app', 'test-project', 'asia-northeast3', 'sky-demo',
                          'demo', 'demo_app', 'demo-password:1')
SETTINGS = CloudRunSettings(REQUEST.project, REQUEST.region)
ATTEMPT = 'a' * 16 + '-a1'


class CloudSqlTests(unittest.TestCase):
    def test_binding_rejects_unsafe_identifiers_and_unpinned_secrets(self):
        REQUEST.validate()
        for changes in ({'password_secret': 'password:latest'}, {'password_secret': 'projects/other/secrets/p:1'},
                        {'user': 'postgres'}, {'database': 'postgres'}, {'instance': '--other'},
                        {'database': 'db,PGUSER=postgres'}, {'project': 'other/project'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(REQUEST, **changes).validate()
        self.assertNotIn('PGPASSWORD', REQUEST.environment())
        self.assertEqual(REQUEST.environment()['PGHOST'], '/cloudsql/test-project:asia-northeast3:sky-demo')

    def metadata(self, args, **kwargs):
        if args[:3] == ['sql', 'instances', 'describe']:
            return json.dumps({'connectionName': REQUEST.connection_name, 'region': REQUEST.region,
                'state': 'RUNNABLE', 'databaseVersion': 'POSTGRES_16', 'ipAddresses': [{'type': 'PRIMARY'}],
                'settings': {'userLabels': {'sky-app': REQUEST.application_id}}})
        if args[:3] == ['sql', 'databases', 'describe']:
            return json.dumps({'name': REQUEST.database, 'instance': REQUEST.instance})
        if args[:3] == ['sql', 'users', 'list']:
            return json.dumps([{'name': REQUEST.user, 'type': 'BUILT_IN'}])
        if args[:3] == ['secrets', 'versions', 'describe']:
            return json.dumps({'state': 'ENABLED'})
        raise AssertionError(args)

    def test_inspection_reads_metadata_without_password_or_mutation(self):
        command = Mock(side_effect=self.metadata)
        self.assertEqual(REQUEST.inspect(command)['database_id'], REQUEST.database_id)
        self.assertFalse(any(word in {'access', 'create', 'delete', 'patch'}
                             for call in command.call_args_list for word in call.args[0]))
        wrong = json.loads(self.metadata(['sql', 'instances', 'describe']))
        wrong['settings']['userLabels']['sky-app'] = 'someone-else'
        with self.assertRaisesRegex(ValueError, '소유'):
            REQUEST.inspect(Mock(return_value=json.dumps(wrong)))
        wrong['settings']['userLabels']['sky-app'] = REQUEST.application_id
        wrong['ipAddresses'] = [{'type': 'PRIVATE'}]
        with self.assertRaisesRegex(ValueError, '사설'):
            REQUEST.inspect(Mock(return_value=json.dumps(wrong)))

    def test_snapshot_conversion_preserves_original_and_compiles_cloud_sql_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = sqlite_project(root)
            original = (source / 'data/scores.db').read_bytes()
            conversion, profile = preflight_sqlite_conversion(source, inspect_infrastructure(source))
            plan = explicit_infrastructure_plan('cloud-run', profile, existing_postgres_id=REQUEST.database_id)
            plan['compatibility'] = infrastructure_compatibility(profile, 'cloud-run', postgres=True, public_access=True)
            plan['conversion_pending'] = 'sqlite-to-postgresql'
            policy = deployment_policy('cloud-run', True, allow_data_migration=True)
            tools = DeploymentTools(source, root / 'work', 'a' * 16, {}, lambda *_: None, lambda **_: None,
                adapter_factory=Mock(), target='cloud-run', postgres_request=REQUEST,
                infrastructure_plan=plan, sqlite_conversion=conversion, deployment_policy=policy)
            tools.prepare_sqlite_migration()
            bundle = collect_sql_migrations(tools.work)
            self.assertEqual(check_sqlite_migration_consistency(plan, profile, source, conversion,
                bundle, REQUEST, policy)['status'], 'pass')
            self.assertEqual(check_database_consistency(plan, profile, postgres_request=REQUEST,
                sqlite_conversion=conversion)['status'], 'pass')
            self.assertEqual((source / 'data/scores.db').read_bytes(), original)
            self.assertFalse((tools.work / 'data/scores.db').exists())
            context = stage_migrator_context(bundle, root / 'migrator', cloud_sql=True)
            self.assertNotIn('rds-global', (context / 'Dockerfile').read_text())
            self.assertEqual((context / 'migrations/0000_sky_sqlite_import.sql').read_bytes(),
                             (tools.work / 'migrations/0000_sky_sqlite_import.sql').read_bytes())

    def test_upload_records_cloud_sql_and_recovery_keeps_provider_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = archive(sqlite_project(root))
            app = App(root / 'state', AISettings('test-key'), cloud_settings=SETTINGS, monitor_interval=0)
            binding = {k: v for k, v in asdict(REQUEST).items() if k in {'instance', 'database', 'user', 'password_secret'}}

            def upload(target='cloud-run', extra=None):
                handler = handler_for(app).__new__(handler_for(app))
                handler.path = '/api/deployments'
                handler.headers = {'X-Sky-Token': app.token, 'X-Application-Id': REQUEST.application_id,
                    'X-Deploy-Target': target, 'X-Public-Access': 'true', 'X-Sqlite-Convert': 'true',
                    'X-GCP-Postgres': json.dumps(binding), 'Content-Length': str(len(payload)), **(extra or {})}
                handler.rfile = io.BytesIO(payload)
                handler.json_response = Mock()
                with patch('interfaces.http.server.threading.Thread.start'), \
                        patch.object(CloudRunSettings, 'unavailable_reason', return_value=None), \
                        patch.object(CloudSqlRequest, 'inspect', return_value={'database_id': REQUEST.database_id}):
                    handler.do_POST()
                return handler.json_response.call_args.args

            status, response = upload('local-docker')
            self.assertEqual(status, 400)
            status, response = upload(extra={'X-Postgres-Existing': 'true'})
            self.assertEqual(status, 400)
            status, response = upload()
            self.assertEqual(status, 202, response)
            job = app.jobs[response['id']]
            self.assertEqual(postgres_request_from_job(job), REQUEST)
            self.assertEqual(job['compilation']['target_plan']['execution_configuration']['database_mode'],
                             'existing_cloud_sql')
            self.assertNotIn('postgres', job)
            with self.assertRaises(ValueError):
                postgres_request_from_job({**job, 'target': 'aws-ecs-express'})
            with self.assertRaises(ValueError):
                postgres_request_from_job({**job, 'cloud': {**job['cloud'], 'project': 'other-project'}})
            with self.assertRaises(ValueError):
                postgres_request_from_job({**job, 'application_id': 'other-app'})

    def exercise_deploy(self, fail_migration=False):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('require("pg");')
            (project / 'migrations').mkdir()
            (project / 'migrations/0001_start.sql').write_text('CREATE TABLE scores (id integer);')
            bundle = collect_sql_migrations(project)
            plan = replace(analyze(project), target='cloud-run', required_env=['PGHOST', 'PGPASSWORD'])
            calls = []
            adapter = CloudRunAdapter(lambda *_: None, SETTINGS, public=True)

            def cloud(args, **kwargs):
                calls.append(args)
                if args[0] in {'sql', 'secrets'}:
                    return self.metadata(args, **kwargs)
                if args[:2] == ['auth', 'print-access-token']:
                    return 'test-token-not-logged'
                if args[:3] in (['run', 'services', 'list'], ['run', 'jobs', 'list']):
                    return '[]'
                if args[:3] == ['run', 'jobs', 'execute']:
                    if fail_migration:
                        raise TimeoutError('unknown execution result')
                    return json.dumps({'metadata': {'name': 'migration-execution'}, 'status': {
                        'succeededCount': 1, 'conditions': [{'type': 'Completed', 'status': 'True'}]}})
                if args[:3] == ['run', 'jobs', 'describe']:
                    return json.dumps({'metadata': {'name': f'sky-{ATTEMPT}-migrate',
                        'labels': {'sky-managed': 'true', 'sky-attempt': ATTEMPT}}})
                if args[:2] == ['run', 'deploy'] or args[:3] == ['run', 'jobs', 'create']:
                    env = json.loads(Path(args[args.index('--env-vars-file') + 1]).read_text())
                    self.assertNotIn('PGPASSWORD', env)
                    self.assertEqual(env['PGHOST'], REQUEST.environment()['PGHOST'])
                    self.assertIn('PGPASSWORD=demo-password:1', args)
                    if args[:2] == ['run', 'deploy']:
                        self.assertTrue(any(c[:3] == ['run', 'jobs', 'execute'] for c in calls))
                        return json.dumps({'status': {'url': 'https://sky-test.run.app'}})
                return '{}'

            with patch.object(adapter, 'prepare_infrastructure'), patch.object(adapter, 'gcloud', side_effect=cloud), \
                    patch.object(adapter, 'command', return_value='linux/amd64'), patch.object(adapter, 'verify'):
                if fail_migration:
                    with self.assertRaises(CloudConfigurationError) as error:
                        adapter.deploy(project, plan, ATTEMPT, {}, postgres=REQUEST, migrations=bundle)
                    self.assertFalse(error.exception.retryable)
                else:
                    result = adapter.deploy(project, plan, ATTEMPT, {}, postgres=REQUEST, migrations=bundle)
                    self.assertEqual(result['database']['database_id'], REQUEST.database_id)
                    self.assertEqual(result['migration']['digest'], bundle.digest)
            self.assertFalse(any(c[:2] in (['sql', 'delete'], ['secrets', 'delete']) for c in calls))
            return calls

    def test_cloud_deploy_runs_sql_before_service_and_cleans_worker(self):
        calls = self.exercise_deploy()
        self.assertTrue(any(c[:3] == ['run', 'jobs', 'delete'] for c in calls))

    def test_uncertain_sql_execution_blocks_service_and_retains_worker(self):
        calls = self.exercise_deploy(fail_migration=True)
        self.assertFalse(any(c[:2] == ['run', 'deploy'] or 'delete' in c for c in calls))

