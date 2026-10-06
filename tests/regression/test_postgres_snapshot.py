import json
import unittest
from unittest.mock import patch

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.postgres_snapshot import (create_snapshot, inspect_snapshot, main,
                                          plan_snapshot)


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
APP = 'demo-app'
DATABASE = 'sky-demo-app'
SNAPSHOT = 'sky-demo-app-before-migration'
ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:{SNAPSHOT}'


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    service_security_group='sg-33333333')
        self.backups = {'database_id': DATABASE, 'database_status': 'available',
                        'manual_snapshot_count': 0}
        self.snapshot = {'DBSnapshotIdentifier': SNAPSHOT,
                         'DBInstanceIdentifier': DATABASE, 'DBSnapshotArn': ARN,
                         'SnapshotType': 'manual', 'Status': 'creating', 'Encrypted': True}

    def test_plan_is_read_only_and_rejects_duplicate_or_unavailable_db(self):
        with patch('adapters.aws.postgres_snapshot.inspect_postgres_backup_status',
                   return_value=self.backups), \
                patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                      return_value=json.dumps({'DBSnapshots': []})) as aws:
            plan = plan_snapshot(APP, SNAPSHOT, self.settings)
        self.assertEqual(plan['snapshot_arn'], ARN)
        self.assertEqual(aws.call_args.args[0][:2], ['rds', 'describe-db-snapshots'])
        with patch('adapters.aws.postgres_snapshot.inspect_postgres_backup_status',
                   return_value=self.backups), \
                patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                      return_value=json.dumps({'DBSnapshots': [self.snapshot]})):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                plan_snapshot(APP, SNAPSHOT, self.settings)
        with patch('adapters.aws.postgres_snapshot.inspect_postgres_backup_status',
                   return_value={**self.backups, 'database_status': 'modifying'}), \
                patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '사용 가능한'):
                plan_snapshot(APP, SNAPSHOT, self.settings)
            aws.assert_not_called()

    def test_create_uses_pinned_db_id_and_app_tags(self):
        with patch('adapters.aws.postgres_snapshot.inspect_postgres_backup_status',
                   return_value=self.backups), \
                patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                      side_effect=[json.dumps({'DBSnapshots': []}),
                                   json.dumps({'DBSnapshot': self.snapshot})]) as aws:
            result = create_snapshot(APP, SNAPSHOT, self.settings)
        self.assertEqual(result['status'], 'creating')
        args = aws.call_args.args[0]
        self.assertEqual(args[:2], ['rds', 'create-db-snapshot'])
        self.assertIn(DATABASE, args)
        self.assertIn(SNAPSHOT, args)
        self.assertIn('Key=sky-app,Value=demo-app', args)
        with patch('adapters.aws.postgres_snapshot.inspect_postgres_backup_status',
                   return_value=self.backups), \
                patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                      side_effect=[json.dumps({'DBSnapshots': []}),
                                   json.dumps({'DBSnapshot': {**self.snapshot,
                                       'DBSnapshotArn': 'arn:aws:rds:us-east-1:999999999999:snapshot:wrong'}})]):
            with self.assertRaisesRegex(AwsConfigurationError, '확인하지 못했습니다'):
                create_snapshot(APP, SNAPSHOT, self.settings)

    def test_inspect_requires_owned_encrypted_snapshot(self):
        with patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                      side_effect=[json.dumps({'Account': ACCOUNT}),
                                   json.dumps({'DBSnapshots': [self.snapshot]}),
                                   json.dumps({'TagList': [
                                       {'Key': 'sky-managed', 'Value': 'true'},
                                       {'Key': 'sky-app', 'Value': APP}]})]):
            self.assertEqual(inspect_snapshot(APP, SNAPSHOT, self.settings)['snapshot_arn'], ARN)
        with patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                      side_effect=[json.dumps({'Account': ACCOUNT}),
                                   json.dumps({'DBSnapshots': [self.snapshot]}),
                                   json.dumps({'TagList': [{'Key': 'sky-app',
                                                            'Value': 'other-app'}]})]):
            with self.assertRaisesRegex(AwsConfigurationError, '소유 태그'):
                inspect_snapshot(APP, SNAPSHOT, self.settings)
        with patch('adapters.aws.postgres_snapshot.AwsExpressAdapter.aws',
                   return_value=json.dumps({'Account': '999999999999'})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '계정'):
                inspect_snapshot(APP, SNAPSHOT, self.settings)
        aws.assert_called_once()

    def test_cli_defaults_to_plan_and_requires_apply_for_mutation(self):
        argv = ['--application', APP, '--snapshot-id', SNAPSHOT, '--account', ACCOUNT,
                '--region', REGION, '--service-security-group', 'sg-33333333']
        with patch('adapters.aws.postgres_snapshot.plan_snapshot', return_value={'snapshot_id': SNAPSHOT}) as plan, \
                patch('adapters.aws.postgres_snapshot.create_snapshot') as create:
            main(argv)
        plan.assert_called_once()
        create.assert_not_called()
        with patch('adapters.aws.postgres_snapshot.create_snapshot',
                   return_value={'snapshot_id': SNAPSHOT}) as create:
            main(argv + ['--apply'])
        create.assert_called_once()


if __name__ == '__main__':
    unittest.main()
