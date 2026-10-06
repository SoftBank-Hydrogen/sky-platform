"""Journalled manual RDS snapshot requests with explicit plans and read-only recovery."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from adapters.aws.ecs import AwsSettings
from adapters.aws.postgres_snapshot import _identity, create_snapshot, inspect_snapshot, plan_snapshot


class SnapshotOperations:
    def __init__(self, root: Path, settings: AwsSettings):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.settings = settings
        self.lock = threading.Lock()
        self.plans: dict[str, dict] = {}
        self.operations: dict[str, dict] = {}
        self.recovery_warnings: list[str] = []
        for path in self.root.glob('*.json'):
            try:
                operation = json.loads(path.read_text())
                application_id, snapshot_id = operation['application_id'], operation['snapshot_id']
                _identity(application_id, snapshot_id, settings)
                if (path.stem != snapshot_id
                        or operation['account'] != settings.expected_account
                        or operation['region'] != settings.region
                        or operation['status'] not in {'running', 'pending', 'succeeded', 'needs_attention'}):
                    raise ValueError('Unexpected snapshot operation')
                if operation['status'] == 'running':
                    operation['status'] = 'needs_attention'
                    operation['message'] = '서버 재시작으로 생성 요청 결과가 불확실합니다. AWS 상태를 재확인하세요.'
                    self._save(operation)
                self.operations[snapshot_id] = operation
            except (OSError, ValueError, KeyError, TypeError):
                self.recovery_warnings.append('수동 스냅샷 작업 기록을 불러오지 못했습니다: ' + path.stem)

    def _save(self, operation: dict) -> None:
        with tempfile.NamedTemporaryFile('w', dir=self.root, prefix='.snapshot-',
                                         suffix='.json', delete=False) as file:
            try:
                json.dump(operation, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                temporary = Path(file.name)
            except Exception:
                Path(file.name).unlink(missing_ok=True)
                raise
        os.replace(temporary, self.root / (operation['snapshot_id'] + '.json'))

    def plan(self, application_id: str, snapshot_id: str) -> dict:
        with self.lock:
            if snapshot_id in self.operations:
                raise ValueError('이 수동 스냅샷의 생성 요청 기록이 이미 있습니다.')
            if any(op['application_id'] == application_id and
                   op['status'] in {'running', 'pending', 'needs_attention'}
                   for op in self.operations.values()):
                raise ValueError('이 앱의 이전 수동 스냅샷 생성 결과를 먼저 확인하세요.')
        result = plan_snapshot(application_id, snapshot_id, self.settings)
        token = secrets.token_urlsafe(24)
        with self.lock:
            if snapshot_id in self.operations:
                raise ValueError('이 수동 스냅샷의 생성 요청 기록이 이미 있습니다.')
            self.plans[application_id] = {'id': token, 'snapshot_id': snapshot_id,
                                          'result': result, 'expires': time.monotonic() + 900}
        return {**result, 'plan_id': token}

    def start(self, application_id: str, plan_id: str) -> dict:
        with self.lock:
            plan = self.plans.get(application_id)
            if not plan or plan['id'] != plan_id or time.monotonic() > plan['expires']:
                raise ValueError('스냅샷 생성 계획이 없거나 만료됐습니다. 다시 확인하세요.')
            snapshot_id, expected = plan['snapshot_id'], plan['result']
            if snapshot_id in self.operations:
                raise ValueError('이 수동 스냅샷의 생성 요청 기록이 이미 있습니다.')
        current = plan_snapshot(application_id, snapshot_id, self.settings)
        if current != expected:
            raise ValueError('스냅샷 생성 계획이 변경됐습니다. 다시 확인하세요.')
        operation = {'application_id': application_id, 'snapshot_id': snapshot_id,
                     'account': self.settings.expected_account, 'region': self.settings.region,
                     'status': 'running', 'created_at': datetime.now(timezone.utc).isoformat(),
                     'message': '수동 스냅샷 생성 요청을 AWS에 전달하고 있습니다.'}
        with self.lock:
            if snapshot_id in self.operations or any(
                    op['application_id'] == application_id and
                    op['status'] in {'running', 'pending', 'needs_attention'}
                    for op in self.operations.values()):
                raise ValueError('이 앱의 수동 스냅샷 생성 요청이 이미 진행 중입니다.')
            self._save(operation)
            self.operations[snapshot_id] = operation
            del self.plans[application_id]
        try:
            threading.Thread(target=self._run, args=(snapshot_id,), daemon=True).start()
        except Exception:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = '생성 작업을 시작하지 못했습니다. AWS 상태를 재확인하세요.'
                self._save(operation)
            raise
        return self.get(application_id, snapshot_id)

    def _run(self, snapshot_id: str) -> None:
        with self.lock:
            application_id = self.operations[snapshot_id]['application_id']
        try:
            snapshot = create_snapshot(application_id, snapshot_id, self.settings)
        except Exception as exc:
            with self.lock:
                operation = self.operations[snapshot_id]
                operation['status'] = 'needs_attention'
                operation['message'] = 'AWS 생성 결과를 확정하지 못했습니다: ' + str(exc)
                self._save(operation)
            return
        with self.lock:
            operation = self.operations[snapshot_id]
            operation['status'] = 'pending' if snapshot['status'] != 'available' else 'succeeded'
            operation['aws_status'] = snapshot['status']
            operation['message'] = ('스냅샷이 생성 중입니다. AWS 상태를 재확인하세요.'
                                    if operation['status'] == 'pending' else '스냅샷이 사용 가능합니다.')
            self._save(operation)

    def get(self, application_id: str, snapshot_id: str) -> dict:
        _identity(application_id, snapshot_id, self.settings)
        with self.lock:
            operation = self.operations.get(snapshot_id)
            if not operation or operation['application_id'] != application_id:
                raise ValueError('이 앱의 수동 스냅샷 작업 기록이 없습니다.')
            return dict(operation)

    def reconcile(self, application_id: str, snapshot_id: str) -> dict:
        operation = self.get(application_id, snapshot_id)
        if operation['status'] == 'running':
            raise ValueError('생성 요청이 실행 중입니다. 완료 후 다시 확인하세요.')
        try:
            snapshot = inspect_snapshot(application_id, snapshot_id, self.settings)
        except Exception as exc:
            with self.lock:
                operation = self.operations[snapshot_id]
                operation['status'] = 'needs_attention'
                operation['message'] = 'AWS 스냅샷 상태를 확정하지 못했습니다: ' + str(exc)
                self._save(operation)
        else:
            with self.lock:
                operation = self.operations[snapshot_id]
                operation['aws_status'] = snapshot['status']
                operation['status'] = ('succeeded' if snapshot['status'] == 'available'
                                       else 'pending' if snapshot['status'] == 'creating'
                                       else 'needs_attention')
                operation['message'] = ('스냅샷이 사용 가능합니다.' if operation['status'] == 'succeeded'
                                        else 'AWS 스냅샷 상태: ' + snapshot['status'] + '. 다시 확인하세요.')
                self._save(operation)
        return self.get(application_id, snapshot_id)
