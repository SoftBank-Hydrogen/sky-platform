"""Explicit lifecycle for an isolated, disposable RDS snapshot restore."""
from __future__ import annotations

import argparse
import json
import re

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from database.postgres_restore import plan_restore_drill
from database.postgres_restore_network import RestoreNetworkRequest, RestoreSecurityGroup
from database.postgres_restore_probe_network import RestoreProbeNetwork


class RestoreInstance:
    def __init__(self, application_id: str, snapshot_id: str, target_id: str,
                 settings: AwsSettings, vpc_id: str, group_id: str):
        self.network = RestoreSecurityGroup(RestoreNetworkRequest(
            application_id, target_id, settings.expected_account, settings.region, vpc_id))
        self.application_id = application_id
        self.snapshot_id = snapshot_id
        self.target_id = target_id
        self.settings = settings
        self.group_id = group_id
        self.adapter = AwsExpressAdapter(lambda *_: None, settings)
        self.arn = (f'arn:aws:rds:{settings.region}:{settings.expected_account}:'
                    f'db:{target_id}')

    def _network(self) -> None:
        if self.network.inspect()['group_id'] != self.group_id:
            raise AwsConfigurationError('복원 DB의 격리 보안 그룹 ID가 다릅니다.')

    def _verified(self, db: dict) -> dict:
        if not isinstance(db, dict):
            raise AwsConfigurationError('복원 DB 응답을 확인하지 못했습니다.')
        groups = db.get('VpcSecurityGroups')
        subnet = db.get('DBSubnetGroup')
        if (db.get('DBInstanceIdentifier') != self.target_id
                or db.get('DBInstanceArn') != self.arn
                or db.get('Engine') != 'postgres'
                or db.get('DBInstanceClass') != 'db.t4g.micro'
                or db.get('AllocatedStorage') != 20
                or db.get('StorageType') != 'gp3'
                or db.get('StorageEncrypted') is not True
                or db.get('PubliclyAccessible') is not False
                or db.get('MultiAZ') is not False
                or db.get('DeletionProtection') is not False
                or not isinstance(subnet, dict)
                or subnet.get('VpcId') != self.network.request.vpc_id
                or not isinstance(groups, list)
                or len(groups) != 1
                or groups[0].get('VpcSecurityGroupId') != self.group_id
                or not isinstance(db.get('DBInstanceStatus'), str)):
            raise AwsConfigurationError('복원 DB의 소유권·격리·용량 구성이 예상과 다릅니다.')
        return {'target_database_id': self.target_id, 'target_arn': self.arn,
                'snapshot_id': self.snapshot_id, 'group_id': self.group_id,
                'status': db['DBInstanceStatus'], 'publicly_accessible': False,
                'encrypted': True}

    def _tags(self) -> None:
        result = json.loads(self.adapter.aws(['rds', 'list-tags-for-resource',
            '--resource-name', self.arn], private=True, quiet=True))
        tags = result.get('TagList')
        if not isinstance(tags, list) or any(not isinstance(tag, dict) for tag in tags):
            raise AwsConfigurationError('복원 DB의 소유 태그를 확인하지 못했습니다.')
        owned = {tag.get('Key'): tag.get('Value') for tag in tags}
        if (owned.get('sky-managed') != 'true'
                or owned.get('sky-app') != self.application_id
                or owned.get('sky-restore-target') != self.target_id
                or owned.get('sky-source-snapshot') != self.snapshot_id):
            raise AwsConfigurationError('복원 DB의 소유 태그가 예상과 다릅니다.')

    def inspect(self) -> dict:
        self._network()
        return self._inspect_instance()

    def _inspect_instance(self) -> dict:
        result = json.loads(self.adapter.aws(['rds', 'describe-db-instances',
            '--db-instance-identifier', self.target_id], private=True, quiet=True))
        databases = result.get('DBInstances')
        if result.get('Marker') or not isinstance(databases, list) or len(databases) != 1:
            raise AwsConfigurationError('복원 DB를 하나로 확인하지 못했습니다.')
        inspected = self._verified(databases[0])
        if inspected['status'] == 'available':
            endpoint = databases[0].get('Endpoint', {})
            address = endpoint.get('Address') if isinstance(endpoint, dict) else None
            if (not isinstance(address, str)
                    or not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.'
                                        + re.escape(self.settings.region)
                                        + r'\.rds\.amazonaws\.com', address)
                    or endpoint.get('Port') != 5432):
                raise AwsConfigurationError('복원 DB의 비공개 엔드포인트를 확인하지 못했습니다.')
            inspected['endpoint'] = address
            inspected['port'] = 5432
        self._tags()
        return inspected

    def inspect_for_probe(self, probe_group_id: str) -> dict:
        RestoreProbeNetwork(self.network.request, self.group_id).inspect(probe_group_id)
        return self._inspect_instance()

    def preflight(self) -> dict:
        plan = plan_restore_drill(self.application_id, self.snapshot_id,
                                  self.target_id, self.settings)
        self._network()
        if plan['vpc_id'] != self.network.request.vpc_id:
            raise AwsConfigurationError('복원 계획의 VPC와 격리 그룹의 VPC가 다릅니다.')
        return {**plan, 'restore_security_group_id': self.group_id}

    def create(self) -> dict:
        plan = self.preflight()
        tags = [{'Key': 'sky-managed', 'Value': 'true'},
                {'Key': 'sky-app', 'Value': self.application_id},
                {'Key': 'sky-restore-target', 'Value': self.target_id},
                {'Key': 'sky-source-snapshot', 'Value': self.snapshot_id}]
        result = json.loads(self.adapter.aws(['rds', 'restore-db-instance-from-db-snapshot',
            '--db-instance-identifier', self.target_id,
            '--db-snapshot-identifier', self.snapshot_id,
            '--db-instance-class', plan['instance_class'],
            '--db-subnet-group-name', plan['db_subnet_group_name'],
            '--vpc-security-group-ids', self.group_id,
            '--no-publicly-accessible', '--no-multi-az', '--no-deletion-protection',
            '--backup-retention-period', '0', '--tags', json.dumps(tags)],
            private=True, quiet=True))
        created = self._verified(result.get('DBInstance'))
        if created['status'] not in {'creating', 'available'}:
            raise AwsConfigurationError('복원 요청 상태가 예상과 다릅니다. 다시 생성하지 말고 조회하세요.')
        return created

    def delete(self) -> dict:
        current = self.inspect()
        if current['status'] != 'available':
            raise AwsConfigurationError('복원 DB가 available 상태일 때만 정리합니다.')
        result = json.loads(self.adapter.aws(['rds', 'delete-db-instance',
            '--db-instance-identifier', self.target_id, '--skip-final-snapshot',
            '--delete-automated-backups'], private=True, quiet=True))
        deleted = self._verified(result.get('DBInstance'))
        if deleted['status'] != 'deleting':
            raise AwsConfigurationError('복원 DB 삭제 요청 상태를 확인하지 못했습니다.')
        return deleted


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Lifecycle for an isolated RDS restore drill')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Restore a billable DB instance')
    mode.add_argument('--inspect', action='store_true', help='Verify the restored DB')
    mode.add_argument('--delete', action='store_true', help='Delete only the owned restore DB')
    parser.add_argument('--application', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--group-id', required=True)
    parser.add_argument('--service-security-group')
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    restore = RestoreInstance(args.application, args.snapshot_id, args.target_id,
                              settings, args.vpc_id, args.group_id)
    result = (restore.create() if args.apply else restore.inspect() if args.inspect else
              restore.delete() if args.delete else restore.preflight())
    if not (args.apply or args.inspect or args.delete):
        print('읽기 전용 계획 완료. --apply 없이는 복원 DB를 생성하지 않습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
