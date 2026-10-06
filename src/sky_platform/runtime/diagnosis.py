"""Conservative deployment diagnosis from persisted job events.

Events identify the last observed phase, not the root cause. This projection never
copies event messages: command output and cloud errors may contain private values.
"""

PHASES = {
    'preparing': ('configuration', '실행 설정 준비'),
    'building': ('image_build', '이미지 빌드'),
    'rehearsal': ('local_rehearsal', '로컬 리허설'),
    'infrastructure': ('infrastructure', '인프라 준비'),
    'uploading': ('registry_upload', '이미지 업로드'),
    'deploying': ('service_rollout', '서비스 배포'),
    'update_submitting': ('service_update', '기존 서비스 업데이트'),
    'update_accepted': ('service_update', '기존 서비스 업데이트'),
    'verifying': ('http_verification', 'HTTP 동작 확인'),
}


def deployment_diagnosis(job: dict) -> dict | None:
    """Return a bounded, read-only explanation for an unfinished deployment."""
    status = job.get('status')
    attention = job.get('deployment_state') == 'needs_attention'
    if status not in {'failed', 'interrupted', 'waiting_input'} and not attention:
        return None
    events = job.get('events') if isinstance(job.get('events'), list) else []
    observed = next((event for event in reversed(events)
                     if isinstance(event, dict) and event.get('stage') in PHASES), None)
    phase, phase_label = PHASES[observed['stage']] if observed else ('unknown', '단계 확인 불가')
    evidence = ([{'stage': observed['stage'], 'time': observed.get('time')}]
                if observed else [])
    if job.get('persistence_failed'):
        issue = 'record_write_failed'
        summary = '작업 기록 저장에 실패했습니다. 실제 배포 상태는 별도로 확인해야 합니다.'
        action = '작업 기록 저장 문제를 해결하고, 대상 환경의 실제 리소스를 확인하세요.'
    elif attention or job.get('aws_update_submitted') and status in {'failed', 'interrupted'}:
        issue = 'cloud_outcome_uncertain'
        summary = '클라우드 변경 요청 후 결과를 확정하지 못했습니다.'
        action = '같은 요청을 다시 보내기 전에 기존 작업의 AWS 상태와 소유 리소스를 재확인하세요.'
    elif status == 'waiting_input':
        issue = 'input_required'
        summary = '배포에 필요한 입력을 기다리고 있습니다.'
        action = '화면에 표시된 필수 환경변수를 입력해 같은 작업을 계속하세요.'
    elif status == 'interrupted':
        issue = 'interrupted'
        summary = '작업이 중단됐고 최종 배포 결과가 확인되지 않았습니다.'
        action = '작업 기록과 대상 환경 상태를 확인한 뒤 제공된 복구 경로를 사용하세요.'
    else:
        issue = 'deployment_failed'
        summary = f'배포가 실패했습니다. 마지막으로 확인된 단계: {phase_label}.'
        action = '작업 내역의 마지막 오류와 해당 단계의 출력을 확인하세요.'
    return {
        'issue': issue,
        'summary': summary,
        'last_observed_phase': phase,
        'evidence': evidence,
        'recommended_action': action,
        'root_cause_verified': False,
        'limitation': '작업 이벤트만 사용했습니다. 실패 원인이나 현재 클라우드 상태를 검증한 결과가 아닙니다.',
    }
