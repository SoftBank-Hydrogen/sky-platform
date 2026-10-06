import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from planning.agent import AgentError, DeploymentAgent, DeploymentTools
from database.postgres import PostgresRequest
from tests.live.smoke_aws_postgres_api import PostgresFixture, archive


class PostgresAgentTests(unittest.TestCase):
    def test_update_archives_change_app_code_without_changing_migration(self):
        for runtime, entry in (('node', 'server.js'), ('python', 'app.py')):
            with self.subTest(runtime=runtime):
                with zipfile.ZipFile(io.BytesIO(archive(runtime, 'v1'))) as first, \
                        zipfile.ZipFile(io.BytesIO(archive(runtime, 'v2'))) as second:
                    self.assertNotEqual(first.read(entry), second.read(entry))
                    self.assertEqual(first.read('migrations/9000_sky_probe.sql'),
                                     second.read('migrations/9000_sky_probe.sql'))
                self.assertNotIn('version=', (Path('tests/fixtures/apps') /
                    f'postgres-probe-{runtime}' / entry).read_text())

    def test_python_api_fixture_uploads_and_deploys_managed_postgres_app(self):
        with zipfile.ZipFile(io.BytesIO(archive('python'))) as bundle:
            names = set(bundle.namelist())
        self.assertIn('app.py', names)
        self.assertIn('requirements.txt', names)
        self.assertIn('migrations/9000_sky_probe.sql', names)
        self.assertNotIn('Dockerfile', names)
        request = PostgresRequest('demo-app', '123456789012', 'ap-northeast-2', 'vpc-12345678',
                                  ('subnet-11111111', 'subnet-22222222'), 'sg-33333333')
        calls = []
        class Adapter:
            def deploy(self, project, plan, attempt_id, environment, postgres=None, migrations=None):
                calls.append((project, plan, attempt_id, environment, postgres, migrations))
                return {'url': 'https://example.test', 'target': 'aws-ecs-express'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = DeploymentTools(Path('tests/fixtures/apps/postgres-probe-python'), root / 'work',
                                    'a' * 16, {'PROBE_KEY': 'p' * 32}, lambda *_: None,
                                    lambda **_: None, target='aws-ecs-express',
                                    adapter_factory=lambda _event: Adapter(), postgres_request=request)
            result = DeploymentAgent(PostgresFixture(None, 'python'), tools).run()
            self.assertEqual(result['url'], 'https://example.test')
            self.assertEqual(len(calls), 1)
            project, plan, attempt, environment, postgres, migrations = calls[0]
            self.assertEqual(plan.runtime, 'python-wsgi')
            self.assertEqual(plan.health_path, '/health')
            self.assertFalse((project / 'Dockerfile').exists())
            self.assertEqual(attempt, 'a' * 16 + '-a1')
            self.assertEqual(environment, {'PROBE_KEY': 'p' * 32})
            self.assertEqual(postgres, request)
            self.assertEqual(migrations.migrations[0].name, '9000_sky_probe.sql')

    def test_explicit_postgres_agent_path_supplies_managed_environment(self):
        request = PostgresRequest('demo-app', '123456789012', 'ap-northeast-2', 'vpc-12345678',
                                  ('subnet-11111111', 'subnet-22222222'), 'sg-33333333')
        calls = []
        class Adapter:
            def deploy(self, project, plan, attempt_id, environment, postgres=None, migrations=None):
                calls.append((plan.required_env, environment, postgres, attempt_id, migrations))
                return {'url': 'https://example.test'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            project.mkdir()
            (project / 'package.json').write_text(json.dumps({
                'scripts': {'start': 'node server.js'}, 'dependencies': {'pg': '8.23.0'}}))
            (project / 'server.js').write_text('const {Pool} = require("pg");')
            (project / 'migrations').mkdir()
            (project / 'migrations' / '0001_init.sql').write_text('CREATE TABLE demo (id int);')
            tools = DeploymentTools(project, root / 'work', 'a' * 16, {}, lambda *_: None,
                                    lambda **_: None, target='aws-ecs-express',
                                    adapter_factory=lambda _event: Adapter(), postgres_request=request)
            ready = tools.configure_deployment('start', None, 3000, '/', ['PGHOST', 'PGPASSWORD'])
            self.assertEqual(ready['missing_environment'], [])
            self.assertTrue(tools.request_environment(['PGHOST', 'PGPASSWORD'], 'DB')['available'])
            class Provider:
                def next(self, history):
                    initial = json.loads(history[0]['content'])
                    self.assert_managed(initial)
                    return [{'type': 'function_call', 'call_id': 'stop', 'name': 'report_blocker',
                             'arguments': json.dumps({'reason': 'fixture done'})}]
                def assert_managed(self, initial):
                    assert initial['managed_postgres_connection'] is True
                    assert 'PGPASSWORD' in initial['available_environment_names']
            with self.assertRaises(AgentError):
                DeploymentAgent(Provider(), tools).run()
            self.assertTrue(tools.deploy_application()['verified'])
            self.assertEqual(calls[0][1], {})
            self.assertEqual(calls[0][2], request)
            self.assertEqual(calls[0][4].migrations[0].name, '0001_init.sql')
            with self.assertRaisesRegex(ValueError, 'DATABASE_URL'):
                tools.configure_deployment('start', None, 3000, '/', ['DATABASE_URL'])


if __name__ == '__main__':
    unittest.main()
