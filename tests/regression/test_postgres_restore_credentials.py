import json
import unittest
from unittest.mock import patch

from sky_platform.deployment.aws import AwsConfigurationError, AwsSettings
from sky_platform.database.postgres_restore_credentials import plan_restore_credentials


APP = 'demo-app'
SNAPSHOT = 'sky-demo-app-backup-20261002'
TARGET = 'sky-restore-demo-app-drill-20261002'
ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
SOURCE = 'sky-demo-app'
SECRET = f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:rds!db-example-AbCdEf'
SNAPSHOT_ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:{SNAPSHOT}'
SOURCE_ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:db:{SOURCE}'
VERSION = '406a01d7-f0d5-4c65-9092-4d425937d179'


class RestoreCredentialTests(unittest.TestCase):
    def setUp(self):
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    account_pin_required=True,
                                    service_security_group='sg-33333333')
        self.snapshot = {'DBSnapshotArn': SNAPSHOT_ARN, 'DBInstanceIdentifier': SOURCE,
                         'VpcId': 'vpc-12345678', 'Status': 'available',
                         'SnapshotCreateTime': '2026-10-01T15:10:08+00:00'}
        self.source = {'DBInstanceArn': SOURCE_ARN, 'DBInstanceStatus': 'available',
                       'MasterUserSecret': {'SecretArn': SECRET}}
        self.versions = {'ARN': SECRET, 'Versions': [{'VersionId': VERSION,
                          'CreatedDate': '2026-10-01T00:47:53+09:00',
                          'VersionStages': ['AWSCURRENT']}]}

    def _plan(self, versions=None, snapshot=None):
        replies = [{'DBSnapshots': [snapshot or self.snapshot]},
                   {'DBInstances': [self.source]}, versions or self.versions]
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            return json.dumps(replies.pop(0))
        with patch('sky_platform.database.postgres_restore_credentials.inspect_snapshot',
                   return_value={'snapshot_arn': SNAPSHOT_ARN, 'status': 'available'}), \
                patch('sky_platform.database.postgres_restore_credentials.discover_existing_postgres',
                      return_value={'database_id': SOURCE, 'vpc_id': 'vpc-12345678'}), \
                patch('sky_platform.database.postgres_restore_credentials.AwsExpressAdapter.aws',
                      autospec=True, side_effect=aws):
            result = plan_restore_credentials(APP, SNAPSHOT, TARGET, self.settings)
        return result, calls

    def test_pins_current_secret_version_without_getting_its_value(self):
        plan, calls = self._plan()
        self.assertEqual(plan['secret_version_id'], VERSION)
        self.assertEqual(plan['ecs_password_value_from'], SECRET + ':password::' + VERSION)
        self.assertTrue(plan['password_match_unverified'])
        self.assertFalse(plan['secret_value_read'])
        self.assertEqual(calls, [['rds', 'describe-db-snapshots'],
                                 ['rds', 'describe-db-instances'],
                                 ['secretsmanager', 'list-secret-version-ids']])

    def test_refuses_secret_created_after_snapshot(self):
        versions = {'ARN': SECRET, 'Versions': [{**self.versions['Versions'][0],
                    'CreatedDate': '2026-10-02T00:00:00+00:00'}]}
        with self.assertRaisesRegex(AwsConfigurationError, '늦거나'):
            self._plan(versions=versions)

    def test_refuses_ambiguous_or_incomplete_secret_versions(self):
        duplicate = {'ARN': SECRET, 'Versions': self.versions['Versions'] * 2}
        with self.assertRaisesRegex(AwsConfigurationError, '불확실'):
            self._plan(versions=duplicate)
        truncated = {**self.versions, 'NextToken': 'more'}
        with self.assertRaisesRegex(AwsConfigurationError, '완전히'):
            self._plan(versions=truncated)

    def test_refuses_snapshot_ownership_drift(self):
        with self.assertRaisesRegex(AwsConfigurationError, '스냅샷 원본'):
            self._plan(snapshot={**self.snapshot, 'VpcId': 'vpc-99999999'})


if __name__ == '__main__':
    unittest.main()
