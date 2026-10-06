import json
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, TEMPLATE, discover_default_network, main)

ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
GROUP = 'sg-33333333'
STACK = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/sky-network-demo-app/stack-id'


class AwsNetworkTests(unittest.TestCase):
    def setUp(self):
        self.request = ServiceNetworkRequest('demo-app', ACCOUNT, REGION, VPC)
        self.provisioner = AwsServiceNetworkProvisioner(self.request)

    def test_template_has_app_ownership_and_no_inbound_rule(self):
        template = json.loads(TEMPLATE.read_text())
        group = template['Resources']['ServiceSecurityGroup']['Properties']
        self.assertNotIn('SecurityGroupIngress', group)
        self.assertEqual(group['VpcId'], {'Ref': 'VpcId'})
        self.assertIn({'Key': 'sky-app', 'Value': {'Ref': 'ApplicationId'}},
                      group['Tags'])

    def test_preflight_checks_account_default_vpc_and_existing_stack(self):
        def aws(args, **_kwargs):
            if args[0] == 'sts':
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]})
            return json.dumps({'StackSummaries': []})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.provisioner.preflight()['stack_name'],
                             'sky-network-demo-app')
        def existing(args, **kwargs):
            if args[0] == 'cloudformation':
                return json.dumps({'StackSummaries': [{'StackName': self.request.stack_name}]})
            return aws(args, **kwargs)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=existing):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.provisioner.preflight()

    def test_create_checks_owned_output_after_stack_wait(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['cloudformation', 'create-stack']:
                file = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                payload = json.loads(file.read_text())
                self.assertTrue(payload['EnableTerminationProtection'])
                self.assertEqual(payload['StackName'], self.request.stack_name)
                return json.dumps({'StackId': STACK})
            return ''
        with patch.object(self.provisioner, 'preflight', return_value={'account': ACCOUNT}), \
                patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'inspect', return_value={'service_security_group': GROUP}) as inspect:
            self.assertEqual(self.provisioner.create(), {'service_security_group': GROUP})
        self.assertEqual(calls, [['cloudformation', 'create-stack'],
                                 ['cloudformation', 'wait']])
        inspect.assert_called_once_with(STACK)

    def test_inspect_rejects_unowned_group(self):
        stack = {'StackId': STACK, 'StackStatus': 'CREATE_COMPLETE',
                 'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                          {'Key': 'sky-app', 'Value': 'demo-app'}],
                 'Outputs': [{'OutputKey': 'VpcId', 'OutputValue': VPC},
                             {'OutputKey': 'ServiceSecurityGroupId', 'OutputValue': GROUP}]}
        group = {'GroupId': GROUP, 'OwnerId': ACCOUNT, 'VpcId': VPC,
                 'IpPermissions': [], 'Tags': [
                     {'Key': 'sky-managed', 'Value': 'true'},
                     {'Key': 'sky-app', 'Value': 'demo-app'}]}
        def aws(args, **_kwargs):
            return json.dumps({'Stacks': [stack]} if args[0] == 'cloudformation'
                              else {'SecurityGroups': [group]})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.provisioner.inspect(STACK)['service_security_group'], GROUP)
            group['Tags'][-1]['Value'] = 'other-app'
            with self.assertRaisesRegex(AwsConfigurationError, '소유권'):
                self.provisioner.inspect(STACK)

    def test_default_cli_mode_never_creates_group(self):
        args = ['--application', 'demo-app', '--account', ACCOUNT,
                '--region', REGION, '--vpc-id', VPC]
        with patch('adapters.aws.network.AwsServiceNetworkProvisioner.preflight',
                   return_value={'account': ACCOUNT}) as preflight, \
                patch('adapters.aws.network.AwsServiceNetworkProvisioner.create') as create:
            main(args)
        preflight.assert_called_once_with()
        create.assert_not_called()

    def test_default_network_discovery_returns_one_available_default_subnet_per_zone(self):
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            if args[0] == 'sts':
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]})
            return json.dumps({'Subnets': [
                {'SubnetId': 'subnet-11111111', 'VpcId': VPC, 'State': 'available',
                 'DefaultForAz': True, 'AvailabilityZone': 'ap-northeast-2a'},
                {'SubnetId': 'subnet-22222222', 'VpcId': VPC, 'State': 'available',
                 'DefaultForAz': True, 'AvailabilityZone': 'ap-northeast-2c'},
                {'SubnetId': 'subnet-33333333', 'VpcId': VPC, 'State': 'available',
                 'DefaultForAz': False, 'AvailabilityZone': 'ap-northeast-2d'}]})
        settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('adapters.aws.network.AwsExpressAdapter.aws', autospec=True,
                   side_effect=aws):
            result = discover_default_network(settings)
        self.assertEqual(result['vpc_id'], VPC)
        self.assertEqual(result['subnet_ids'], ['subnet-11111111', 'subnet-22222222'])
        self.assertEqual(calls, [['sts', 'get-caller-identity'],
                                 ['ec2', 'describe-vpcs'], ['ec2', 'describe-subnets']])

    def test_default_network_discovery_checks_account_before_ec2(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('adapters.aws.network.AwsExpressAdapter.aws',
                   return_value=json.dumps({'Account': '999999999999'})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '계정'):
                discover_default_network(settings)
        self.assertEqual(aws.call_count, 1)

    def test_default_network_discovery_requires_two_available_zones(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT)
        with patch('adapters.aws.network.AwsExpressAdapter.aws', side_effect=[
                json.dumps({'Account': ACCOUNT}),
                json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]}),
                json.dumps({'Subnets': [{'SubnetId': 'subnet-11111111', 'VpcId': VPC,
                    'State': 'available', 'DefaultForAz': True,
                    'AvailabilityZone': 'ap-northeast-2a'}]})]):
            with self.assertRaisesRegex(AwsConfigurationError, '두 개 이상'):
                discover_default_network(settings)


if __name__ == '__main__':
    unittest.main()
