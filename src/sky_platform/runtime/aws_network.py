"""Opt-in, application-owned security group for the ECS and RDS data path."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sky_platform.runtime.aws import (AwsConfigurationError, AwsExpressAdapter, AwsSettings,
                           service_group_ingress_is_restricted)


TEMPLATE = Path(__file__).parent / 'infra' / 'aws-service-network.json'


def discover_default_network(settings: AwsSettings) -> dict:
    """Read the pinned account's default VPC and one available default subnet per AZ."""
    settings.validate()
    if not settings.expected_account:
        raise AwsConfigurationError('기본 네트워크 조회에는 AWS 계정 ID 고정이 필요합니다.')
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    identity = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
    if identity.get('Account') != settings.expected_account:
        raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
    described = json.loads(adapter.aws(['ec2', 'describe-vpcs', '--filters',
        'Name=isDefault,Values=true'], private=True, quiet=True))
    vpcs = described.get('Vpcs', [])
    vpc = vpcs[0] if isinstance(vpcs, list) and len(vpcs) == 1 and isinstance(vpcs[0], dict) else {}
    vpc_id = vpc.get('VpcId', '')
    if (described.get('NextToken') or not vpc.get('IsDefault')
            or not isinstance(vpc_id, str)
            or not re.fullmatch(r'vpc-[a-f0-9]{8,17}', vpc_id)):
        raise AwsConfigurationError('기본 VPC를 하나로 확인하지 못했습니다.')
    described = json.loads(adapter.aws(['ec2', 'describe-subnets', '--filters',
        'Name=vpc-id,Values=' + vpc_id], private=True, quiet=True))
    if described.get('NextToken') or not isinstance(described.get('Subnets'), list):
        raise AwsConfigurationError('기본 VPC의 서브넷 목록을 완전히 확인하지 못했습니다.')
    by_az: dict[str, str] = {}
    for subnet in described['Subnets']:
        if not isinstance(subnet, dict):
            raise AwsConfigurationError('서브넷 조회 결과가 올바르지 않습니다.')
        if (subnet.get('VpcId') != vpc_id or subnet.get('State') != 'available'
                or not subnet.get('DefaultForAz')):
            continue
        zone, subnet_id = subnet.get('AvailabilityZone'), subnet.get('SubnetId')
        if (not isinstance(zone, str) or not zone
                or not isinstance(subnet_id, str)
                or not re.fullmatch(r'subnet-[a-f0-9]{8,17}', subnet_id)):
            raise AwsConfigurationError('기본 서브넷의 가용 영역이나 ID가 올바르지 않습니다.')
        if zone in by_az:
            raise AwsConfigurationError('같은 가용 영역에 기본 서브넷이 여러 개 있습니다.')
        by_az[zone] = subnet_id
    if len(by_az) < 2:
        raise AwsConfigurationError('서로 다른 가용 영역의 사용 가능한 기본 서브넷이 두 개 이상 필요합니다.')
    return {'account': settings.expected_account, 'region': settings.region,
            'vpc_id': vpc_id, 'subnet_ids': [by_az[zone] for zone in sorted(by_az)[:8]],
            'availability_zones': sorted(by_az)[:8]}


@dataclass(frozen=True)
class ServiceNetworkRequest:
    application_id: str
    account: str
    region: str
    vpc_id: str

    @property
    def stack_name(self) -> str:
        return 'sky-network-' + self.application_id

    def validate(self) -> None:
        if (not 3 <= len(self.application_id) <= 31
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', self.application_id)):
            raise ValueError('앱 ID는 3~31자리 소문자·숫자와 내부 하이픈이어야 합니다.')
        if not re.fullmatch(r'\d{12}', self.account):
            raise ValueError('AWS 계정 ID는 12자리 숫자여야 합니다.')
        if not re.fullmatch(r'vpc-[a-f0-9]{8,17}', self.vpc_id):
            raise ValueError('VPC ID가 올바르지 않습니다.')
        AwsSettings(self.region, expected_account=self.account,
                    account_pin_required=True).validate()


class AwsServiceNetworkProvisioner:
    def __init__(self, request: ServiceNetworkRequest):
        request.validate()
        self.request = request
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(
            request.region, expected_account=request.account, account_pin_required=True))

    def preflight(self) -> dict:
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'],
                                              private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        vpcs = json.loads(self.adapter.aws(['ec2', 'describe-vpcs', '--vpc-ids', req.vpc_id],
                                          private=True, quiet=True)).get('Vpcs', [])
        if len(vpcs) != 1 or vpcs[0].get('VpcId') != req.vpc_id or not vpcs[0].get('IsDefault'):
            raise AwsConfigurationError('ECS Express와 같은 기본 VPC가 필요합니다.')
        listed = json.loads(self.adapter.aws(['cloudformation', 'list-stacks'],
                                            private=True, quiet=True))
        stacks = listed.get('StackSummaries')
        if (not isinstance(stacks, list) or listed.get('NextToken')
                or any(not isinstance(item, dict) for item in stacks)):
            raise AwsConfigurationError('CloudFormation 스택 목록을 완전히 확인하지 못했습니다.')
        if any(item.get('StackName') == req.stack_name for item in stacks):
            raise AwsConfigurationError('이 앱의 서비스 네트워크 스택 기록이 이미 있습니다. --inspect를 사용하세요.')
        return {'application_id': req.application_id, 'account': req.account,
                'region': req.region, 'vpc_id': req.vpc_id,
                'stack_name': req.stack_name, 'initial_ingress': []}

    def create(self) -> dict:
        plan = self.preflight()
        req = self.request
        payload = {'StackName': req.stack_name, 'TemplateBody': TEMPLATE.read_text(),
                   'EnableTerminationProtection': True,
                   'Parameters': [
                       {'ParameterKey': 'ApplicationId', 'ParameterValue': req.application_id},
                       {'ParameterKey': 'VpcId', 'ParameterValue': req.vpc_id}],
                   'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                            {'Key': 'sky-app', 'Value': req.application_id}]}
        with tempfile.NamedTemporaryFile('w', suffix='.json', prefix='sky-network-') as file:
            json.dump(payload, file)
            file.flush()
            created = json.loads(self.adapter.aws(['cloudformation', 'create-stack',
                '--cli-input-json', 'file://' + file.name], timeout=60, private=True))
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        stack_id = created.get('StackId', '')
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('서비스 네트워크 생성 결과를 확인하지 못했습니다. 스택 상태를 확인하세요.')
        try:
            self.adapter.aws(['cloudformation', 'wait', 'stack-create-complete',
                              '--stack-name', stack_id], timeout=900, private=True, quiet=True)
            return self.inspect(stack_id)
        except Exception as exc:
            raise RuntimeError(f'서비스 네트워크 생성 결과가 불확실합니다. {stack_id} 상태를 확인하세요: {exc}') from None

    def inspect_current(self) -> dict:
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'],
                                              private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks',
            '--stack-name', req.stack_name], private=True, quiet=True)).get('Stacks', [])
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        stack = stacks[0] if len(stacks) == 1 else {}
        stack_id = stack.get('StackId', '')
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('앱 전용 서비스 네트워크 스택을 확인하지 못했습니다.')
        return self.inspect(stack_id)

    def inspect(self, stack_id: str) -> dict:
        req = self.request
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('서비스 네트워크 스택 ARN이 예상과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks',
            '--stack-name', stack_id], private=True, quiet=True)).get('Stacks', [])
        stack = stacks[0] if len(stacks) == 1 else {}
        tags = {item.get('Key'): item.get('Value') for item in stack.get('Tags', [])}
        outputs = {item.get('OutputKey'): item.get('OutputValue') for item in stack.get('Outputs', [])}
        group_id = outputs.get('ServiceSecurityGroupId', '')
        if (stack.get('StackId') != stack_id or stack.get('StackStatus') != 'CREATE_COMPLETE'
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != req.application_id
                or outputs.get('VpcId') != req.vpc_id
                or not isinstance(group_id, str)
                or not re.fullmatch(r'sg-[a-f0-9]{8,17}', group_id)):
            raise AwsConfigurationError('서비스 네트워크 스택 상태·출력·소유권이 예상과 다릅니다.')
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups',
            '--group-ids', group_id], private=True, quiet=True)).get('SecurityGroups', [])
        group = groups[0] if len(groups) == 1 else {}
        group_tags = {item.get('Key'): item.get('Value') for item in group.get('Tags', [])}
        if (group.get('GroupId') != group_id or group.get('OwnerId') != req.account
                or group.get('VpcId') != req.vpc_id
                or group_tags.get('sky-managed') != 'true'
                or group_tags.get('sky-app') != req.application_id
                or not service_group_ingress_is_restricted(group)):
            raise AwsConfigurationError('서비스 보안 그룹의 소유권·VPC·인바운드 규칙이 예상과 다릅니다.')
        return {'application_id': req.application_id, 'account': req.account,
                'region': req.region, 'vpc_id': req.vpc_id,
                'stack_id': stack_id, 'service_security_group': group_id,
                'status': 'available'}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Create an application-owned ECS security group')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create the CloudFormation stack')
    mode.add_argument('--inspect', action='store_true', help='Read and verify an existing stack')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    args = parser.parse_args(argv)
    request = ServiceNetworkRequest(args.application, args.account, args.region, args.vpc_id)
    provisioner = AwsServiceNetworkProvisioner(request)
    result = (provisioner.create() if args.apply else
              provisioner.inspect_current() if args.inspect else provisioner.preflight())
    if not args.apply and not args.inspect:
        print('읽기 전용 확인 완료. --apply 없이는 서비스 보안 그룹을 생성하지 않습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
