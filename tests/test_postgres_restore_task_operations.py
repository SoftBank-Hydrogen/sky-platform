import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.aws import AwsConfigurationError, AwsSettings
from sky_platform.runtime.migrations import collect_sql_migrations
from sky_platform.runtime.postgres_restore_task_operations import RestoreVerificationOperations


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
TARGET = 'sky-restore-demo-app-drill-20261002'
DEFINITION = (f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/'
              'sky-restore-verify-aaaaaaaaaaaaaaaa:1')
TASK = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'b' * 32


class RestoreVerificationOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / 'state'
        self.project = root / 'app'
        (self.project / 'migrations').mkdir(parents=True)
        (self.project / 'migrations' / '0001_init.sql').write_text('CREATE TABLE demo(id int);')
        self.bundle = collect_sql_migrations(self.project)
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    account_pin_required=True)
        self.plan = {'application_id': 'demo-app', 'target_database_id': TARGET,
                     'account': ACCOUNT, 'region': REGION, 'vpc_id': 'vpc-12345678',
                     'bundle_digest': self.bundle.digest, 'endpoint': 'db.example.test',
                     'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/demo',
                     'repository': f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed',
                     'username_value_from': 'secret-username',
                     'password_value_from': 'secret-password',
                     'log_group': '/aws/ecs/demo', 'migration_count': 1}

    def test_persists_before_execution_and_blocks_duplicate_start(self):
        operations = RestoreVerificationOperations(self.state, self.settings)
        def execute(runner, _bundle):
            record = operations._read(TARGET)
            self.assertEqual(record['stage'], 'planned')
            self.assertEqual(record['status'], 'running')
            runner.checkpoint('image_verified', image_digest='sha256:' + 'c' * 64)
            runner.checkpoint('registered', task_definition_arn=DEFINITION)
            runner.checkpoint('launched', task_arn=TASK)
            runner.checkpoint('cleaned')
            return {'cleanup': {'status': 'cleaned'}}
        with patch('sky_platform.runtime.postgres_restore_task_operations.secrets.token_hex',
                   return_value='a' * 16), \
                patch('sky_platform.runtime.postgres_restore_task_operations.RestoreVerifierRunner.execute',
                      autospec=True, side_effect=execute):
            result = operations.start(self.plan, self.bundle)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['task_arn'], TASK)
        self.assertEqual(os.stat(self.state / (TARGET + '.json')).st_mode & 0o777, 0o600)
        restarted = RestoreVerificationOperations(self.state, self.settings)
        self.assertEqual(restarted.get(TARGET)['status'], 'succeeded')
        with self.assertRaisesRegex(ValueError, '이미'):
            restarted.start(self.plan, self.bundle)

    def test_uncertain_launch_is_retained_and_reconciled_read_only(self):
        operations = RestoreVerificationOperations(self.state, self.settings)
        def fail(runner, _bundle):
            runner.checkpoint('registered', task_definition_arn=DEFINITION)
            runner.checkpoint('launching')
            raise AwsConfigurationError('unknown')
        with patch('sky_platform.runtime.postgres_restore_task_operations.secrets.token_hex',
                   return_value='a' * 16), \
                patch('sky_platform.runtime.postgres_restore_task_operations.RestoreVerifierRunner.execute',
                      autospec=True, side_effect=fail):
            with self.assertRaisesRegex(AwsConfigurationError, 'unknown'):
                operations.start(self.plan, self.bundle)
        restarted = RestoreVerificationOperations(self.state, self.settings)
        self.assertEqual(restarted.get(TARGET)['status'], 'needs_attention')
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecr', 'list-images']:
                return json.dumps({'imageIds': [{'imageTag': 'restore-verify-' + 'a' * 16}]})
            return json.dumps({'taskDefinitionArns': [DEFINITION]})
        with patch('sky_platform.runtime.postgres_restore_task_operations.AwsExpressAdapter.aws',
                   autospec=True, side_effect=aws):
            result = restarted.reconcile(TARGET)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertTrue(result['recovery']['image_present'])
        self.assertEqual(result['recovery']['active_definition_arns'], [DEFINITION])
        self.assertEqual(calls, [['sts', 'get-caller-identity'],
                                 ['ecr', 'list-images'],
                                 ['ecs', 'list-task-definitions']])

    def test_reconcile_checks_task_sql_and_cleanup_without_mutation(self):
        operations = RestoreVerificationOperations(self.state, self.settings)
        def fail(runner, _bundle):
            runner.checkpoint('image_verified', image_digest='sha256:' + 'c' * 64)
            runner.checkpoint('registered', task_definition_arn=DEFINITION)
            runner.checkpoint('launched', task_arn=TASK)
            raise AwsConfigurationError('unknown')
        with patch('sky_platform.runtime.postgres_restore_task_operations.secrets.token_hex',
                   return_value='a' * 16), \
                patch('sky_platform.runtime.postgres_restore_task_operations.RestoreVerifierRunner.execute',
                      autospec=True, side_effect=fail):
            with self.assertRaises(AwsConfigurationError):
                operations.start(self.plan, self.bundle)
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecr', 'list-images']:
                return json.dumps({'imageIds': []})
            if args[:2] == ['ecs', 'list-task-definitions']:
                return json.dumps({'taskDefinitionArns': []})
            return json.dumps({'taskDefinition': {'taskDefinitionArn': DEFINITION,
                                                   'status': 'INACTIVE'}})
        with patch('sky_platform.runtime.postgres_restore_task_operations.AwsExpressAdapter.aws',
                   autospec=True, side_effect=aws), \
                patch('sky_platform.runtime.postgres_restore_task_operations.RestoreVerifierRunner.inspect_result',
                      return_value={'status': 'succeeded', 'log_stream': 'verified'}):
            result = operations.reconcile(TARGET)
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['sql_verified'])
        self.assertTrue(result['cleanup_complete'])
        self.assertEqual(calls, [['sts', 'get-caller-identity'],
                                 ['ecr', 'list-images'],
                                 ['ecs', 'list-task-definitions'],
                                 ['ecs', 'describe-task-definition']])


if __name__ == '__main__':
    unittest.main()
