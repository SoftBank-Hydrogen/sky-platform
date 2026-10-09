"""Read-only deployment evidence derived from persisted job records.

This is a snapshot of Sky's own records, not a signed attestation or a
fresh probe of cloud resources. Missing evidence remains explicitly unknown.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from engine.architecture_decision import verify_architecture_decision
from engine.compilation import verify_compilation
from engine.deployment_policy import policy_from_record
from application.consistency import health_result_matches_plan
from application.verification_gates import static_consistency_gate, target_verification_obligations


_SHA256 = re.compile(r'[a-f0-9]{64}')


def _digest(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _items(value: object) -> list | tuple:
    return value if isinstance(value, (list, tuple)) else ()


def _decision_trace(job: dict, infrastructure: dict, compatibility: dict) -> dict:
    """Expose recorded decisions, not unverified source or cloud claims."""
    ir = job.get('application_ir') if isinstance(job.get('application_ir'), dict) else {}
    evidence = _items(ir.get('evidence'))
    requirements = _items(ir.get('requirements'))
    hypotheses = _items(ir.get('hypotheses'))
    constraints = _items(compatibility.get('constraint_results'))
    candidates = _items(infrastructure.get('candidates'))
    source_revision = _digest(job.get('source_digest'))
    ir_revision = _digest(ir.get('source_revision'))
    source_evidence = []
    mismatched_evidence = False
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            continue
        source = item.get('source') if isinstance(item.get('source'), dict) else {}
        path = item.get('path') or source.get('path')
        signal = item.get('signal') or item.get('interpretation') or item.get('observation')
        if not isinstance(path, str) or not isinstance(signal, str):
            continue
        record_revision = source.get('revision')
        if record_revision is not None and record_revision != source_revision:
            mismatched_evidence = True
            continue
        source_evidence.append({
            'id': item['id'], 'path': path, 'signal': signal,
            'origin': item.get('origin'), 'status': item.get('status', 'confirmed'),
            'source_revision': _digest(record_revision),
        })
    evidence_ids = {item['id'] for item in source_evidence}
    referenced_ids = {
        identifier
        for record in (*requirements, *hypotheses, *constraints)
        if isinstance(record, dict)
        for identifier in _items(record.get('evidence_ids'))
        if isinstance(identifier, str)
    }
    selected = [item for item in candidates if isinstance(item, dict) and item.get('selected') is True]
    decision_consistent = (not candidates or len(selected) == 1 and selected[0].get('id') == job.get('target'))
    if infrastructure.get('target') is not None and infrastructure['target'] != job.get('target'):
        decision_consistent = False
    decision = job.get('architecture_decision')
    validated_decision = None
    if decision is not None:
        try:
            verify_architecture_decision(decision, ir,
                                         policy_from_record(job.get('deployment_policy')), infrastructure)
        except (ValueError, TypeError, KeyError):
            decision_consistent = False
        else:
            validated_decision = decision
    return {
        'status': ('recorded' if ir and constraints and decision_consistent
                   and not mismatched_evidence and not (referenced_ids - evidence_ids)
                   and (ir_revision is None or ir_revision == source_revision) else 'incomplete'),
        'applies_to_uploaded_source_sha256': source_revision,
        'ir_source_revision': ir_revision,
        'decision_id': validated_decision['decision_id'] if validated_decision else None,
        'decision_revision': validated_decision['decision_revision'] if validated_decision else None,
        'pending_verification_rule_ids': (validated_decision['pending_verification_rule_ids']
                                           if validated_decision else []),
        'topology_status': ir.get('topology_status', 'unresolved'),
        'source_evidence': source_evidence,
        'unresolved_evidence_ids': sorted(referenced_ids - evidence_ids),
        'requirements': [
            {'id': item['id'], 'kind': item['kind'], 'evidence_ids': item.get('evidence_ids', [])}
            for item in requirements if isinstance(item, dict)
            and isinstance(item.get('id'), str) and isinstance(item.get('kind'), str)
        ],
        'hypotheses': [
            {'id': item['id'], 'kind': item['kind'], 'status': item['status'],
             'evidence_ids': item.get('evidence_ids', [])}
            for item in hypotheses if isinstance(item, dict)
            and isinstance(item.get('id'), str) and isinstance(item.get('kind'), str)
            and item.get('status') == 'inferred'
        ],
        'constraint_results': [
            {'rule_id': item['rule_id'], 'status': item['status'],
             'requirement': item.get('requirement'),
             'evidence_ids': item.get('evidence_ids', []), 'reason': item.get('reason')}
            for item in constraints if isinstance(item, dict)
            and isinstance(item.get('rule_id'), str) and item.get('status') in {'satisfied', 'violated', 'unknown'}
        ],
        'candidate_evaluations': [
            {'target': item['id'], 'status': item['status'], 'selected': item.get('selected') is True,
             'violated_rule_ids': item.get('violated_rule_ids', [])}
            for item in candidates if isinstance(item, dict)
            and isinstance(item.get('id'), str) and isinstance(item.get('status'), str)
        ],
        'selected_target': job.get('target'),
        'selection_basis': infrastructure.get('planner'),
        'note': '업로드한 소스와 작업 시점의 판단 기록입니다. 실행 중 상태의 독립적인 증명이 아닙니다.',
    }


def _gate_snapshot(job: dict, infrastructure: dict, completed: bool) -> dict:
    """Show only a gate record that still matches its stored decision and compilation."""
    gate = job.get('static_consistency_gate')
    if gate is None:
        return {'status': 'unrecorded', 'required_obligations': []}
    try:
        decision = job['architecture_decision']
        compilation = job['compilation']
        checks = job['consistency_checks']
        policy = policy_from_record(job['deployment_policy'])
        verify_compilation(compilation, decision, job['application_ir'], policy, infrastructure)
        expected = static_consistency_gate(compilation, decision, checks)
    except (ValueError, TypeError, KeyError):
        return {'status': 'incomplete', 'required_obligations': []}
    if gate != expected:
        return {'status': 'incomplete', 'required_obligations': []}
    observed = job.get('websocket_verification') if completed else None
    plan = job.get('plan') if isinstance(job.get('plan'), dict) else {}
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    deployment_http_verified = completed and health_result_matches_plan(plan, result)
    obligations = target_verification_obligations(
        gate, observed, deployment_http_verified=deployment_http_verified)
    return {'status': 'recorded', 'static_consistency': gate,
            'required_obligations': obligations,
            'target_verification_status': ('failed' if any(item['status'] == 'failed' for item in obligations)
                                           else 'pending' if any(item['status'] == 'pending' for item in obligations)
                                           else 'evidence_recorded' if obligations else 'not_required')}


def deployment_certificate(job: dict, health_history: list[dict] | None = None) -> dict:
    """Build a safe, explicit evidence snapshot without modifying the job."""
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    plan = job.get('plan') if isinstance(job.get('plan'), dict) else {}
    infrastructure = job.get('infrastructure_plan') if isinstance(job.get('infrastructure_plan'), dict) else {}
    compatibility = infrastructure.get('compatibility') if isinstance(infrastructure.get('compatibility'), dict) else {}
    rehearsal = result.get('rehearsal') if isinstance(result.get('rehearsal'), dict) else {}
    promotion = result.get('promotion') if isinstance(result.get('promotion'), dict) else {}
    image_digest = result.get('image_digest') if isinstance(result.get('image_digest'), str) else None
    registry_digest = image_digest if image_digest and re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest) else None
    completed = job.get('status') == 'succeeded' and bool(result.get('url'))
    rehearsal_passed = bool(completed and rehearsal.get('status') == 'passed'
                            and isinstance(rehearsal.get('image_id'), str)
                            and re.fullmatch(r'sha256:[a-f0-9]{64}', rehearsal['image_id']))
    promoted = bool(completed and registry_digest
                    and isinstance(promotion.get('source_job_id'), str)
                    and re.fullmatch(r'[a-f0-9]{16}', promotion['source_job_id'])
                    and isinstance(promotion.get('image_id'), str)
                    and re.fullmatch(r'sha256:[a-f0-9]{64}', promotion['image_id'])
                    and promotion.get('platform') == 'linux/amd64')
    ai_execution = job.get('ai_model_execution') if isinstance(job.get('ai_model_execution'), dict) else {}
    ai_recorded = bool(completed and ai_execution.get('provider') == 'openai-responses'
                       and isinstance(ai_execution.get('response_id'), str)
                       and re.fullmatch(r'resp_[A-Za-z0-9]+', ai_execution['response_id'])
                       and isinstance(ai_execution.get('model'), str)
                       and re.fullmatch(r'[A-Za-z0-9._-]{1,100}', ai_execution['model'])
                       and type(ai_execution.get('response_count')) is int
                       and ai_execution['response_count'] > 0
                       and isinstance(ai_execution.get('recorded_at'), str))
    history = health_history or []
    latest_health = history[-1] if history else None
    checks = [
        {'name': 'deployment_http', 'status': 'passed' if completed else 'unverified',
         'detail': ('배포 작업이 실제 HTTP 응답을 확인한 뒤 완료로 기록했습니다. 현재 가용성은 별도 검사입니다.'
                    if completed else '완료된 배포의 HTTP 확인 기록이 없습니다.')},
        {'name': 'local_rehearsal', 'status': 'passed' if rehearsal_passed else 'unverified',
         'detail': ('클라우드 업로드 전에 같은 태그의 이미지를 로컬에서 실행해 HTTP 200을 확인했습니다.'
                    if rehearsal_passed else '같은 산출물의 로컬 리허설 결과가 기록되지 않았습니다.')},
        {'name': 'cross_target_promotion', 'status': 'passed' if promoted else 'unverified',
         'detail': ('별도 Local Docker 배포에서 HTTP 검증한 이미지 ID를 AWS 업로드 태그와 대조했습니다.'
                    if promoted else '다른 배포 대상에서 검증한 이미지를 승격한 기록이 없습니다.')},
        {'name': 'registry_manifest', 'status': 'passed' if completed and registry_digest else 'unverified',
         'detail': ('ECR 이미지 태그의 매니페스트 다이제스트를 업로드 후와 배포 후에 확인했습니다.'
                    if completed and registry_digest else '레지스트리 매니페스트 다이제스트 확인 기록이 없습니다.')},
        {'name': 'image_identity', 'status': 'unverified',
         'detail': '실행 중인 ECS 태스크의 이미지 다이제스트는 아직 대조하지 않았습니다.'},
        {'name': 'ai_model_execution', 'status': 'passed' if ai_recorded else 'unverified',
         'detail': ('Sky가 OpenAI Responses API의 완료 응답 ID와 모델명을 작업에 기록했습니다. 독립 서명 검증은 아닙니다.'
                    if ai_recorded else '실제 모델 호출과 고정 응답을 구분하는 출처 기록이 없습니다.')},
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
    local_sqlite = job.get('local_sqlite_binding')
    if isinstance(local_sqlite, dict):
        mounted = bool(completed and result.get('sqlite_volume') == local_sqlite.get('volume_name')
                       and result.get('sqlite_mount') == local_sqlite.get('mount_path'))
        checks.append({'name': 'local_sqlite_mount',
                       'status': 'passed' if mounted else 'unverified',
                       'detail': ('배포 시 컨테이너의 지정 볼륨 연결을 확인했습니다.'
                                  if mounted else '배포 시 SQLite 볼륨 연결 확인 기록이 없습니다.')})
        checks.append({'name': 'data_persistence_after_restart', 'status': 'unverified',
                       'detail': '이 앱의 DB 기록을 재시작 전후에 대조한 작업별 증거가 없습니다.'})

    ir = job.get('application_ir')
    hypotheses = _items(ir.get('hypotheses')) if isinstance(ir, dict) else ()
    if any(item.get('kind') == 'websocket' for item in hypotheses if isinstance(item, dict)):
        websocket_evidence_ids = sorted({identifier for item in hypotheses
            if isinstance(item, dict) and item.get('kind') == 'websocket'
            for identifier in _items(item.get('evidence_ids')) if isinstance(identifier, str)})
        websocket = job.get('websocket_verification') or {}
        recorded = websocket.get('status') if completed else None
        checks.append({'name': 'websocket_round_trip',
                       'status': recorded if recorded in {'passed', 'failed'} else 'unverified',
                       'source_evidence_ids': websocket_evidence_ids,
                       'checked_at': websocket.get('checked_at') if recorded else None,
                       'detail': ('실제 대상에서 sky.probe nonce 왕복을 확인한 당시 기록입니다.'
                                  if recorded == 'passed' else
                                  '실제 대상의 sky.probe 왕복에 실패했습니다.' if recorded == 'failed' else
                                  '실제 대상의 WebSocket 메시지 왕복 기록이 없습니다.')})

    changes = job.get('changes') if isinstance(job.get('changes'), list) else []
    changed_paths = sorted({item['path'] for item in changes
                            if isinstance(item, dict) and isinstance(item.get('path'), str)})
    decision_trace = _decision_trace(job, infrastructure, compatibility)
    valid_evidence_ids = {item['id'] for item in decision_trace['source_evidence']}
    for check in checks:
        if 'source_evidence_ids' in check:
            check['source_evidence_ids'] = [identifier for identifier in check['source_evidence_ids']
                                            if identifier in valid_evidence_ids]
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
                     'local_image_id': (promotion.get('image_id') if promoted else
                                        rehearsal.get('image_id') if rehearsal_passed else None),
                     'promoted_from_job_id': promotion.get('source_job_id') if promoted else None,
                     'registry_manifest_digest': registry_digest},
        'decision_trace': decision_trace,
        'verification_gates': _gate_snapshot(job, infrastructure, completed),
        'verification': checks,
        'unverified': [item['name'] for item in checks if item['status'] == 'unverified'],
        'rollback': {'previous_job_id': job.get('replaces_job_id'),
                     'state': job.get('release_rollback_state'), 'rehearsed': False},
        'cost': {'estimated_total': None, 'actual_total': None,
                 'detail': '이 작업 전체의 비용 견적과 실제 청구액은 기록되지 않았습니다.'},
        'limitations': ['서버의 작업 기록에서 생성한 읽기 전용 스냅샷입니다.',
                        '서명·외부 보관·현재 클라우드 상태의 증거가 아닙니다.'],
    }
