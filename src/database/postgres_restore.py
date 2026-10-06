"""Read-only safety and cost plan for restoring an owned RDS snapshot to a separate instance."""
from __future__ import annotations

import argparse
import json
import re

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from deployment.aws_pricing import estimate_postgres_base_capacity
from database.postgres import discover_existing_postgres
from database.postgres_snapshot import inspect_snapshot


def plan_restore_drill(application_id: str, snapshot_id: str, target_id: str,
                       settings: AwsSettings) -> dict:
    prefix = 'sky-restore-' + application_id + '-'
    if (not isinstance(target_id, str) or not target_id.startswith(prefix)
            or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', target_id)
            or len(target_id) > 63):
        raise ValueError('복원 대상 ID는 sky-restore-<앱 ID>-<이름> 형식의 최대 63자여야 합니다.')
    owned_snapshot = inspect_snapshot(application_id, snapshot_id, settings)
    if owned_snapshot['status'] != 'available':
        raise AwsConfigurationError('복원 드릴에는 사용 가능한 수동 스냅샷이 필요합니다.')
    source = discover_existing_postgres(application_id, settings)
    if source['status'] != 'available':
        raise AwsConfigurationError('복원 드릴 전에 원본 RDS 상태를 확인하세요.')
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    snapshots = json.loads(adapter.aws(['rds', 'describe-db-snapshots',
        '--db-snapshot-identifier', snapshot_id], private=True, quiet=True)).get('DBSnapshots')
    if (not isinstance(snapshots, list) or len(snapshots) != 1
            or not isinstance(snapshots[0], dict)):
        raise AwsConfigurationError('복원할 스냅샷을 하나로 확인하지 못했습니다.')
    snapshot = snapshots[0]
    if (snapshot.get('DBSnapshotArn') != owned_snapshot['snapshot_arn']
            or snapshot.get('DBInstanceIdentifier') != source['database_id']
            or snapshot.get('VpcId') != source['vpc_id']
            or snapshot.get('Engine') != 'postgres'
            or snapshot.get('EngineVersion') != source['engine_version']
            or snapshot.get('AllocatedStorage') != 20
            or snapshot.get('StorageType') != 'gp3'
            or snapshot.get('Encrypted') is not True
            or snapshot.get('Status') != 'available'):
        raise AwsConfigurationError('스냅샷의 원본·VPC·엔진·저장소가 현재 DB와 다릅니다.')
    described = json.loads(adapter.aws(['rds', 'describe-db-instances',
        '--db-instance-identifier', source['database_id']], private=True, quiet=True))
    instances = described.get('DBInstances')
    instance = (instances[0] if isinstance(instances, list) and len(instances) == 1
                and isinstance(instances[0], dict) else {})
    subnet_group = instance.get('DBSubnetGroup', {})
    subnet_name = subnet_group.get('DBSubnetGroupName')
    expected_arn = (f'arn:aws:rds:{settings.region}:{settings.expected_account}:'
                    f'db:{source["database_id"]}')
    if (instance.get('DBInstanceIdentifier') != source['database_id']
            or instance.get('DBInstanceArn') != expected_arn
            or instance.get('DBInstanceStatus') != 'available'
            or instance.get('DBInstanceClass') != 'db.t4g.micro'
            or instance.get('AllocatedStorage') != 20
            or instance.get('StorageType') != 'gp3'
            or instance.get('PubliclyAccessible') is not False
            or subnet_group.get('VpcId') != source['vpc_id']
            or not isinstance(subnet_name, str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,254}', subnet_name)):
        raise AwsConfigurationError('원본 RDS의 복원 대상 네트워크·용량을 확인하지 못했습니다.')
    all_instances = json.loads(adapter.aws(['rds', 'describe-db-instances'],
                                            private=True, quiet=True))
    databases = all_instances.get('DBInstances')
    if all_instances.get('Marker') or not isinstance(databases, list):
        raise AwsConfigurationError('RDS 인스턴스 목록을 완전히 확인하지 못했습니다.')
    if any(not isinstance(db, dict) for db in databases):
        raise AwsConfigurationError('RDS 인스턴스 목록이 올바르지 않습니다.')
    if any(db.get('DBInstanceIdentifier') == target_id for db in databases):
        raise AwsConfigurationError('복원 대상 DB ID가 이미 사용 중입니다.')
    pricing = estimate_postgres_base_capacity(adapter, settings.region)
    return {'application_id': application_id, 'account': settings.expected_account,
            'region': settings.region, 'source_database_id': source['database_id'],
            'snapshot_id': snapshot_id, 'snapshot_arn': owned_snapshot['snapshot_arn'],
            'target_database_id': target_id, 'vpc_id': source['vpc_id'],
            'db_subnet_group_name': subnet_name, 'instance_class': 'db.t4g.micro',
            'storage_type': 'gp3', 'storage_gib': 20,
            'source_security_group_reused': False,
            'isolated_security_group_required': True,
            'restore_security_group_name': target_id + '-db',
            'restore_request_enabled': False,
            'pricing': pricing,
            'next_step': '복원 전용 보안 그룹을 생성한 뒤 postgres_restore_instance CLI로 복원·조회·정리를 실행하세요.'}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Read-only plan for an isolated RDS restore drill')
    parser.add_argument('--application', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service-security-group')
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    try:
        result = plan_restore_drill(args.application, args.snapshot_id, args.target_id, settings)
        print('읽기 전용 복원 드릴 계획입니다. RDS 리소스는 생성하지 않습니다.', flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, AwsConfigurationError) as exc:
        parser.exit(2, f'PostgreSQL 복원 드릴 계획 실패: {exc}\n')


if __name__ == '__main__':
    main()
