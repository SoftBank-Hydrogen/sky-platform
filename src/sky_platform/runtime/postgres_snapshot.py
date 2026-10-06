"""Explicit, account-pinned manual snapshots for an existing Sky RDS instance."""
from __future__ import annotations

import argparse
import json
import re

from sky_platform.runtime.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from sky_platform.runtime.postgres import inspect_postgres_backup_status


def _identity(application_id: str, snapshot_id: str, settings: AwsSettings) -> tuple[str, str]:
    settings.validate()
    if (not settings.expected_account
            or not isinstance(application_id, str)
            or not 3 <= len(application_id) <= 31
            or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', application_id)):
        raise ValueError('DB 앱 ID 또는 고정 AWS 계정이 올바르지 않습니다.')
    if not isinstance(snapshot_id, str) or not re.fullmatch(
            rf'sky-{re.escape(application_id)}-[a-z0-9]+(?:-[a-z0-9]+)*', snapshot_id
    ) or len(snapshot_id) > 255:
        raise ValueError('스냅샷 ID는 sky-<앱 ID>-<이름> 형식이어야 합니다.')
    return ('sky-' + application_id,
            f'arn:aws:rds:{settings.region}:{settings.expected_account}:snapshot:{snapshot_id}')


def _snapshots(application_id: str, settings: AwsSettings) -> list[dict]:
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    result = json.loads(adapter.aws(['rds', 'describe-db-snapshots',
        '--db-instance-identifier', 'sky-' + application_id,
        '--snapshot-type', 'manual'], private=True, quiet=True))
    snapshots = result.get('DBSnapshots')
    if result.get('Marker') or not isinstance(snapshots, list):
        raise AwsConfigurationError('수동 스냅샷 목록을 완전히 확인하지 못했습니다.')
    return snapshots


def plan_snapshot(application_id: str, snapshot_id: str, settings: AwsSettings) -> dict:
    database_id, snapshot_arn = _identity(application_id, snapshot_id, settings)
    backups = inspect_postgres_backup_status(application_id, settings)
    if backups['database_id'] != database_id or backups['database_status'] != 'available':
        raise AwsConfigurationError('스냅샷 생성에는 사용 가능한 앱 소유 RDS가 필요합니다.')
    if any(item.get('DBSnapshotIdentifier') == snapshot_id
           for item in _snapshots(application_id, settings)):
        raise AwsConfigurationError('같은 ID의 수동 스냅샷이 이미 있습니다.')
    return {'application_id': application_id, 'database_id': database_id,
            'snapshot_id': snapshot_id, 'snapshot_arn': snapshot_arn,
            'account': settings.expected_account, 'region': settings.region,
            'manual_snapshot_count': backups['manual_snapshot_count'],
            'storage_cost_warning': '수동 스냅샷은 삭제 전까지 보관되며 백업 저장 비용이 발생할 수 있습니다.'}


def _verified_snapshot(snapshot: dict, database_id: str, snapshot_id: str,
                       snapshot_arn: str) -> dict:
    if (not isinstance(snapshot, dict)
            or snapshot.get('DBSnapshotIdentifier') != snapshot_id
            or snapshot.get('DBInstanceIdentifier') != database_id
            or snapshot.get('DBSnapshotArn') != snapshot_arn
            or snapshot.get('SnapshotType') != 'manual'
            or snapshot.get('Encrypted') is not True
            or not isinstance(snapshot.get('Status'), str)
            or not snapshot['Status']):
        raise AwsConfigurationError('생성된 스냅샷의 DB·계정·암호화 상태를 확인하지 못했습니다.')
    return {'database_id': database_id, 'snapshot_id': snapshot_id,
            'snapshot_arn': snapshot_arn, 'status': snapshot['Status'], 'encrypted': True}


def create_snapshot(application_id: str, snapshot_id: str, settings: AwsSettings) -> dict:
    plan = plan_snapshot(application_id, snapshot_id, settings)
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    created = json.loads(adapter.aws(['rds', 'create-db-snapshot',
        '--db-instance-identifier', plan['database_id'],
        '--db-snapshot-identifier', snapshot_id, '--tags',
        'Key=sky-managed,Value=true', f'Key=sky-app,Value={application_id}'],
        private=True, quiet=True)).get('DBSnapshot')
    snapshot = _verified_snapshot(created, plan['database_id'], snapshot_id, plan['snapshot_arn'])
    if snapshot['status'] not in {'creating', 'available'}:
        raise AwsConfigurationError('수동 스냅샷 생성 요청의 상태를 확인하지 못했습니다.')
    return snapshot


def inspect_snapshot(application_id: str, snapshot_id: str, settings: AwsSettings) -> dict:
    database_id, snapshot_arn = _identity(application_id, snapshot_id, settings)
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    identity = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
    if identity.get('Account') != settings.expected_account:
        raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
    result = json.loads(adapter.aws(['rds', 'describe-db-snapshots',
        '--db-snapshot-identifier', snapshot_id], private=True, quiet=True))
    snapshots = result.get('DBSnapshots')
    if result.get('Marker') or not isinstance(snapshots, list) or len(snapshots) != 1:
        raise AwsConfigurationError('수동 스냅샷을 하나로 확인하지 못했습니다.')
    snapshot = _verified_snapshot(snapshots[0], database_id, snapshot_id, snapshot_arn)
    tags = json.loads(adapter.aws(['rds', 'list-tags-for-resource',
        '--resource-name', snapshot_arn], private=True, quiet=True)).get('TagList')
    if not isinstance(tags, list) or any(not isinstance(tag, dict) for tag in tags):
        raise AwsConfigurationError('수동 스냅샷의 소유 태그를 확인하지 못했습니다.')
    owned = {tag.get('Key'): tag.get('Value') for tag in tags}
    if owned.get('sky-managed') != 'true' or owned.get('sky-app') != application_id:
        raise AwsConfigurationError('수동 스냅샷의 소유 태그가 예상과 다릅니다.')
    return snapshot


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Create an explicitly named RDS manual snapshot')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create a billable manual snapshot')
    mode.add_argument('--inspect', action='store_true', help='Verify an existing snapshot')
    parser.add_argument('--application', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service-security-group')
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    try:
        if args.inspect:
            result = inspect_snapshot(args.application, args.snapshot_id, settings)
        elif args.apply:
            result = create_snapshot(args.application, args.snapshot_id, settings)
        else:
            result = plan_snapshot(args.application, args.snapshot_id, settings)
            print('읽기 전용 계획 완료. --apply 없이는 수동 스냅샷을 만들지 않습니다.', flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, AwsConfigurationError) as exc:
        parser.exit(2, f'PostgreSQL 수동 스냅샷 점검 실패: {exc}\n')


if __name__ == '__main__':
    main()
