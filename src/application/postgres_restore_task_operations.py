"""Durable local journal for a one-shot RDS restore verification task."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from adapters.database.migrations import MigrationBundle, collect_sql_migrations
from adapters.aws.postgres_restore_network import RestoreNetworkRequest
from adapters.aws.postgres_restore_task import RestoreVerifierRunner, plan_restore_verifier_task


class RestoreVerificationOperations:
    def __init__(self, root: Path, settings: AwsSettings):
        settings.validate()
        if root.is_symlink():
            raise ValueError('복원 검사 기록 디렉터리에 심볼릭 링크를 사용할 수 없습니다.')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        self.root, self.settings = root, settings
        self.lock = threading.RLock()
        for path in root.glob('*.json'):
            self._read(path.stem)

    @contextmanager
    def _exclusive(self):
        fd = os.open(self.root / '.operation.lock',
                     os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('다른 복원 검사 작업이 실행 중입니다.') from None
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _path(self, target_id: str) -> Path:
        if not re.fullmatch(r'sky-restore-[a-z0-9]+(?:-[a-z0-9]+)*', target_id):
            raise ValueError('복원 대상 ID 형식이 올바르지 않습니다.')
        return self.root / (target_id + '.json')

    def _read(self, target_id: str) -> dict:
        path = self._path(target_id)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            raise ValueError('복원 검사 작업 기록이 없습니다.') from None
        with os.fdopen(fd, 'r', encoding='utf-8') as file:
            record = json.load(file)
        if not isinstance(record, dict) or not isinstance(record.get('plan'), dict):
            raise ValueError('복원 검사 기록 형식이 올바르지 않습니다.')
        plan = record.get('plan', {})
        if (record.get('target_id') != target_id
                or record.get('account') != self.settings.expected_account
                or record.get('region') != self.settings.region
                or plan.get('target_database_id') != target_id
                or plan.get('account') != self.settings.expected_account
                or plan.get('region') != self.settings.region
                or not isinstance(record.get('attempt_id'), str)
                or not re.fullmatch(r'[a-f0-9]{16}', record['attempt_id'])
                or record.get('status') not in {'running', 'needs_attention', 'succeeded'}):
            raise ValueError('복원 검사 기록의 소유권·상태가 예상과 다릅니다.')
        return record

    def _replace(self, record: dict) -> None:
        path = self._path(record['target_id'])
        with tempfile.NamedTemporaryFile('w', dir=self.root, prefix='.restore-verify-',
                                         suffix='.json', delete=False, encoding='utf-8') as file:
            temporary = Path(file.name)
            try:
                json.dump(record, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        os.replace(temporary, path)
        self._sync_dir()

    def _sync_dir(self) -> None:
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _create(self, record: dict) -> None:
        path = self._path(record['target_id'])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(record, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._sync_dir()

    def get(self, target_id: str) -> dict:
        with self.lock:
            record = self._read(target_id)
        return {key: record.get(key) for key in (
            'target_id', 'account', 'region', 'attempt_id', 'status', 'stage',
            'message', 'image_tag', 'image_digest', 'task_definition_arn',
            'task_arn', 'sql_verified', 'cleanup_complete', 'created_at',
            'recovery')}

    def start(self, plan: dict, bundle: MigrationBundle,
              marker_id: str | None = None) -> dict:
        with self._exclusive():
            return self._start_locked(plan, bundle, marker_id)

    def _start_locked(self, plan: dict, bundle: MigrationBundle,
                      marker_id: str | None = None) -> dict:
        target_id = plan['target_database_id']
        RestoreNetworkRequest(plan['application_id'], target_id, plan['account'],
                              plan['region'], plan['vpc_id']).validate()
        if (plan['account'] != self.settings.expected_account
                or plan['region'] != self.settings.region
                or plan['bundle_digest'] != bundle.digest
                or (marker_id is not None and not re.fullmatch(r'[a-f0-9]{32}', marker_id))):
            raise ValueError('복원 검사 계획의 계정·리전·SQL 번들이 다릅니다.')
        attempt_id = secrets.token_hex(8)
        adapter = AwsExpressAdapter(lambda *_: None, self.settings)
        record = {'target_id': target_id, 'account': plan['account'],
                  'region': plan['region'], 'attempt_id': attempt_id,
                  'plan': plan, 'marker_id': marker_id,
                  'created_at': datetime.now(timezone.utc).isoformat(),
                  'status': 'running', 'stage': 'planned',
                  'message': '복원 검사 작업을 실행 중입니다.',
                  'image_tag': 'restore-verify-' + attempt_id,
                  'sql_verified': False, 'cleanup_complete': False}
        with self.lock:
            try:
                self._create(record)
            except FileExistsError:
                raise ValueError('이 복원 대상에는 이미 검사 작업 기록이 있습니다.') from None

        def checkpoint(stage: str, **fields) -> None:
            with self.lock:
                record['stage'] = stage
                record.update(fields)
                self._replace(record)

        try:
            runner = RestoreVerifierRunner(adapter, plan, attempt_id,
                                           marker_id=marker_id, checkpoint=checkpoint)
            result = runner.execute(bundle)
        except Exception:
            with self.lock:
                record['status'] = 'needs_attention'
                record['message'] = 'AWS 결과가 실패 또는 불확실합니다. 재실행하지 말고 재확인하세요.'
                self._replace(record)
            raise
        with self.lock:
            record['sql_verified'] = True
            record['cleanup_complete'] = result['cleanup']['status'] == 'cleaned'
            record['status'] = 'succeeded' if record['cleanup_complete'] else 'needs_attention'
            record['message'] = ('SQL 검사와 작업 정리가 완료됐습니다.' if record['cleanup_complete']
                                 else 'SQL은 통과했지만 작업 정리를 확인해야 합니다.')
            self._replace(record)
        return self.get(target_id)

    def reconcile(self, target_id: str) -> dict:
        with self._exclusive():
            return self._reconcile_locked(target_id)

    def _reconcile_locked(self, target_id: str) -> dict:
        with self.lock:
            record = self._read(target_id)
        adapter = AwsExpressAdapter(lambda *_: None, self.settings)
        identity = json.loads(adapter.aws(['sts', 'get-caller-identity'],
                                          private=True, quiet=True))
        if identity.get('Account') != record['account']:
            raise AwsConfigurationError('현재 AWS 계정이 검사 기록의 계정과 다릅니다.')
        runner = RestoreVerifierRunner(adapter, record['plan'], record['attempt_id'],
                                       image_digest=record.get('image_digest'),
                                       marker_id=record.get('marker_id'))
        task_arn = record.get('task_arn')
        definition_arn = record.get('task_definition_arn')
        images_response = json.loads(adapter.aws(['ecr', 'list-images', '--registry-id',
            record['account'], '--repository-name', 'sky-managed',
            '--filter', 'tagStatus=TAGGED'], private=True, quiet=True))
        images = images_response.get('imageIds')
        if images_response.get('nextToken') or not isinstance(images, list):
            raise AwsConfigurationError('검사 이미지 목록을 완전히 확인하지 못했습니다.')
        image_present = any(item.get('imageTag') == record['image_tag'] for item in images)
        family = 'sky-restore-verify-' + record['attempt_id']
        definitions_response = json.loads(adapter.aws(['ecs', 'list-task-definitions',
            '--family-prefix', family, '--status', 'ACTIVE'],
            private=True, quiet=True))
        definitions = definitions_response.get('taskDefinitionArns')
        prefix = (f'arn:aws:ecs:{record["region"]}:{record["account"]}:'
                  f'task-definition/{family}:')
        if (definitions_response.get('nextToken') or not isinstance(definitions, list)
                or any(not isinstance(arn, str) or not arn.startswith(prefix)
                       or not arn.removeprefix(prefix).isdigit() for arn in definitions)):
            raise AwsConfigurationError('검사 태스크 정의 목록을 완전히 확인하지 못했습니다.')
        record['recovery'] = {'image_present': image_present,
                              'active_definition_arns': definitions}
        with self.lock:
            if task_arn and definition_arn:
                try:
                    result = runner.inspect_result(task_arn, definition_arn)
                except (AwsConfigurationError, ValueError):
                    record['message'] = 'ECS 작업·SQL 결과를 확정하지 못했습니다. AWS에서 확인하세요.'
                else:
                    record['sql_verified'] = result['status'] == 'succeeded' and 'log_stream' in result
                    record['message'] = ('SQL 검사를 확인했습니다. 정리 상태를 별도 확인하세요.'
                                         if record['sql_verified'] else
                                         'ECS 작업 상태: ' + result['status'])
            else:
                record['message'] = ('작업 ARN 기록이 없어 ECS 실행 여부가 불확실합니다. '
                                     '이미지 태그와 태스크 정의를 AWS에서 확인하세요.')
            record['cleanup_complete'] = False
            if record['sql_verified'] and definition_arn and not image_present \
                    and definition_arn not in definitions:
                described = json.loads(adapter.aws(['ecs', 'describe-task-definition',
                    '--task-definition', definition_arn], private=True, quiet=True))
                definition = described.get('taskDefinition', {})
                if (definition.get('taskDefinitionArn') == definition_arn
                        and definition.get('status') == 'INACTIVE'):
                    record['cleanup_complete'] = True
                    record['message'] = 'SQL 검사와 AWS 작업 정리를 읽기 전용으로 확인했습니다.'
            record['status'] = ('succeeded' if record['sql_verified'] and
                                record['cleanup_complete'] else 'needs_attention')
            self._replace(record)
        return self.get(target_id)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Journalled RDS restore verification')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Build image and run billable ECS task')
    mode.add_argument('--reconcile', action='store_true', help='Read-only AWS result reconciliation')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--application')
    parser.add_argument('--snapshot-id')
    parser.add_argument('--vpc-id')
    parser.add_argument('--db-group-id')
    parser.add_argument('--probe-group-id')
    parser.add_argument('--service-security-group')
    parser.add_argument('--project', type=Path)
    parser.add_argument('--marker-id')
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    operations = RestoreVerificationOperations(args.state_dir, settings)
    if args.apply:
        if not all((args.application, args.snapshot_id, args.vpc_id, args.db_group_id,
                    args.probe_group_id, args.service_security_group, args.project)):
            parser.error('--apply에는 앱·스냅샷·VPC·보안 그룹·서비스 그룹·프로젝트가 필요합니다.')
        plan = plan_restore_verifier_task(args.application, args.snapshot_id,
            args.target_id, settings, args.vpc_id, args.db_group_id,
            args.probe_group_id, args.project)
        result = operations.start(plan, collect_sql_migrations(args.project), args.marker_id)
    elif args.reconcile:
        result = operations.reconcile(args.target_id)
    else:
        result = operations.get(args.target_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
