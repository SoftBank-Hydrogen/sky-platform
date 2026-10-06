"""Pin source RDS secret metadata to a snapshot without reading its password."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from database.postgres import discover_existing_postgres
from database.postgres_restore_network import RestoreNetworkRequest
from database.postgres_snapshot import inspect_snapshot


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AwsConfigurationError('스냅샷·비밀 버전 시각을 확인하지 못했습니다.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise AwsConfigurationError('스냅샷·비밀 버전 시각을 확인하지 못했습니다.') from None
    if parsed.tzinfo is None:
        raise AwsConfigurationError('스냅샷·비밀 버전 시각에 시간대가 없습니다.')
    return parsed.astimezone(timezone.utc)


def plan_restore_credentials(application_id: str, snapshot_id: str,
                             target_id: str, settings: AwsSettings) -> dict:
    owned = inspect_snapshot(application_id, snapshot_id, settings)
    if owned['status'] != 'available':
        raise AwsConfigurationError('사용 가능한 앱 소유 스냅샷이 필요합니다.')
    source = discover_existing_postgres(application_id, settings)
    RestoreNetworkRequest(application_id, target_id, settings.expected_account,
                          settings.region, source['vpc_id']).validate()
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    snapshot_response = json.loads(adapter.aws(['rds', 'describe-db-snapshots',
        '--db-snapshot-identifier', snapshot_id], private=True, quiet=True))
    snapshots = snapshot_response.get('DBSnapshots')
    snapshot = snapshots[0] if isinstance(snapshots, list) and len(snapshots) == 1 else {}
    if (snapshot_response.get('Marker') or snapshot.get('DBSnapshotArn') != owned['snapshot_arn']
            or snapshot.get('DBInstanceIdentifier') != source['database_id']
            or snapshot.get('VpcId') != source['vpc_id']
            or snapshot.get('Status') != 'available'):
        raise AwsConfigurationError('스냅샷 원본·상태를 확인하지 못했습니다.')
    snapshot_time = _timestamp(snapshot.get('SnapshotCreateTime'))
    source_response = json.loads(adapter.aws(['rds', 'describe-db-instances',
        '--db-instance-identifier', source['database_id']], private=True, quiet=True))
    instances = source_response.get('DBInstances')
    instance = instances[0] if isinstance(instances, list) and len(instances) == 1 else {}
    source_arn = (f'arn:aws:rds:{settings.region}:{settings.expected_account}:'
                  f'db:{source["database_id"]}')
    secret = instance.get('MasterUserSecret')
    secret_arn = secret.get('SecretArn') if isinstance(secret, dict) else None
    secret_prefix = f'arn:aws:secretsmanager:{settings.region}:{settings.expected_account}:secret:'
    if (source_response.get('Marker') or instance.get('DBInstanceArn') != source_arn
            or instance.get('DBInstanceStatus') != 'available'
            or not isinstance(secret_arn, str)
            or not secret_arn.startswith(secret_prefix)
            or not re.fullmatch(r'[A-Za-z0-9/_+=.!@-]+',
                                secret_arn.removeprefix(secret_prefix))):
        raise AwsConfigurationError('원본 RDS의 관리형 비밀 소유권을 확인하지 못했습니다.')
    versions_response = json.loads(adapter.aws(['secretsmanager', 'list-secret-version-ids',
        '--secret-id', secret_arn], private=True, quiet=True))
    versions = versions_response.get('Versions')
    if (versions_response.get('NextToken') or versions_response.get('ARN') != secret_arn
            or not isinstance(versions, list)
            or any(not isinstance(version, dict) for version in versions)):
        raise AwsConfigurationError('원본 관리형 비밀 버전을 완전히 확인하지 못했습니다.')
    if any(not isinstance(version.get('VersionStages', []), list) for version in versions):
        raise AwsConfigurationError('비밀 버전 상태를 확인하지 못했습니다.')
    current = [version for version in versions
               if 'AWSCURRENT' in version.get('VersionStages', [])]
    version = current[0] if len(current) == 1 else {}
    version_id = version.get('VersionId')
    if (not isinstance(version_id, str)
            or not re.fullmatch(r'[A-Za-z0-9-]{32,64}', version_id)
            or _timestamp(version.get('CreatedDate')) > snapshot_time):
        raise AwsConfigurationError('현재 비밀 버전이 스냅샷 생성 시점보다 늦거나 불확실합니다.')
    return {'application_id': application_id, 'snapshot_id': snapshot_id,
            'target_database_id': target_id, 'source_database_id': source['database_id'],
            'account': settings.expected_account, 'region': settings.region,
            'secret_arn': secret_arn, 'secret_version_id': version_id,
            'snapshot_created_at': snapshot_time.isoformat(),
            'secret_version_created_at': _timestamp(version['CreatedDate']).isoformat(),
            'ecs_username_value_from': secret_arn + ':username::' + version_id,
            'ecs_password_value_from': secret_arn + ':password::' + version_id,
            'secret_value_read': False,
            'password_match_unverified': True}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Read-only snapshot credential version plan')
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
    result = plan_restore_credentials(args.application, args.snapshot_id,
                                      args.target_id, settings)
    print('읽기 전용 비밀 버전 계획입니다. 비밀번호 값은 조회하지 않았습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
