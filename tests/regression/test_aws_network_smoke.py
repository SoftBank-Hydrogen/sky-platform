import json
import unittest
from unittest.mock import patch

from deployment.aws import AwsConfigurationError, AwsSettings
from deployment.aws_network import AwsServiceNetworkProvisioner, ServiceNetworkRequest
from tests.live.smoke_aws_network import main, retire_probe


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
GROUP = 'sg-33333333'
STACK = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/sky-network-netprobe-12345678/id'


class AwsNetworkSmokeTests(unittest.TestCase):
    def setUp(self):
        request = ServiceNetworkRequest('netprobe-12345678', ACCOUNT, REGION, VPC)
        self.provisioner = AwsServiceNetworkProvisioner(request)
        self.network = {'stack_id': STACK, 'service_security_group': GROUP}

    def test_default_mode_does_not_create_or_delete(self):
        with patch('tests.live.smoke_aws_network.discover_default_network',
                   return_value={'vpc_id': VPC}), \
                patch('tests.live.smoke_aws_network.AwsServiceNetworkProvisioner.preflight',
                      return_value={'stack_name': 'sky-network-netprobe-12345678'}), \
                patch('tests.live.smoke_aws_network.AwsServiceNetworkProvisioner.create') as create, \
                patch('tests.live.smoke_aws_network.retire_probe') as retire:
            main(['--application', 'netprobe-12345678', '--account', ACCOUNT,
                  '--region', REGION])
        create.assert_not_called()
        retire.assert_not_called()

    def test_cleanup_rejects_group_in_use_before_cloudformation_mutation(self):
        def aws(args, **_kwargs):
            if args[:2] == ['ec2', 'describe-security-groups']:
                return json.dumps({'SecurityGroups': [{'GroupId': GROUP, 'IpPermissions': []}]})
            if args[:2] == ['ec2', 'describe-network-interfaces']:
                return json.dumps({'NetworkInterfaces': [{'NetworkInterfaceId': 'eni-12345678'}]})
            raise AssertionError('CloudFormation mutation must not run')
        with patch.object(self.provisioner, 'inspect_current', return_value=self.network), \
                patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            with self.assertRaisesRegex(AwsConfigurationError, '네트워크 인터페이스'):
                retire_probe(self.provisioner, STACK, GROUP)

    def test_cleanup_rejects_stack_id_drift_before_any_aws_command(self):
        with patch.object(self.provisioner, 'inspect_current', return_value=self.network), \
                patch.object(self.provisioner.adapter, 'aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '생성 기록과 다릅니다'):
                retire_probe(self.provisioner, STACK + '-other', GROUP)
        aws.assert_not_called()

    def test_cleanup_checks_references_then_deletes_only_owned_probe_stack(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['ec2', 'describe-security-groups'] and '--group-ids' in args:
                return json.dumps({'SecurityGroups': [{'GroupId': GROUP, 'IpPermissions': []}]})
            if args[:2] == ['ec2', 'describe-network-interfaces']:
                return json.dumps({'NetworkInterfaces': []})
            if args[:2] == ['ec2', 'describe-security-groups']:
                return json.dumps({'SecurityGroups': [{'IpPermissions': [], 'IpPermissionsEgress': []}]})
            return '{}'
        with patch.object(self.provisioner, 'inspect_current', return_value=self.network), \
                patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            retire_probe(self.provisioner, STACK, GROUP)
        self.assertEqual([call[:2] for call in calls[-3:]], [
            ['cloudformation', 'update-termination-protection'],
            ['cloudformation', 'delete-stack'], ['cloudformation', 'wait']])
        self.assertTrue(all(STACK in call for call in calls[-3:]))


if __name__ == '__main__':
    unittest.main()
