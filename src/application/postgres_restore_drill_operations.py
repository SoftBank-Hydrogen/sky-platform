"""Durable, single-attempt start and read-only recovery for an RDS restore drill."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.postgres_restore import plan_restore_drill
from adapters.aws.postgres_restore_instance import RestoreInstance
from adapters.aws.postgres_restore_network import RestoreNetworkRequest, RestoreSecurityGroup
from adapters.aws.postgres_restore_probe_network import RestoreProbeNetwork
from application.postgres_restore_task_operations import RestoreVerificationOperations


class RestoreDrillOperations:
    def __init__(self, root: Path, application_id: str, snapshot_id: str,
                 target_id: str, settings: AwsSettings, vpc_id: str):
        request = RestoreNetworkRequest(application_id, target_id,
                                        settings.expected_account, settings.region, vpc_id)
        request.validate()
        if root.is_symlink():
            raise ValueError('복원 작업 기록 디렉터리에 심볼릭 링크를 사용할 수 없습니다.')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        self.root, self.request, self.settings = root, request, settings
        self.snapshot_id = snapshot_id
        self.network = RestoreSecurityGroup(request)
        self.path = root / (target_id + '.json')

    @contextmanager
    def _exclusive(self):
        fd = os.open(self.root / '.operation.lock',
                     os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('다른 복원 작업이 실행 중입니다.') from None
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _sync_dir(self) -> None:
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            raise ValueError('복원 작업 기록이 없습니다.') from None
        with os.fdopen(fd, 'r', encoding='utf-8') as file:
            record = json.load(file)
        req = self.request
        if (not isinstance(record, dict)
                or record.get('application_id') != req.application_id
                or record.get('snapshot_id') != self.snapshot_id
                or record.get('target_id') != req.target_id
                or record.get('account') != req.account
                or record.get('region') != req.region
                or record.get('vpc_id') != req.vpc_id
                or record.get('status') not in {'starting', 'restoring', 'ready_for_probe',
                                                'needs_attention', 'cleaned'}):
            raise ValueError('복원 작업 기록의 소유권·상태가 예상과 다릅니다.')
        return record

    def _replace(self, record: dict) -> None:
        with tempfile.NamedTemporaryFile('w', dir=self.root, prefix='.restore-drill-',
                                         suffix='.json', delete=False, encoding='utf-8') as file:
            temporary = Path(file.name)
            try:
                json.dump(record, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        os.replace(temporary, self.path)
        self._sync_dir()

    def _create(self, record: dict) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(record, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        self._sync_dir()

    @staticmethod
    def _public(record: dict) -> dict:
        return {key: record.get(key) for key in (
            'application_id', 'snapshot_id', 'target_id', 'account', 'region',
            'vpc_id', 'status', 'stage', 'message', 'db_group_id', 'target_arn',
            'database_status', 'created_at')}

    def get(self) -> dict:
        return self._public(self._read())

    def plan(self) -> dict:
        restore = plan_restore_drill(self.request.application_id, self.snapshot_id,
                                     self.request.target_id, self.settings)
        self.network.preflight()
        if restore['vpc_id'] != self.request.vpc_id:
            raise AwsConfigurationError('복원 계획과 보안 그룹의 VPC가 다릅니다.')
        return {**restore, 'next_step':
                '기록된 복원 시작에는 이 CLI의 --apply를 사용하고 --reconcile로 상태를 재확인하세요.'}

    def start(self) -> dict:
        with self._exclusive():
            if self.path.exists() or self.path.is_symlink():
                raise ValueError('이 복원 대상에는 이미 작업 기록이 있습니다. 재확인하세요.')
            self.plan()
            req = self.request
            record = {'application_id': req.application_id, 'snapshot_id': self.snapshot_id,
                      'target_id': req.target_id, 'account': req.account,
                      'region': req.region, 'vpc_id': req.vpc_id,
                      'created_at': datetime.now(timezone.utc).isoformat(),
                      'status': 'starting', 'stage': 'planned',
                      'message': '복원 보안 그룹과 DB 생성 요청을 준비했습니다.'}
            try:
                self._create(record)
            except FileExistsError:
                raise ValueError('이 복원 대상에는 이미 작업 기록이 있습니다.') from None
            try:
                record['stage'] = 'creating_group'
                self._replace(record)
                group = self.network.create()
                record['db_group_id'] = group['group_id']
                record['stage'] = 'group_created'
                self._replace(record)
                instance = RestoreInstance(req.application_id, self.snapshot_id,
                                           req.target_id, self.settings,
                                           req.vpc_id, group['group_id'])
                record['stage'] = 'requesting_restore'
                self._replace(record)
                created = instance.create()
                record['target_arn'] = created['target_arn']
                record['database_status'] = created['status']
                record['stage'] = 'restore_requested'
                record['status'] = 'restoring'
                record['message'] = '복원 요청을 기록했습니다. 읽기 전용으로 상태를 재확인하세요.'
                self._replace(record)
            except Exception:
                record['status'] = 'needs_attention'
                record['message'] = 'AWS 생성 결과가 불확실합니다. 재시도하지 말고 재확인하세요.'
                self._replace(record)
                raise
            return self._public(record)

    def reconcile(self) -> dict:
        with self._exclusive():
            record = self._read()
            if record['status'] == 'cleaned':
                return self._public(record)
            self.network._account()
            try:
                group = self.network.inspect()
            except AwsConfigurationError:
                record['status'] = 'needs_attention'
                record['message'] = '복원 보안 그룹을 확인하지 못했습니다. AWS에서 조사하세요.'
                self._replace(record)
                return self._public(record)
            group_id = group['group_id']
            if record.get('db_group_id') not in {None, group_id}:
                raise AwsConfigurationError('복원 작업 기록과 AWS 보안 그룹 ID가 다릅니다.')
            record['db_group_id'] = group_id
            instance = RestoreInstance(self.request.application_id, self.snapshot_id,
                                       self.request.target_id, self.settings,
                                       self.request.vpc_id, group_id)
            try:
                database = instance.inspect()
            except AwsConfigurationError:
                record['status'] = 'needs_attention'
                record['message'] = '복원 DB를 확인하지 못했습니다. 새 복원 요청을 보내지 마세요.'
            else:
                if record.get('target_arn') not in {None, database['target_arn']}:
                    raise AwsConfigurationError('복원 작업 기록과 AWS DB ARN이 다릅니다.')
                record['target_arn'] = database['target_arn']
                record['database_status'] = database['status']
                if database['status'] == 'available':
                    record['status'] = 'ready_for_probe'
                    record['message'] = '복원 DB가 준비됐습니다. SQL 데이터 검사는 아직 필요합니다.'
                elif database['status'] in {'creating', 'configuring-enhanced-monitoring'}:
                    record['status'] = 'restoring'
                    record['message'] = '복원 DB가 생성·설정 중입니다.'
                else:
                    record['status'] = 'needs_attention'
                    record['message'] = '복원 DB 상태를 수동으로 조사하세요.'
            self._replace(record)
            return self._public(record)

    def finalize(self, verifier_state_dir: Path) -> dict:
        """Record cleanup only after read-only checks of the SQL result and AWS absence."""
        with self._exclusive():
            record = self._read()
            group_id = record.get('db_group_id')
            if not group_id or not record.get('target_arn'):
                raise AwsConfigurationError('복원 DB와 격리 그룹 생성 기록이 필요합니다.')
            verification = RestoreVerificationOperations(
                verifier_state_dir, self.settings).reconcile(self.request.target_id)
            if (verification['status'] != 'succeeded'
                    or verification['sql_verified'] is not True
                    or verification['cleanup_complete'] is not True):
                raise AwsConfigurationError('SQL 검사와 ECS 작업 정리가 먼저 확인돼야 합니다.')
            self.network._account()
            response = json.loads(self.network.adapter.aws(
                ['rds', 'describe-db-instances'], private=True, quiet=True))
            databases = response.get('DBInstances')
            if (response.get('Marker') or not isinstance(databases, list)
                    or any(not isinstance(db, dict) for db in databases)
                    or any(db.get('DBInstanceIdentifier') == self.request.target_id
                           for db in databases)):
                raise AwsConfigurationError('복원 DB 삭제 완료를 확인하지 못했습니다.')
            if self.network._matching_groups():
                raise AwsConfigurationError('복원 DB 격리 그룹이 아직 남아 있습니다.')
            if RestoreProbeNetwork(self.request, group_id)._existing():
                raise AwsConfigurationError('복원 검사 그룹이 아직 남아 있습니다.')
            record['status'] = 'cleaned'
            record['stage'] = 'cleaned'
            record['database_status'] = 'deleted'
            record['message'] = 'SQL 검사와 임시 AWS 리소스 정리를 확인했습니다.'
            self._replace(record)
            return self._public(record)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Journalled isolated RDS restore start')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create billable restore resources')
    mode.add_argument('--reconcile', action='store_true', help='Read-only AWS recovery')
    mode.add_argument('--record', action='store_true', help='Read local operation record')
    mode.add_argument('--finalize', action='store_true', help='Verify SQL and cleanup, then close record')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--application', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--service-security-group')
    parser.add_argument('--verifier-state-dir', type=Path)
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    operations = RestoreDrillOperations(args.state_dir, args.application,
        args.snapshot_id, args.target_id, settings, args.vpc_id)
    if args.finalize and args.verifier_state_dir is None:
        parser.error('--finalize에는 --verifier-state-dir이 필요합니다.')
    result = (operations.start() if args.apply else operations.reconcile()
              if args.reconcile else operations.get() if args.record else
              operations.finalize(args.verifier_state_dir) if args.finalize else operations.plan())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
