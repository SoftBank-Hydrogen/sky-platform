"""Opt-in AWS network-stack smoke: create, verify app selection, then retire the probe."""
from __future__ import annotations

import argparse
import json
import re
import secrets

from sky_platform.deployment.aws import AwsConfigurationError, AwsSettings
from sky_platform.deployment.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, discover_default_network)
from sky_platform.database.postgres import postgres_settings_for_application


def retire_probe(provisioner: AwsServiceNetworkProvisioner, expected_stack: str,
                 expected_group: str) -> None:
    """Delete only the just-created, unused probe stack after a fresh ownership audit."""
    network = provisioner.inspect_current()
    if (network['stack_id'] != expected_stack
            or network['service_security_group'] != expected_group):
        raise AwsConfigurationError('검증용 네트워크 스택 ID 또는 보안 그룹이 생성 기록과 다릅니다.')
    adapter = provisioner.adapter
    group = json.loads(adapter.aws(['ec2', 'describe-security-groups', '--group-ids',
                                    expected_group], private=True, quiet=True))['SecurityGroups'][0]
    if group.get('GroupId') != expected_group or group.get('IpPermissions') != []:
        raise AwsConfigurationError('검증용 보안 그룹에 인바운드 규칙이 생겨 자동 정리하지 않습니다.')
    interfaces = json.loads(adapter.aws(['ec2', 'describe-network-interfaces', '--filters',
        'Name=group-id,Values=' + expected_group], private=True, quiet=True))
    if interfaces.get('NextToken') or interfaces.get('NetworkInterfaces') != []:
        raise AwsConfigurationError('검증용 보안 그룹을 사용하는 네트워크 인터페이스가 있어 정리하지 않습니다.')
    groups = json.loads(adapter.aws(['ec2', 'describe-security-groups', '--filters',
        'Name=vpc-id,Values=' + provisioner.request.vpc_id], private=True, quiet=True))
    if groups.get('NextToken') or not isinstance(groups.get('SecurityGroups'), list):
        raise AwsConfigurationError('보안 그룹 참조 목록을 완전히 확인하지 못해 정리하지 않습니다.')
    for item in groups['SecurityGroups']:
        if any(pair.get('GroupId') == expected_group
               for rule in item.get('IpPermissions', []) + item.get('IpPermissionsEgress', [])
               for pair in rule.get('UserIdGroupPairs', [])):
            raise AwsConfigurationError('다른 보안 그룹에서 검증용 그룹을 참조해 정리하지 않습니다.')
    adapter.aws(['cloudformation', 'update-termination-protection',
                 '--no-enable-termination-protection', '--stack-name', expected_stack],
                private=True, quiet=True)
    adapter.aws(['cloudformation', 'delete-stack', '--stack-name', expected_stack],
                private=True, quiet=True)
    adapter.aws(['cloudformation', 'wait', 'stack-delete-complete',
                 '--stack-name', expected_stack], timeout=900, private=True, quiet=True)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Verify app-owned AWS network creation')
    parser.add_argument('--apply', action='store_true', help='Create and retire a temporary security-group stack')
    parser.add_argument('--application', default='netprobe-' + secrets.token_hex(4))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'netprobe-[a-f0-9]{8}', args.application):
        parser.error('--application must be a unique netprobe-<8 hex> ID')
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    discovered = discover_default_network(settings)
    request = ServiceNetworkRequest(args.application, args.account, args.region, discovered['vpc_id'])
    provisioner = AwsServiceNetworkProvisioner(request)
    plan = provisioner.preflight()
    print('Read-only network preflight:', json.dumps(plan, ensure_ascii=False), flush=True)
    if not args.apply:
        print('읽기 전용 검증 완료. --apply 없이는 스택을 생성하지 않습니다.', flush=True)
        return
    created = None
    try:
        created = provisioner.create()
        selected = postgres_settings_for_application(args.application, request.vpc_id, settings)
        if selected.service_security_group != created['service_security_group']:
            raise AssertionError('서버 앱별 네트워크 선택 결과가 생성된 그룹과 다릅니다.')
        print('PASS: created stack and selected its verified app-owned group', flush=True)
    finally:
        if created is None:
            print('스택 생성 결과가 불확실해 자동 정리하지 않았습니다. AWS 상태를 확인하세요:',
                  request.stack_name, flush=True)
        else:
            try:
                retire_probe(provisioner, created['stack_id'],
                             created['service_security_group'])
                print('PASS: temporary network stack and security group retired', flush=True)
            except Exception as exc:
                print('검증용 스택의 자동 정리를 확정하지 못했습니다. AWS 상태를 확인하세요:',
                      request.stack_name, str(exc), flush=True)
                raise


if __name__ == '__main__':
    main()
