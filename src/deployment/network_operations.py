"""Journal explicit app-network creation without retrying uncertain AWS mutations."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from deployment.aws import AwsConfigurationError, AwsSettings
from deployment.aws_network import AwsServiceNetworkProvisioner, ServiceNetworkRequest


class NetworkOperations:
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
                request = ServiceNetworkRequest(**operation['request'])
                request.validate()
                if (path.stem != request.application_id
                        or operation.get('application_id') != request.application_id
                        or request.account != settings.expected_account
                        or request.region != settings.region
                        or operation['status'] not in {'running', 'succeeded', 'needs_attention'}):
                    raise ValueError('Unexpected network operation')
                if operation['status'] == 'running':
                    operation['status'] = 'needs_attention'
                    operation['message'] = '서버 재시작으로 네트워크 생성 결과가 불확실합니다. AWS 상태를 재확인하세요.'
                    self._save(operation)
                self.operations[request.application_id] = operation
            except (OSError, ValueError, KeyError, TypeError, AwsConfigurationError):
                self.recovery_warnings.append('서비스 네트워크 생성 기록을 불러오지 못했습니다: ' + path.stem)

    def _save(self, operation: dict) -> None:
        with tempfile.NamedTemporaryFile('w', dir=self.root, prefix='.network-',
                                         suffix='.json', delete=False) as file:
            try:
                json.dump(operation, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                temporary = Path(file.name)
            except Exception:
                Path(file.name).unlink(missing_ok=True)
                raise
        os.replace(temporary, self.root / (operation['application_id'] + '.json'))

    def plan(self, request: ServiceNetworkRequest) -> dict:
        request.validate()
        if (request.account != self.settings.expected_account
                or request.region != self.settings.region
                or self.settings.service_security_group):
            raise AwsConfigurationError('앱별 네트워크에는 고정 서비스 보안 그룹 없이 AWS 계정·리전을 지정하세요.')
        with self.lock:
            if request.application_id in self.operations:
                raise ValueError('이 앱에는 이미 네트워크 생성 기록이 있습니다. 상태를 확인하세요.')
        result = AwsServiceNetworkProvisioner(request).preflight()
        token = secrets.token_urlsafe(24)
        with self.lock:
            if request.application_id in self.operations:
                raise ValueError('이 앱에는 이미 네트워크 생성 기록이 있습니다.')
            self.plans[request.application_id] = {'id': token, 'request': request,
                                                   'result': result, 'expires': time.monotonic() + 900}
        return {**result, 'plan_id': token}

    def start(self, application_id: str, plan_id: str) -> dict:
        with self.lock:
            plan = self.plans.get(application_id)
            if not plan or plan['id'] != plan_id or time.monotonic() > plan['expires']:
                raise ValueError('네트워크 생성 계획이 없거나 만료됐습니다. 다시 확인하세요.')
            if application_id in self.operations:
                raise ValueError('이 앱의 네트워크 생성 요청이 이미 기록돼 있습니다.')
            request, expected = plan['request'], plan['result']
        if AwsServiceNetworkProvisioner(request).preflight() != expected:
            raise ValueError('네트워크 계획이 변경됐습니다. 다시 확인하세요.')
        operation = {'application_id': application_id, 'request': asdict(request),
                     'status': 'running', 'created_at': datetime.now(timezone.utc).isoformat(),
                     'message': '앱 전용 서비스 보안 그룹을 생성하고 있습니다.'}
        with self.lock:
            if application_id in self.operations:
                raise ValueError('이 앱의 네트워크 생성 요청이 이미 기록돼 있습니다.')
            self._save(operation)
            self.operations[application_id] = operation
            del self.plans[application_id]
        try:
            threading.Thread(target=self._run, args=(application_id,), daemon=True).start()
        except Exception:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = '네트워크 생성 작업을 시작하지 못했습니다. AWS 상태를 재확인하세요.'
                self._save(operation)
            raise
        return self.get(application_id)

    def _run(self, application_id: str) -> None:
        with self.lock:
            operation = self.operations[application_id]
            request = ServiceNetworkRequest(**operation['request'])
        try:
            network = AwsServiceNetworkProvisioner(request).create()
            group_id = network['service_security_group']
            stack_id = network['stack_id']
        except Exception as exc:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = '네트워크 생성 결과를 확인하지 못했습니다: ' + str(exc)
                self._save(operation)
            return
        with self.lock:
            operation['status'] = 'succeeded'
            operation['message'] = '앱 전용 네트워크를 확인했습니다. RDS 생성 계획을 진행할 수 있습니다.'
            operation['service_security_group'] = group_id
            operation['stack_id'] = stack_id
            self._save(operation)

    def get(self, application_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(application_id)
            if not operation:
                raise ValueError('이 앱의 네트워크 생성 요청 기록이 없습니다.')
            request = operation['request']
            return {'application_id': application_id, 'account': request['account'],
                    'region': request['region'], 'vpc_id': request['vpc_id'],
                    'status': operation['status'], 'message': operation['message'],
                    'created_at': operation['created_at'],
                    'service_security_group': operation.get('service_security_group'),
                    'stack_id': operation.get('stack_id')}

    def reconcile(self, application_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(application_id)
            if not operation:
                raise ValueError('이 앱의 네트워크 생성 요청 기록이 없습니다.')
            if operation['status'] == 'running':
                raise ValueError('네트워크 생성 작업이 실행 중입니다.')
            request = ServiceNetworkRequest(**operation['request'])
        try:
            network = AwsServiceNetworkProvisioner(request).inspect_current()
            group_id = network['service_security_group']
            stack_id = network['stack_id']
        except (AwsConfigurationError, ValueError, KeyError) as exc:
            with self.lock:
                operation['status'] = 'needs_attention'
                operation['message'] = 'AWS 네트워크 상태를 확정하지 못했습니다: ' + str(exc)
                self._save(operation)
        else:
            with self.lock:
                operation['status'] = 'succeeded'
                operation['message'] = '앱 전용 네트워크를 다시 확인했습니다.'
                operation['service_security_group'] = group_id
                operation['stack_id'] = stack_id
                self._save(operation)
        return self.get(application_id)
