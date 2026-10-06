import json
import unittest
from unittest.mock import patch

from deployment.aws import AwsConfigurationError
from database.postgres_restore_network import RestoreNetworkRequest
from database.postgres_restore_probe_network import RestoreProbeNetwork


ACCOUNT = '123456789012'
VPC = 'vpc-12345678'
TARGET = 'sky-restore-demo-app-drill-20261002'
DB = 'sg-12345678'
PROBE = 'sg-87654321'


class RestoreProbeNetworkTests(unittest.TestCase):
    def setUp(self):
        request = RestoreNetworkRequest('demo-app', TARGET, ACCOUNT, 'ap-northeast-2', VPC)
        self.network = RestoreProbeNetwork(request, DB)
        tags = [{'Key': 'sky-managed', 'Value': 'true'},
                {'Key': 'sky-app', 'Value': 'demo-app'},
                {'Key': 'sky-restore-target', 'Value': TARGET}]
        self.db = {'GroupId': DB, 'GroupName': TARGET + '-db',
                   'OwnerId': ACCOUNT, 'VpcId': VPC, 'Tags': tags,
                   'IpPermissions': [{'IpProtocol': 'tcp', 'FromPort': 5432,
                                      'ToPort': 5432, 'UserIdGroupPairs': [
                                          {'GroupId': PROBE, 'UserId': ACCOUNT}]}],
                   'IpPermissionsEgress': []}
        self.probe = {'GroupId': PROBE, 'GroupName': TARGET + '-probe',
                      'OwnerId': ACCOUNT, 'VpcId': VPC,
                      'Tags': tags + [{'Key': 'sky-probe', 'Value': 'true'}],
                      'IpPermissions': [],
                      'IpPermissionsEgress': [
                          {'IpProtocol': 'tcp', 'FromPort': 5432,
                           'ToPort': 5432, 'UserIdGroupPairs': [
                               {'GroupId': DB, 'UserId': ACCOUNT}]},
                          {'IpProtocol': 'tcp', 'FromPort': 443,
                           'ToPort': 443, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}]}

    def test_preflight_requires_sealed_owned_database_group(self):
        with patch.object(self.network.database, 'inspect',
                          return_value={'group_id': DB}), \
                patch.object(self.network, '_existing', return_value=[]):
            self.assertEqual(self.network.preflight()['database_port'], 5432)
        with patch.object(self.network.database, 'inspect',
                          return_value={'group_id': 'sg-99999999'}), \
                patch.object(self.network, '_existing') as existing:
            with self.assertRaisesRegex(AwsConfigurationError, 'ID'):
                self.network.preflight()
        existing.assert_not_called()

    def test_inspect_requires_exact_peer_rules_and_owned_groups(self):
        with patch.object(self.network.database, '_account'), \
                patch.object(self.network.database, '_matching_groups',
                             return_value=[self.db]), \
                patch.object(self.network, '_probe', return_value=self.probe):
            self.assertEqual(self.network.inspect(PROBE)['status'], 'open')
            bad = {**self.probe, 'IpPermissionsEgress': [
                *self.probe['IpPermissionsEgress'],
                {'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}]}
            with patch.object(self.network, '_probe', return_value=bad):
                with self.assertRaisesRegex(AwsConfigurationError, '규칙'):
                    self.network.inspect(PROBE)

    def test_open_removes_default_egress_before_authorizing_peer(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            return json.dumps({'GroupId': PROBE})
        with patch.object(self.network, 'preflight'), \
                patch.object(self.network, '_probe', return_value={
                    **self.probe, 'IpPermissionsEgress': [
                        {'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}]}), \
                patch.object(self.network.adapter, 'aws', side_effect=aws), \
                patch.object(self.network, 'inspect', return_value={'status': 'open'}):
            self.network.open()
        self.assertEqual(calls, [['ec2', 'create-security-group'],
                                 ['ec2', 'revoke-security-group-egress'],
                                 ['ec2', 'authorize-security-group-egress'],
                                 ['ec2', 'authorize-security-group-ingress']])

    def test_close_refuses_attached_probe_and_removes_db_ingress_first(self):
        with patch.object(self.network, 'inspect', return_value={'status': 'open'}), \
                patch.object(self.network.adapter, 'aws', return_value=json.dumps({
                    'NetworkInterfaces': [{'NetworkInterfaceId': 'eni-12345678'}]})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '태스크'):
                self.network.close(PROBE)
        self.assertEqual(aws.call_count, 1)
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            return json.dumps({'NetworkInterfaces': []})
        with patch.object(self.network, 'inspect', return_value={'status': 'open'}), \
                patch.object(self.network.adapter, 'aws', side_effect=aws), \
                patch.object(self.network, '_existing', return_value=[]), \
                patch.object(self.network.database, 'inspect',
                             return_value={'group_id': DB}):
            self.assertEqual(self.network.close(PROBE)['status'], 'closed')
        self.assertEqual(calls, [['ec2', 'describe-network-interfaces'],
                                 ['ec2', 'revoke-security-group-ingress'],
                                 ['ec2', 'delete-security-group']])


if __name__ == '__main__':
    unittest.main()
