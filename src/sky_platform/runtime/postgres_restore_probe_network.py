"""Temporary, narrowly scoped ECS-to-restored-PostgreSQL network access."""
from __future__ import annotations

import argparse
import json
import re

from sky_platform.runtime.aws import AwsConfigurationError
from sky_platform.runtime.postgres_restore_network import RestoreNetworkRequest, RestoreSecurityGroup


class RestoreProbeNetwork:
    def __init__(self, request: RestoreNetworkRequest, db_group_id: str):
        self.database = RestoreSecurityGroup(request)
        if not re.fullmatch(r'sg-[a-f0-9]{8,17}', db_group_id):
            raise ValueError('복원 DB 보안 그룹 ID가 올바르지 않습니다.')
        self.db_group_id = db_group_id
        self.adapter = self.database.adapter
        self.name = request.target_id + '-probe'

    def _group(self, group_id: str) -> dict:
        response = json.loads(self.adapter.aws(['ec2', 'describe-security-groups',
            '--group-ids', group_id], private=True, quiet=True))
        groups = response.get('SecurityGroups')
        if (response.get('NextToken') or not isinstance(groups, list)
                or len(groups) != 1 or groups[0].get('GroupId') != group_id):
            raise AwsConfigurationError('검사용 보안 그룹을 하나로 확인하지 못했습니다.')
        return groups[0]

    def _probe(self, probe_id: str) -> dict:
        req = self.database.request
        group = self._group(probe_id)
        tags = {item.get('Key'): item.get('Value') for item in group.get('Tags', [])
                if isinstance(item, dict)}
        if (group.get('OwnerId') != req.account or group.get('VpcId') != req.vpc_id
                or group.get('GroupName') != self.name
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != req.application_id
                or tags.get('sky-restore-target') != req.target_id
                or tags.get('sky-probe') != 'true'):
            raise AwsConfigurationError('검사용 보안 그룹 소유권이 예상과 다릅니다.')
        return group

    def _existing(self) -> list[dict]:
        req = self.database.request
        response = json.loads(self.adapter.aws(['ec2', 'describe-security-groups',
            '--filters', 'Name=group-name,Values=' + self.name,
            'Name=vpc-id,Values=' + req.vpc_id], private=True, quiet=True))
        groups = response.get('SecurityGroups')
        if response.get('NextToken') or not isinstance(groups, list):
            raise AwsConfigurationError('검사용 보안 그룹 목록을 완전히 확인하지 못했습니다.')
        return groups

    @staticmethod
    def _rule(peer_id: str) -> dict:
        return {'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432,
                'UserIdGroupPairs': [{'GroupId': peer_id}]}

    @staticmethod
    def _https() -> dict:
        return {'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443,
                'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}

    @staticmethod
    def _matches(actual: dict, expected: dict) -> bool:
        pairs = actual.get('UserIdGroupPairs', [])
        ranges = actual.get('IpRanges', [])
        expected_pairs = expected.get('UserIdGroupPairs', [])
        expected_ranges = expected.get('IpRanges', [])
        return (actual.get('IpProtocol') == expected['IpProtocol']
                and actual.get('FromPort') == expected['FromPort']
                and actual.get('ToPort') == expected['ToPort']
                and [item.get('GroupId') for item in pairs] ==
                    [item['GroupId'] for item in expected_pairs]
                and [item.get('CidrIp') for item in ranges] ==
                    [item['CidrIp'] for item in expected_ranges]
                and actual.get('Ipv6Ranges', []) == []
                and actual.get('PrefixListIds', []) == [])

    def preflight(self) -> dict:
        if self.database.inspect()['group_id'] != self.db_group_id:
            raise AwsConfigurationError('복원 DB 그룹 ID가 다릅니다.')
        if self._existing():
            raise AwsConfigurationError('검사용 그룹 이름이 이미 사용 중입니다.')
        return {'db_group_id': self.db_group_id, 'probe_group_name': self.name,
                'database_port': 5432, 'probe_https_egress': True}

    def inspect(self, probe_id: str) -> dict:
        self.database._account()
        db = self.database._matching_groups()
        req = self.database.request
        db_tags = {item.get('Key'): item.get('Value') for item in db[0].get('Tags', [])
                   if isinstance(item, dict)} if len(db) == 1 else {}
        if (len(db) != 1 or db[0].get('GroupId') != self.db_group_id
                or db[0].get('OwnerId') != req.account
                or db[0].get('VpcId') != req.vpc_id
                or db[0].get('GroupName') != req.group_name
                or db_tags.get('sky-managed') != 'true'
                or db_tags.get('sky-app') != req.application_id
                or db_tags.get('sky-restore-target') != req.target_id):
            raise AwsConfigurationError('복원 DB 그룹 ID가 다릅니다.')
        probe = self._probe(probe_id)
        if (len(db[0].get('IpPermissions', [])) != 1
                or not self._matches(db[0]['IpPermissions'][0], self._rule(probe_id))
                or db[0].get('IpPermissionsEgress') != []
                or probe.get('IpPermissions') != []
                or len(probe.get('IpPermissionsEgress', [])) != 2
                or not any(self._matches(item, self._rule(self.db_group_id))
                           for item in probe['IpPermissionsEgress'])
                or not any(self._matches(item, self._https())
                           for item in probe['IpPermissionsEgress'])):
            raise AwsConfigurationError('복원 DB 검사 연결 규칙이 예상과 다릅니다.')
        return {'db_group_id': self.db_group_id, 'probe_group_id': probe_id,
                'status': 'open', 'database_port': 5432}

    def open(self) -> dict:
        self.preflight()
        req = self.database.request
        tags = [{'Key': 'sky-managed', 'Value': 'true'},
                {'Key': 'sky-app', 'Value': req.application_id},
                {'Key': 'sky-restore-target', 'Value': req.target_id},
                {'Key': 'sky-probe', 'Value': 'true'}]
        created = json.loads(self.adapter.aws(['ec2', 'create-security-group',
            '--group-name', self.name,
            '--description', 'Temporary Sky RDS restore verification',
            '--vpc-id', req.vpc_id,
            '--tag-specifications', json.dumps([{'ResourceType': 'security-group',
                                                  'Tags': tags}])], private=True, quiet=True))
        probe_id = created.get('GroupId')
        if not isinstance(probe_id, str) or not re.fullmatch(r'sg-[a-f0-9]{8,17}', probe_id):
            raise AwsConfigurationError('검사용 그룹 생성 결과가 불확실합니다. 이름으로 재조회하세요.')
        group = self._probe(probe_id)
        if group.get('IpPermissions') != [] or not isinstance(group.get('IpPermissionsEgress'), list):
            raise AwsConfigurationError('생성 직후 검사용 그룹 규칙을 확인하지 못했습니다.')
        if group['IpPermissionsEgress']:
            self.adapter.aws(['ec2', 'revoke-security-group-egress', '--group-id', probe_id,
                              '--ip-permissions', json.dumps(group['IpPermissionsEgress'])],
                             private=True, quiet=True)
        self.adapter.aws(['ec2', 'authorize-security-group-egress', '--group-id', probe_id,
                          '--ip-permissions', json.dumps([self._rule(self.db_group_id),
                                                          self._https()])], private=True, quiet=True)
        self.adapter.aws(['ec2', 'authorize-security-group-ingress', '--group-id', self.db_group_id,
                          '--ip-permissions', json.dumps([self._rule(probe_id)])],
                         private=True, quiet=True)
        return self.inspect(probe_id)

    def close(self, probe_id: str) -> dict:
        self.inspect(probe_id)
        interfaces = json.loads(self.adapter.aws(['ec2', 'describe-network-interfaces',
            '--filters', 'Name=group-id,Values=' + probe_id], private=True, quiet=True))
        if interfaces.get('NextToken') or interfaces.get('NetworkInterfaces') != []:
            raise AwsConfigurationError('검사용 그룹을 사용하는 태스크가 있어 연결을 닫지 않습니다.')
        self.adapter.aws(['ec2', 'revoke-security-group-ingress', '--group-id', self.db_group_id,
                          '--ip-permissions', json.dumps([self._rule(probe_id)])],
                         private=True, quiet=True)
        self.adapter.aws(['ec2', 'delete-security-group', '--group-id', probe_id],
                         private=True, quiet=True)
        if self._existing():
            raise AwsConfigurationError('검사용 그룹 삭제를 확인하지 못했습니다.')
        self.database.inspect()
        return {'db_group_id': self.db_group_id, 'probe_group_id': probe_id,
                'status': 'closed'}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Temporary ECS access to an isolated restore DB')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--inspect', metavar='PROBE_GROUP_ID')
    mode.add_argument('--close', metavar='PROBE_GROUP_ID')
    parser.add_argument('--application', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--db-group-id', required=True)
    args = parser.parse_args(argv)
    network = RestoreProbeNetwork(RestoreNetworkRequest(
        args.application, args.target_id, args.account, args.region, args.vpc_id),
        args.db_group_id)
    result = (network.open() if args.apply else network.inspect(args.inspect) if args.inspect
              else network.close(args.close) if args.close else network.preflight())
    if not (args.apply or args.inspect or args.close):
        print('읽기 전용 확인 완료. --apply 없이는 검사 연결을 열지 않습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
