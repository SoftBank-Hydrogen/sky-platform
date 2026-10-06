"""Owned, ingress-free security group for an isolated RDS restore drill."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings


@dataclass(frozen=True)
class RestoreNetworkRequest:
    application_id: str
    target_id: str
    account: str
    region: str
    vpc_id: str

    @property
    def group_name(self) -> str:
        return self.target_id + '-db'

    def validate(self) -> None:
        if (not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', self.application_id)
                or not 3 <= len(self.application_id) <= 31
                or not self.target_id.startswith('sky-restore-' + self.application_id + '-')
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', self.target_id)
                or len(self.target_id) > 63
                or not re.fullmatch(r'vpc-[a-f0-9]{8,17}', self.vpc_id)):
            raise ValueError('복원 앱·대상 DB·VPC ID가 올바르지 않습니다.')
        AwsSettings(self.region, expected_account=self.account,
                    account_pin_required=True).validate()


class RestoreSecurityGroup:
    def __init__(self, request: RestoreNetworkRequest):
        request.validate()
        self.request = request
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(
            request.region, expected_account=request.account, account_pin_required=True))

    def _account(self) -> None:
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'],
                                               private=True, quiet=True))
        if identity.get('Account') != self.request.account:
            raise AwsConfigurationError('현재 AWS 계정이 복원 계획의 계정과 다릅니다.')

    def _matching_groups(self) -> list[dict]:
        req = self.request
        result = json.loads(self.adapter.aws(['ec2', 'describe-security-groups', '--filters',
            'Name=group-name,Values=' + req.group_name,
            'Name=vpc-id,Values=' + req.vpc_id], private=True, quiet=True))
        groups = result.get('SecurityGroups')
        if result.get('NextToken') or not isinstance(groups, list) or any(
                not isinstance(group, dict) for group in groups):
            raise AwsConfigurationError('복원 보안 그룹 목록을 완전히 확인하지 못했습니다.')
        return groups

    def preflight(self) -> dict:
        req = self.request
        self._account()
        vpcs = json.loads(self.adapter.aws(['ec2', 'describe-vpcs',
            '--vpc-ids', req.vpc_id], private=True, quiet=True)).get('Vpcs')
        if (not isinstance(vpcs, list) or len(vpcs) != 1
                or vpcs[0].get('VpcId') != req.vpc_id):
            raise AwsConfigurationError('복원 대상 VPC를 확인하지 못했습니다.')
        if self._matching_groups():
            raise AwsConfigurationError('같은 이름의 복원 보안 그룹이 이미 있습니다. --inspect를 사용하세요.')
        return {'application_id': req.application_id, 'target_id': req.target_id,
                'account': req.account, 'region': req.region, 'vpc_id': req.vpc_id,
                'group_name': req.group_name, 'ingress': [], 'egress': []}

    def inspect(self) -> dict:
        req = self.request
        self._account()
        groups = self._matching_groups()
        group = groups[0] if len(groups) == 1 else {}
        tags = {item.get('Key'): item.get('Value') for item in group.get('Tags', [])
                if isinstance(item, dict)}
        group_id = group.get('GroupId')
        if (not isinstance(group_id, str)
                or not re.fullmatch(r'sg-[a-f0-9]{8,17}', group_id)
                or group.get('OwnerId') != req.account
                or group.get('VpcId') != req.vpc_id
                or group.get('GroupName') != req.group_name
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != req.application_id
                or tags.get('sky-restore-target') != req.target_id
                or group.get('IpPermissions') != []
                or group.get('IpPermissionsEgress') != []):
            raise AwsConfigurationError('복원 보안 그룹의 소유권·규칙이 예상과 다릅니다.')
        return {'application_id': req.application_id, 'target_id': req.target_id,
                'account': req.account, 'region': req.region, 'vpc_id': req.vpc_id,
                'group_name': req.group_name, 'group_id': group_id,
                'ingress': [], 'egress': []}

    def create(self) -> dict:
        self.preflight()
        req = self.request
        tags = [{'Key': 'sky-managed', 'Value': 'true'},
                {'Key': 'sky-app', 'Value': req.application_id},
                {'Key': 'sky-restore-target', 'Value': req.target_id}]
        created = json.loads(self.adapter.aws(['ec2', 'create-security-group',
            '--group-name', req.group_name,
            '--description', 'Isolated Sky RDS restore drill',
            '--vpc-id', req.vpc_id,
            '--tag-specifications', json.dumps([{'ResourceType': 'security-group',
                                                  'Tags': tags}])], private=True, quiet=True))
        group_id = created.get('GroupId')
        if not isinstance(group_id, str) or not re.fullmatch(r'sg-[a-f0-9]{8,17}', group_id):
            raise AwsConfigurationError('복원 보안 그룹 생성 결과가 불확실합니다. 이름으로 재조회하세요.')
        # EC2 adds outbound allow-all rules by default. Remove exactly the rules observed.
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups',
            '--group-ids', group_id], private=True, quiet=True)).get('SecurityGroups')
        group = groups[0] if isinstance(groups, list) and len(groups) == 1 else {}
        observed_tags = {item.get('Key'): item.get('Value') for item in group.get('Tags', [])
                         if isinstance(item, dict)}
        if (group.get('GroupId') != group_id or group.get('VpcId') != req.vpc_id
                or group.get('OwnerId') != req.account
                or group.get('GroupName') != req.group_name
                or any(observed_tags.get(tag['Key']) != tag['Value'] for tag in tags)
                or group.get('IpPermissions') != []
                or not isinstance(group.get('IpPermissionsEgress'), list)):
            raise AwsConfigurationError('생성 직후 복원 보안 그룹을 확인하지 못했습니다.')
        egress = group['IpPermissionsEgress']
        if egress:
            self.adapter.aws(['ec2', 'revoke-security-group-egress', '--group-id', group_id,
                              '--ip-permissions', json.dumps(egress)], private=True, quiet=True)
        inspected = self.inspect()
        if inspected['group_id'] != group_id:
            raise AwsConfigurationError('복원 보안 그룹 ID가 생성 결과와 다릅니다.')
        return inspected

    def delete(self, expected_group_id: str) -> dict:
        current = self.inspect()
        if current['group_id'] != expected_group_id:
            raise AwsConfigurationError('정리 대상 보안 그룹 ID가 생성 기록과 다릅니다.')
        req = self.request
        interfaces = json.loads(self.adapter.aws(['ec2', 'describe-network-interfaces',
            '--filters', 'Name=group-id,Values=' + expected_group_id],
            private=True, quiet=True))
        if interfaces.get('NextToken') or interfaces.get('NetworkInterfaces') != []:
            raise AwsConfigurationError('보안 그룹을 사용하는 네트워크 인터페이스가 있어 정리하지 않습니다.')
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups',
            '--filters', 'Name=vpc-id,Values=' + req.vpc_id], private=True, quiet=True))
        if groups.get('NextToken') or not isinstance(groups.get('SecurityGroups'), list):
            raise AwsConfigurationError('보안 그룹 참조 목록을 완전히 확인하지 못했습니다.')
        for group in groups['SecurityGroups']:
            if not isinstance(group, dict):
                raise AwsConfigurationError('보안 그룹 참조 목록이 올바르지 않습니다.')
            for rule in group.get('IpPermissions', []) + group.get('IpPermissionsEgress', []):
                if any(pair.get('GroupId') == expected_group_id
                       for pair in rule.get('UserIdGroupPairs', [])):
                    raise AwsConfigurationError('다른 보안 그룹에서 복원 그룹을 참조해 정리하지 않습니다.')
        self.adapter.aws(['ec2', 'delete-security-group', '--group-id', expected_group_id],
                         private=True, quiet=True)
        if self._matching_groups():
            raise AwsConfigurationError('보안 그룹 삭제를 확인하지 못했습니다.')
        return {'group_id': expected_group_id, 'status': 'deleted'}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Prepare an isolated RDS restore security group')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create an owned security group')
    mode.add_argument('--inspect', action='store_true', help='Verify an existing group')
    mode.add_argument('--delete', metavar='GROUP_ID', help='Delete the verified unused group')
    parser.add_argument('--application', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    args = parser.parse_args(argv)
    request = RestoreNetworkRequest(args.application, args.target_id,
                                    args.account, args.region, args.vpc_id)
    manager = RestoreSecurityGroup(request)
    result = (manager.create() if args.apply else manager.inspect() if args.inspect else
              manager.delete(args.delete) if args.delete else manager.preflight())
    if not args.apply and not args.inspect and not args.delete:
        print('읽기 전용 확인 완료. --apply 없이는 보안 그룹을 생성하지 않습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
