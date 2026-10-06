import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from application.deployment_core import source_digest
from tests.live.smoke_aws_postgres import main


class AwsPostgresSmokeTests(unittest.TestCase):
    def test_cleanup_mode_deletes_only_the_named_record_without_migration(self):
        record_id = 'a' * 32
        arguments = ['--apply', '--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333', '--probe-runtime', 'python',
                     '--cleanup-record-id', record_id]
        calls = []
        class Adapter:
            image = 'v1'
            image_pushed = True
            def deploy(self, project, plan, attempt, environment, postgres=None, migrations=None):
                assert plan.runtime == 'python-wsgi'
                assert migrations is None
                assert postgres.application_id == 'demo-app'
                calls.append(('deploy', attempt))
                return {'url': 'https://example.test', 'service_arn': 'service-arn', 'image': 'v1'}
            def aws(self, *_args, **_kwargs):
                return json.dumps({'service': {'currentDeployment': None,
                    'activeConfigurations': [{'primaryContainer': {'image': 'v1'}}]}})
            def retire(self, result, attempt):
                calls.append(('retire', result['images'], attempt))
        with patch('tests.live.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app',
                                 'stack_id': 'owned-stack', 'status': 'available'}), \
                patch('tests.live.smoke_aws_postgres.AwsExpressAdapter', return_value=Adapter()), \
                patch('tests.live.smoke_aws_postgres.probe_version') as version, \
                patch('tests.live.smoke_aws_postgres.probe',
                      side_effect=lambda _url, _key, actual, method: calls.append((method, actual))):
            main(arguments)
        version.assert_called_once_with('https://example.test', 'v1')
        self.assertEqual([item[0] for item in calls], ['deploy', 'GET', 'DELETE', 'retire'])
        self.assertEqual(calls[1][1], record_id)
        self.assertEqual(calls[2][1], record_id)
        self.assertEqual(calls[3][1], ['v1'])

    def test_cleanup_mode_rejects_invalid_id_before_aws_lookup(self):
        arguments = ['--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333', '--probe-runtime', 'python',
                     '--cleanup-record-id', 'not-a-record']
        with patch('tests.live.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current') as inspect, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(arguments)
        inspect.assert_not_called()

    def test_default_mode_only_inspects_existing_database(self):
        arguments = ['--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333']
        for runtime in ('node', 'python'):
            with self.subTest(runtime=runtime), \
                    patch('tests.live.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current',
                          return_value={'database_id': 'sky-demo-app',
                                        'stack_id': 'owned-stack', 'status': 'available'}) as inspect, \
                    patch('tests.live.smoke_aws_postgres.AwsExpressAdapter.deploy') as deploy:
                main(arguments + ['--probe-runtime', runtime])
                inspect.assert_called_once_with()
                deploy.assert_not_called()

    def test_apply_migrates_before_probe_and_reuses_schema_on_update(self):
        for runtime in ('node', 'python'):
            with self.subTest(runtime=runtime):
                self.assert_apply_migrates_before_probe_and_reuses_schema_on_update(runtime)

    def test_cleanup_failure_does_not_report_success(self):
        self.assert_apply_migrates_before_probe_and_reuses_schema_on_update(
            'python', retire_error=True)

    def test_deploy_error_keeps_its_cause_when_cleanup_also_fails(self):
        arguments = ['--apply', '--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333', '--probe-runtime', 'python']
        class FailingAdapter:
            def deploy(self, *_args, **_kwargs):
                raise ValueError('build failed')
            def cleanup_failure(self, _attempt):
                raise RuntimeError('cleanup failed')
        with patch('tests.live.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app',
                                 'stack_id': 'owned-stack', 'status': 'available'}), \
                patch('tests.live.smoke_aws_postgres.AwsExpressAdapter', return_value=FailingAdapter()):
            with self.assertRaisesRegex(ValueError, 'build failed') as caught:
                main(arguments)
        self.assertIn('cleanup failed', '\n'.join(caught.exception.__notes__))

    def assert_apply_migrates_before_probe_and_reuses_schema_on_update(self, runtime,
                                                                      retire_error=False):
        arguments = ['--apply', '--application', 'demo-app', '--account', '123456789012',
                     '--region', 'ap-northeast-2', '--vpc-id', 'vpc-12345678',
                     '--subnet-id', 'subnet-11111111', '--subnet-id', 'subnet-22222222',
                     '--service-security-group', 'sg-33333333', '--probe-runtime', runtime]
        service_arn = 'arn:aws:ecs:ap-northeast-2:123456789012:service/default/probe'
        calls = []
        class Adapter:
            def __init__(self, image):
                self.image = image
                self.image_pushed = True
            def deploy(self, project, plan, attempt, environment, postgres=None, migrations=None):
                self.assertions(project, plan, migrations)
                assert plan.source_digest == source_digest(project)
                if self.image == 'v1':
                    (Path(project) / '.dockerignore').write_text('node_modules\n')
                    (Path(project) / 'Dockerfile').write_text('FROM scratch\n')
                calls.append(('deploy', self.image, migrations.digest))
                return {'url': 'https://example.test', 'service_arn': service_arn,
                        'image': self.image, 'migration': {
                            'bundle_digest': migrations.digest, 'cleanup_complete': True}}
            def assertions(self, project, plan, migrations):
                assert migrations.migrations[0].name == '9000_sky_probe.sql'
                entry = 'app.py' if runtime == 'python' else 'server.js'
                assert 'CREATE TABLE' not in (Path(project) / entry).read_text()
                marker = (f"version='{self.image}'" if runtime == 'python'
                          else f"version: '{self.image}'")
                assert marker in (Path(project) / entry).read_text()
                assert plan.runtime == ('python-wsgi' if runtime == 'python' else 'custom-dockerfile')
                original_dockerfile = (Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apps'
                                       / f'postgres-probe-{runtime}' / 'Dockerfile')
                dockerfile = Path(project) / 'Dockerfile'
                assert dockerfile.exists() == original_dockerfile.exists()
                if original_dockerfile.exists():
                    assert dockerfile.read_bytes() == original_dockerfile.read_bytes()
            def aws(self, args, **_kwargs):
                return json.dumps({'service': {'currentDeployment': None,
                    'activeConfigurations': [{'primaryContainer': {'image': 'v2'}}]}})
            def retire(self, result, attempt):
                calls.append(('retire', result['image']))
                if retire_error:
                    raise RuntimeError('retire failed')
        first, second = Adapter('v1'), Adapter('v2')
        def probe(_url, _key, _record_id, method):
            calls.append(('probe', method))
        def probe_version(_url, version):
            calls.append(('version', version))
        with patch('tests.live.smoke_aws_postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app',
                                 'stack_id': 'owned-stack', 'status': 'available'}), \
                patch('tests.live.smoke_aws_postgres.AwsExpressAdapter', side_effect=[first, second]), \
                patch('tests.live.smoke_aws_postgres.probe', side_effect=probe), \
                patch('tests.live.smoke_aws_postgres.probe_version', side_effect=probe_version):
            if retire_error:
                with self.assertRaisesRegex(RuntimeError, 'retire failed'):
                    main(arguments)
            else:
                main(arguments)
        self.assertEqual([item[:2] for item in calls], [
            ('deploy', 'v1'), ('version', 'v1'), ('probe', 'POST'), ('probe', 'GET'),
            ('deploy', 'v2'), ('version', 'v2'), ('probe', 'GET'), ('probe', 'DELETE'),
            ('retire', 'v2')])
        self.assertEqual(calls[0][2], calls[4][2])


if __name__ == '__main__':
    unittest.main()
