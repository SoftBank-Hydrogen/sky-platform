import json
import unittest
from unittest.mock import patch

from sky_platform.deployment.aws import AwsConfigurationError, AwsSettings
from sky_platform.database.postgres_restore import plan_restore_drill


APP = 'demo-app'
SNAPSHOT = 'sky-demo-app-backup-20261002'
TARGET = 'sky-restore-demo-app-drill-20261002'
ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
SOURCE_ID = 'sky-demo-app'
SOURCE_ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:db:{SOURCE_ID}'
SNAPSHOT_ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:{SNAPSHOT}'


class RestorePlanTests(unittest.TestCase):
    def setUp(self):
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    service_security_group='sg-33333333')
        self.owned = {'snapshot_arn': SNAPSHOT_ARN, 'status': 'available'}
        self.source = {'database_id': SOURCE_ID, 'vpc_id': 'vpc-12345678',
                       'engine_version': '18.3', 'status': 'available'}
        self.snapshot = {'DBSnapshotArn': SNAPSHOT_ARN,
                         'DBInstanceIdentifier': SOURCE_ID, 'VpcId': 'vpc-12345678',
                         'Engine': 'postgres', 'EngineVersion': '18.3',
                         'AllocatedStorage': 20, 'StorageType': 'gp3',
                         'Encrypted': True, 'Status': 'available'}
        self.instance = {'DBInstanceIdentifier': SOURCE_ID, 'DBInstanceArn': SOURCE_ARN,
                         'DBInstanceStatus': 'available', 'DBInstanceClass': 'db.t4g.micro',
                         'AllocatedStorage': 20, 'StorageType': 'gp3',
                         'PubliclyAccessible': False,
                         'DBSubnetGroup': {'VpcId': 'vpc-12345678',
                                           'DBSubnetGroupName': 'sky-db-demo-app-subnets'}}

    def test_plan_requires_owned_available_snapshot_and_separate_target(self):
        calls = []
        responses = [{'DBSnapshots': [self.snapshot]},
                     {'DBInstances': [self.instance]},
                     {'DBInstances': [self.instance]}]
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            return json.dumps(responses.pop(0))
        with patch('sky_platform.database.postgres_restore.inspect_snapshot', return_value=self.owned), \
                patch('sky_platform.database.postgres_restore.discover_existing_postgres',
                      return_value=self.source), \
                patch('sky_platform.database.postgres_restore.AwsExpressAdapter.aws', autospec=True,
                      side_effect=aws), \
                patch('sky_platform.database.postgres_restore.estimate_postgres_base_capacity',
                      return_value={'baseline_730h_usd': '20.87'}):
            result = plan_restore_drill(APP, SNAPSHOT, TARGET, self.settings)
        self.assertEqual(result['target_database_id'], TARGET)
        self.assertTrue(result['isolated_security_group_required'])
        self.assertFalse(result['source_security_group_reused'])
        self.assertFalse(result['restore_request_enabled'])
        self.assertEqual(result['restore_security_group_name'], TARGET + '-db')
        self.assertEqual(calls, [['rds', 'describe-db-snapshots'],
                                 ['rds', 'describe-db-instances'],
                                 ['rds', 'describe-db-instances']])

    def test_existing_target_or_wrong_snapshot_network_blocks_plan(self):
        with patch('sky_platform.database.postgres_restore.inspect_snapshot', return_value=self.owned), \
                patch('sky_platform.database.postgres_restore.discover_existing_postgres',
                      return_value=self.source), \
                patch('sky_platform.database.postgres_restore.AwsExpressAdapter.aws', side_effect=[
                    json.dumps({'DBSnapshots': [{**self.snapshot, 'VpcId': 'vpc-99999999'}]})]):
            with self.assertRaisesRegex(AwsConfigurationError, '스냅샷'):
                plan_restore_drill(APP, SNAPSHOT, TARGET, self.settings)
        with patch('sky_platform.database.postgres_restore.inspect_snapshot', return_value=self.owned), \
                patch('sky_platform.database.postgres_restore.discover_existing_postgres',
                      return_value=self.source), \
                patch('sky_platform.database.postgres_restore.AwsExpressAdapter.aws', side_effect=[
                    json.dumps({'DBSnapshots': [self.snapshot]}),
                    json.dumps({'DBInstances': [self.instance]}),
                    json.dumps({'DBInstances': [self.instance,
                                               {'DBInstanceIdentifier': TARGET}]})]):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                plan_restore_drill(APP, SNAPSHOT, TARGET, self.settings)

    def test_target_name_is_constrained_before_aws_calls(self):
        with patch('sky_platform.database.postgres_restore.inspect_snapshot') as inspect:
            with self.assertRaisesRegex(ValueError, '복원 대상 ID'):
                plan_restore_drill(APP, SNAPSHOT, SOURCE_ID, self.settings)
        inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
