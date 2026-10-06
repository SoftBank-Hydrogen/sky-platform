import json
import unittest
from unittest.mock import patch

from sky_platform.deployment.aws import AwsConfigurationError
from sky_platform.database.postgres_restore_network import RestoreNetworkRequest, RestoreSecurityGroup


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
TARGET = 'sky-restore-demo-app-drill-20261002'
GROUP_ID = 'sg-12345678'


class RestoreNetworkTests(unittest.TestCase):
    def setUp(self):
        self.request = RestoreNetworkRequest('demo-app', TARGET, ACCOUNT, REGION, VPC)
        self.manager = RestoreSecurityGroup(self.request)
        self.group = {'GroupId': GROUP_ID, 'GroupName': TARGET + '-db',
                      'OwnerId': ACCOUNT, 'VpcId': VPC,
                      'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                               {'Key': 'sky-app', 'Value': 'demo-app'},
                               {'Key': 'sky-restore-target', 'Value': TARGET}],
                      'IpPermissions': [], 'IpPermissionsEgress': []}

    def test_preflight_reads_account_vpc_and_absent_group_only(self):
        replies = [{'Account': ACCOUNT}, {'Vpcs': [{'VpcId': VPC}]},
                   {'SecurityGroups': []}]
        commands = []
        def aws(args, **_kwargs):
            commands.append(args[:2])
            return json.dumps(replies.pop(0))
        with patch.object(self.manager.adapter, 'aws', side_effect=aws):
            plan = self.manager.preflight()
        self.assertEqual(plan['group_name'], TARGET + '-db')
        self.assertEqual(commands, [['sts', 'get-caller-identity'],
                                    ['ec2', 'describe-vpcs'],
                                    ['ec2', 'describe-security-groups']])
        with patch.object(self.manager.adapter, 'aws', side_effect=[
                json.dumps({'Account': ACCOUNT}), json.dumps({'Vpcs': [{'VpcId': VPC}]}),
                json.dumps({'SecurityGroups': [self.group]})]):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.manager.preflight()

    def test_create_revokes_default_egress_then_checks_owned_empty_rules(self):
        default_egress = [{'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}]
        replies = [{'Account': ACCOUNT}, {'Vpcs': [{'VpcId': VPC}]},
                   {'SecurityGroups': []}, {'GroupId': GROUP_ID},
                   {'SecurityGroups': [{**self.group,
                                       'IpPermissionsEgress': default_egress}]},
                   {}, {'Account': ACCOUNT}, {'SecurityGroups': [self.group]}]
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            return json.dumps(replies.pop(0))
        with patch.object(self.manager.adapter, 'aws', side_effect=aws):
            result = self.manager.create()
        self.assertEqual(result['group_id'], GROUP_ID)
        self.assertEqual(calls[3][:2], ['ec2', 'create-security-group'])
        self.assertEqual(calls[5][:2], ['ec2', 'revoke-security-group-egress'])
        self.assertIn('sky-restore-target', calls[3][-1])

    def test_create_refuses_to_change_group_without_expected_ownership(self):
        replies = [{'Account': ACCOUNT}, {'Vpcs': [{'VpcId': VPC}]},
                   {'SecurityGroups': []}, {'GroupId': GROUP_ID},
                   {'SecurityGroups': [{**self.group, 'Tags': []}]}]
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            return json.dumps(replies.pop(0))
        with patch.object(self.manager.adapter, 'aws', side_effect=aws):
            with self.assertRaisesRegex(AwsConfigurationError, '생성 직후'):
                self.manager.create()
        self.assertEqual(calls[-1], ['ec2', 'describe-security-groups'])

    def test_delete_refuses_attached_group(self):
        with patch.object(self.manager, 'inspect', return_value={'group_id': GROUP_ID}), \
                patch.object(self.manager.adapter, 'aws', return_value=json.dumps({
                    'NetworkInterfaces': [{'NetworkInterfaceId': 'eni-12345678'}]})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '인터페이스'):
                self.manager.delete(GROUP_ID)
        self.assertEqual(aws.call_count, 1)

    def test_delete_checks_references_and_exact_group_id(self):
        with patch.object(self.manager, 'inspect', return_value={'group_id': GROUP_ID}), \
                patch.object(self.manager.adapter, 'aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '생성 기록'):
                self.manager.delete('sg-99999999')
        aws.assert_not_called()
        referenced = {**self.group, 'IpPermissions': [{'UserIdGroupPairs': [
            {'GroupId': GROUP_ID}]}]}
        with patch.object(self.manager, 'inspect', return_value={'group_id': GROUP_ID}), \
                patch.object(self.manager.adapter, 'aws', side_effect=[
                    json.dumps({'NetworkInterfaces': []}),
                    json.dumps({'SecurityGroups': [referenced]})]) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '참조'):
                self.manager.delete(GROUP_ID)
        self.assertEqual(aws.call_count, 2)

    def test_delete_requires_unused_owned_group_and_confirms_absence(self):
        replies = [{'NetworkInterfaces': []}, {'SecurityGroups': [self.group]},
                   {}, {'SecurityGroups': []}]
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            return json.dumps(replies.pop(0))
        with patch.object(self.manager, 'inspect', return_value={'group_id': GROUP_ID}), \
                patch.object(self.manager.adapter, 'aws', side_effect=aws):
            result = self.manager.delete(GROUP_ID)
        self.assertEqual(result, {'group_id': GROUP_ID, 'status': 'deleted'})
        self.assertEqual(calls, [['ec2', 'describe-network-interfaces'],
                                 ['ec2', 'describe-security-groups'],
                                 ['ec2', 'delete-security-group'],
                                 ['ec2', 'describe-security-groups']])


if __name__ == '__main__':
    unittest.main()
