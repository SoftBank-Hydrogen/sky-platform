"""Durable HTTP retirement requests with no automatic replay after interruption."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.postgres import (PostgresRequest, discover_existing_postgres,
                                postgres_settings_for_application)
from adapters.aws.postgres_retirement import _save, execute_recorded, plan


class PostgresRetirementOperations:
    def __init__(self, root: Path, settings: AwsSettings):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.settings = settings
        self.lock = threading.Lock()
        self.plans: dict[str, dict] = {}
        self.operations: dict[str, dict] = {}
        self.recovery_warnings: list[str] = []
        for path in root.glob('*.json'):
            try:
                state = json.loads(path.read_text())
                raw = state['request']
                request = PostgresRequest(**{**raw, 'subnet_ids': tuple(raw['subnet_ids'])})
                request.validate()
                if (path.stem != request.application_id
                        or request.account != settings.expected_account
                        or request.region != settings.region
                        or (settings.service_security_group and
                            request.service_security_group != settings.service_security_group)
                        or state['application_id'] != request.application_id
                        or state['database_id'] != request.database_id
                        or state['status'] not in {'running', 'needs_attention', 'succeeded'}
                        or not re.fullmatch(r'sky-[a-z0-9-]+-final-[a-f0-9]{12}',
                                            state['snapshot_id'])):
                    raise ValueError('Invalid retirement record')
                if state['status'] == 'running':
                    state['status'] = 'needs_attention'
                    state['message'] = '서버가 중단됐습니다. 기록된 단계와 AWS 리소스를 수동 확인하세요. 자동 재시도하지 않습니다.'
                    _save(path, state)
                self.operations[request.application_id] = state
            except (OSError, ValueError, KeyError, TypeError, AwsConfigurationError):
                self.recovery_warnings.append('PostgreSQL 폐기 기록을 불러오지 못했습니다: ' + path.stem)

    def _request(self, application_id: str) -> PostgresRequest:
        database = discover_existing_postgres(application_id, self.settings)
        settings = postgres_settings_for_application(application_id, database['vpc_id'], self.settings)
        return PostgresRequest(application_id, settings.expected_account, settings.region,
            database['vpc_id'], tuple(database['subnet_ids']), settings.service_security_group)

    def plan(self, application_id: str) -> dict:
        with self.lock:
            if application_id in self.operations:
                raise ValueError('이 앱의 DB 폐기 기록이 이미 있습니다. 상태를 확인하세요.')
        request = self._request(application_id)
        expected = plan(request)
        token = secrets.token_urlsafe(24)
        with self.lock:
            if application_id in self.operations:
                raise ValueError('이 앱의 DB 폐기 기록이 이미 있습니다.')
            self.plans[application_id] = {'id': token, 'request': request,
                                          'expected': expected, 'expires': time.monotonic() + 900}
        return {**expected, 'plan_id': token}

    def start(self, application_id: str, plan_id: str, confirm_database_id: str) -> dict:
        with self.lock:
            planned = self.plans.get(application_id)
            if (not planned or planned['id'] != plan_id or time.monotonic() > planned['expires']):
                raise ValueError('DB 폐기 계획이 없거나 만료됐습니다. 다시 조회하세요.')
            if application_id in self.operations:
                raise ValueError('이 앱의 DB 폐기 요청이 이미 기록돼 있습니다.')
            request, expected = planned['request'], planned['expected']
        if confirm_database_id != request.database_id:
            raise ValueError('DB ID를 정확히 입력해야 합니다.')
        if plan(request) != expected:
            raise ValueError('DB 폐기 계획이 바뀌었습니다. 다시 조회하세요.')
        state = {**expected, 'request': asdict(request),
                 'snapshot_id': f'sky-{application_id}-final-{secrets.token_hex(6)}',
                 'status': 'running', 'stage': 'planned',
                 'created_at': datetime.now(timezone.utc).isoformat(),
                 'message': '최종 스냅샷 생성부터 시작합니다.'}
        with self.lock:
            if application_id in self.operations:
                raise ValueError('이 앱의 DB 폐기 요청이 이미 기록돼 있습니다.')
            _save(self.root / (application_id + '.json'), state, new=True)
            self.operations[application_id] = state
            del self.plans[application_id]
        try:
            threading.Thread(target=self._run, args=(application_id,), daemon=True).start()
        except Exception:
            with self.lock:
                state['status'] = 'needs_attention'
                state['message'] = '폐기 작업을 시작하지 못했습니다. AWS 상태를 확인하세요.'
                _save(self.root / (application_id + '.json'), state)
            raise
        return self.get(application_id)

    def _run(self, application_id: str) -> None:
        with self.lock:
            state = self.operations[application_id]
            raw = state['request']
            request = PostgresRequest(**{**raw, 'subnet_ids': tuple(raw['subnet_ids'])})
        try:
            execute_recorded(request, self.root / (application_id + '.json'), state)
            with self.lock:
                state['message'] = 'DB와 RDS 스택 삭제를 확인했습니다. 최종 수동 스냅샷과 서비스 네트워크는 보존합니다.'
                _save(self.root / (application_id + '.json'), state)
        except Exception as exc:
            with self.lock:
                state['status'] = 'needs_attention'
                state['message'] = '폐기 결과를 확인하지 못했습니다: ' + str(exc)
                _save(self.root / (application_id + '.json'), state)

    def get(self, application_id: str) -> dict:
        with self.lock:
            state = self.operations.get(application_id)
            if not state:
                raise ValueError('이 앱의 DB 폐기 요청 기록이 없습니다.')
            return {key: state.get(key) for key in ('application_id', 'account', 'region',
                'database_id', 'snapshot_id', 'status', 'stage', 'message', 'created_at')}

    def blocks_deployment(self, application_id: str) -> bool:
        with self.lock:
            return application_id in self.operations
