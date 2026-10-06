"""Read-only deployment evidence derived from persisted job records.

This is a snapshot of Sky's own records, not a signed attestation or a
fresh probe of cloud resources. Missing evidence remains explicitly unknown.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


_SHA256 = re.compile(r'[a-f0-9]{64}')


def _digest(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def deployment_certificate(job: dict, health_history: list[dict] | None = None) -> dict:
    """Build a safe, explicit evidence snapshot without modifying the job."""
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    plan = job.get('plan') if isinstance(job.get('plan'), dict) else {}
    infrastructure = job.get('infrastructure_plan') if isinstance(job.get('infrastructure_plan'), dict) else {}
    compatibility = infrastructure.get('compatibility') if isinstance(infrastructure.get('compatibility'), dict) else {}
    rehearsal = result.get('rehearsal') if isinstance(result.get('rehearsal'), dict) else {}
    image_digest = result.get('image_digest') if isinstance(result.get('image_digest'), str) else None
    registry_digest = image_digest if image_digest and re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest) else None
    completed = job.get('status') == 'succeeded' and bool(result.get('url'))
    rehearsal_passed = bool(completed and rehearsal.get('status') == 'passed'
                            and isinstance(rehearsal.get('image_id'), str)
                            and re.fullmatch(r'sha256:[a-f0-9]{64}', rehearsal['image_id']))
    history = health_history or []
    latest_health = history[-1] if history else None
    checks = [
        {'name': 'deployment_http', 'status': 'passed' if completed else 'unverified',
         'detail': ('배포 작업이 실제 HTTP 응답을 확인한 뒤 완료로 기록했습니다. 현재 가용성은 별도 검사입니다.'
                    if completed else '완료된 배포의 HTTP 확인 기록이 없습니다.')},
        {'name': 'local_rehearsal', 'status': 'passed' if rehearsal_passed else 'unverified',
         'detail': ('클라우드 업로드 전에 같은 태그의 이미지를 로컬에서 실행해 HTTP 200을 확인했습니다.'
                    if rehearsal_passed else '같은 산출물의 로컬 리허설 결과가 기록되지 않았습니다.')},
        {'name': 'registry_manifest', 'status': 'passed' if completed and registry_digest else 'unverified',
         'detail': ('ECR 이미지 태그의 매니페스트 다이제스트를 업로드 후와 배포 후에 확인했습니다.'
                    if completed and registry_digest else '레지스트리 매니페스트 다이제스트 확인 기록이 없습니다.')},
        {'name': 'image_identity', 'status': 'unverified',
         'detail': '실행 중인 ECS 태스크의 이미지 다이제스트는 아직 대조하지 않았습니다.'},
        {'name': 'ai_model_execution', 'status': 'unverified',
         'detail': '실제 모델 호출과 고정 응답을 구분하는 출처 기록이 없습니다.'},
        {'name': 'rollback_rehearsal', 'status': 'unverified',
         'detail': '롤백을 실행하고 원래 릴리스로 복귀한 리허설 결과가 없습니다.'},
    ]
    if latest_health:
        checks.append({'name': 'latest_health',
                       'status': 'passed' if latest_health.get('healthy') is True else 'failed',
                       'checked_at': latest_health.get('checked_at'),
                       'detail': '기록된 마지막 상태 검사입니다. 현재 상태를 보증하지 않습니다.'})
    else:
        checks.append({'name': 'latest_health', 'status': 'unverified',
                       'detail': '배포 후 별도 상태 검사 기록이 없습니다.'})
    if infrastructure.get('database') or job.get('postgres'):
        migration = result.get('migration') if isinstance(result.get('migration'), dict) else None
        checks.append({'name': 'schema_migration',
                       'status': 'passed' if completed and migration else 'unverified',
                       'detail': ('완료된 배포 기록에 SQL 마이그레이션 결과가 있습니다.'
                                  if completed and migration else '완료된 SQL 마이그레이션 결과가 없습니다.')})
        checks.append({'name': 'cross_environment_data_migration', 'status': 'unverified',
                       'detail': '환경 간 기존 데이터 이전은 SQL 스키마 마이그레이션과 별도로 검증해야 합니다.'})

    changes = job.get('changes') if isinstance(job.get('changes'), list) else []
    changed_paths = sorted({item['path'] for item in changes
                            if isinstance(item, dict) and isinstance(item.get('path'), str)})
    return {
        'schema_version': 1,
        'kind': 'sky-record-snapshot',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'job': {'id': job.get('id'), 'application_id': job.get('application_id', job.get('id')),
                'status': job.get('status'), 'deployment_state': job.get('deployment_state', 'active'),
                'created_at': job.get('created_at'), 'target': job.get('target', 'local-docker')},
        'source': {'uploaded_sha256': _digest(job.get('source_digest')),
                   'prepared_sha256': _digest(plan.get('source_digest')),
                   'changed_paths': changed_paths, 'change_count': len(changes),
                   'diff_in_job_history': bool(changes or job.get('diff'))},
        'destination': {'region': result.get('region'), 'account': result.get('account'),
                        'project': result.get('project'), 'access_mode': compatibility.get('access_mode'),
                        'url': result.get('url') if completed else None,
                        'service': result.get('service'), 'container': result.get('container'),
                        'planned_resources': infrastructure.get('resources') or []},
        'artifact': {'image_reference': result.get('image'),
                     'local_image_id': rehearsal.get('image_id') if rehearsal_passed else None,
                     'registry_manifest_digest': registry_digest},
        'verification': checks,
        'unverified': [item['name'] for item in checks if item['status'] == 'unverified'],
        'rollback': {'previous_job_id': job.get('replaces_job_id'),
                     'state': job.get('release_rollback_state'), 'rehearsed': False},
        'cost': {'estimated_total': None, 'actual_total': None,
                 'detail': '이 작업 전체의 비용 견적과 실제 청구액은 기록되지 않았습니다.'},
        'limitations': ['서버의 작업 기록에서 생성한 읽기 전용 스냅샷입니다.',
                        '서명·외부 보관·현재 클라우드 상태의 증거가 아닙니다.'],
    }
