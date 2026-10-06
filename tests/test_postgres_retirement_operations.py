import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.aws import AwsSettings
from sky_platform.runtime.postgres_retirement_operations import PostgresRetirementOperations


class RetirementOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'retirement'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                                    service_security_group='sg-33333333')
        self.manager = PostgresRetirementOperations(self.root, self.settings)
        self.database = {'vpc_id': 'vpc-12345678',
                         'subnet_ids': ['subnet-11111111', 'subnet-22222222']}
        self.expected = {'application_id': 'demo-app', 'account': '123456789012',
            'region': 'ap-northeast-2', 'database_id': 'sky-demo-app',
            'database_arn': 'arn:aws:rds:ap-northeast-2:123456789012:db:sky-demo-app',
            'stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/sky-db-demo-app/id',
            'active_db_users': 0, 'deletion_protection': True,
            'final_snapshot_required': True, 'retained_resources': ['all manual snapshots (including final)', 'app service network']}
        patches = [patch('sky_platform.runtime.postgres_retirement_operations.discover_existing_postgres',
                         return_value=self.database),
                   patch('sky_platform.runtime.postgres_retirement_operations.plan',
                         return_value=self.expected)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_accepted_request_is_durable_and_restart_never_retries(self):
        with patch('sky_platform.runtime.postgres_retirement_operations.threading.Thread.start') as start:
            quote = self.manager.plan('demo-app')
            result = self.manager.start('demo-app', quote['plan_id'], 'sky-demo-app')
        self.assertEqual(result['status'], 'running')
        self.assertEqual(start.call_count, 1)
        stored = json.loads((self.root / 'demo-app.json').read_text())
        self.assertEqual(stored['stage'], 'planned')
        self.assertEqual(stored['request']['subnet_ids'], self.database['subnet_ids'])
        self.assertNotIn('secret_arn', json.dumps(stored))
        restarted = PostgresRetirementOperations(self.root, self.settings)
        self.assertEqual(restarted.get('demo-app')['status'], 'needs_attention')
        self.assertTrue(restarted.blocks_deployment('demo-app'))
        with self.assertRaisesRegex(ValueError, '이미'):
            restarted.plan('demo-app')

    def test_wrong_confirmation_and_changed_plan_leave_no_record(self):
        quote = self.manager.plan('demo-app')
        with self.assertRaisesRegex(ValueError, '정확히'):
            self.manager.start('demo-app', quote['plan_id'], 'other-db')
        self.assertFalse((self.root / 'demo-app.json').exists())
        with patch('sky_platform.runtime.postgres_retirement_operations.plan', return_value={**self.expected,
                   'active_db_users': 1}):
            with self.assertRaisesRegex(ValueError, '바뀌'):
                self.manager.start('demo-app', quote['plan_id'], 'sky-demo-app')
        self.assertFalse((self.root / 'demo-app.json').exists())


if __name__ == '__main__':
    unittest.main()
