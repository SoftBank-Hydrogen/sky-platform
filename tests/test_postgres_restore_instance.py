import json
import unittest
from unittest.mock import patch

from sky_platform.deployment.aws import AwsConfigurationError, AwsSettings
from sky_platform.database.postgres_restore_instance import RestoreInstance


APP = 'demo-app'
SNAPSHOT = 'sky-demo-app-backup-20261002'
TARGET = 'sky-restore-demo-app-drill-20261002'
ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
GROUP = 'sg-12345678'


class RestoreInstanceTests(unittest.TestCase):
    def setUp(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT, account_pin_required=True)
        self.restore = RestoreInstance(APP, SNAPSHOT, TARGET, settings, VPC, GROUP)
        self.db = {'DBInstanceIdentifier': TARGET, 'DBInstanceArn': self.restore.arn,
                   'DBInstanceStatus': 'creating', 'Engine': 'postgres',
                   'DBInstanceClass': 'db.t4g.micro', 'AllocatedStorage': 20,
                   'StorageType': 'gp3', 'StorageEncrypted': True,
                   'PubliclyAccessible': False, 'MultiAZ': False,
                   'DeletionProtection': False,
                   'DBSubnetGroup': {'VpcId': VPC},
                   'Endpoint': {'Address': 'restore.abcdef.' + REGION + '.rds.amazonaws.com',
                                'Port': 5432},
                   'VpcSecurityGroups': [{'VpcSecurityGroupId': GROUP}]}
        self.tags = {'TagList': [{'Key': 'sky-managed', 'Value': 'true'},
                                 {'Key': 'sky-app', 'Value': APP},
                                 {'Key': 'sky-restore-target', 'Value': TARGET},
                                 {'Key': 'sky-source-snapshot', 'Value': SNAPSHOT}]}

    def test_preflight_requires_existing_owned_group(self):
        with patch('sky_platform.database.postgres_restore_instance.plan_restore_drill',
                   return_value={'vpc_id': VPC}), \
                patch.object(self.restore.network, 'inspect',
                             return_value={'group_id': GROUP}):
            self.assertEqual(self.restore.preflight()['restore_security_group_id'], GROUP)
        with patch('sky_platform.database.postgres_restore_instance.plan_restore_drill',
                   return_value={'vpc_id': VPC}), \
                patch.object(self.restore.network, 'inspect',
                             return_value={'group_id': 'sg-99999999'}):
            with self.assertRaisesRegex(AwsConfigurationError, '그룹 ID'):
                self.restore.preflight()

    def test_create_requires_matching_network_and_uses_isolated_restore_options(self):
        plan = {'vpc_id': VPC, 'instance_class': 'db.t4g.micro',
                'db_subnet_group_name': 'demo-subnets'}
        commands = []
        def aws(args, **_kwargs):
            commands.append(args)
            return json.dumps({'DBInstance': self.db})
        with patch('sky_platform.database.postgres_restore_instance.plan_restore_drill',
                   return_value=plan), patch.object(self.restore.network, 'inspect',
                   return_value={'group_id': GROUP}), patch.object(self.restore.adapter,
                   'aws', side_effect=aws):
            self.assertEqual(self.restore.create()['status'], 'creating')
        command = commands[0]
        self.assertEqual(command[:2], ['rds', 'restore-db-instance-from-db-snapshot'])
        self.assertEqual(command[command.index('--vpc-security-group-ids') + 1], GROUP)
        self.assertIn('--no-publicly-accessible', command)
        self.assertIn('--no-multi-az', command)
        self.assertIn('--no-deletion-protection', command)
        self.assertNotIn('--storage-type', command)

    def test_create_refuses_vpc_mismatch_before_restore_call(self):
        with patch('sky_platform.database.postgres_restore_instance.plan_restore_drill',
                   return_value={'vpc_id': 'vpc-99999999'}), \
                patch.object(self.restore.network, 'inspect',
                             return_value={'group_id': GROUP}), \
                patch.object(self.restore.adapter, 'aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, 'VPC'):
                self.restore.create()
        aws.assert_not_called()

    def test_inspect_checks_owned_tags_and_exact_isolation(self):
        with patch.object(self.restore.network, 'inspect',
                          return_value={'group_id': GROUP}), \
                patch.object(self.restore.adapter, 'aws', side_effect=[
                    json.dumps({'DBInstances': [self.db]}), json.dumps(self.tags)]):
            result = self.restore.inspect()
        self.assertEqual(result['status'], 'creating')
        bad = {**self.db, 'VpcSecurityGroups': [{'VpcSecurityGroupId': 'sg-99999999'}]}
        with patch.object(self.restore.network, 'inspect',
                          return_value={'group_id': GROUP}), \
                patch.object(self.restore.adapter, 'aws',
                             return_value=json.dumps({'DBInstances': [bad]})):
            with self.assertRaisesRegex(AwsConfigurationError, '구성'):
                self.restore.inspect()

    def test_probe_inspection_accepts_only_verified_temporary_link(self):
        with patch('sky_platform.database.postgres_restore_instance.RestoreProbeNetwork.inspect',
                   return_value={'status': 'open'}) as link, \
                patch.object(self.restore, '_inspect_instance',
                             return_value={'status': 'available'}):
            self.assertEqual(self.restore.inspect_for_probe('sg-87654321')['status'],
                             'available')
        link.assert_called_once_with('sg-87654321')

    def test_available_restore_requires_expected_private_endpoint(self):
        available = {**self.db, 'DBInstanceStatus': 'available'}
        with patch.object(self.restore.network, 'inspect',
                          return_value={'group_id': GROUP}), \
                patch.object(self.restore.adapter, 'aws', side_effect=[
                    json.dumps({'DBInstances': [available]}), json.dumps(self.tags)]):
            inspected = self.restore.inspect()
        self.assertEqual(inspected['port'], 5432)
        self.assertEqual(inspected['endpoint'], available['Endpoint']['Address'])
        bad = {**available, 'Endpoint': {'Address': 'public.example.com', 'Port': 5432}}
        with patch.object(self.restore.network, 'inspect',
                          return_value={'group_id': GROUP}), \
                patch.object(self.restore.adapter, 'aws',
                             return_value=json.dumps({'DBInstances': [bad]})):
            with self.assertRaisesRegex(AwsConfigurationError, '엔드포인트'):
                self.restore.inspect()

    def test_delete_requires_owned_available_instance(self):
        with patch.object(self.restore, 'inspect', return_value={'status': 'creating'}), \
                patch.object(self.restore.adapter, 'aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, 'available'):
                self.restore.delete()
        aws.assert_not_called()
        with patch.object(self.restore, 'inspect', return_value={'status': 'available'}), \
                patch.object(self.restore.adapter, 'aws', return_value=json.dumps({
                    'DBInstance': {**self.db, 'DBInstanceStatus': 'deleting'}})) as aws:
            result = self.restore.delete()
        self.assertEqual(result['status'], 'deleting')
        self.assertIn('--skip-final-snapshot', aws.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
