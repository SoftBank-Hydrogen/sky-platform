"""Read-only deployment evidence derived from persisted job records.

This is a snapshot of Sky's own records, not a signed attestation or a
fresh probe of cloud resources. Missing evidence remains explicitly unknown.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from application.consistency import health_result_matches_plan
from application.deployment_core import DeploymentPlan
from application.source_transform import executable_plan_digest, resolved_target_plan
from application.verification_gates import static_consistency_gate, target_verification_obligations
from engine.architecture_decision import verify_architecture_decision
from engine.compilation import verify_compilation
from engine.deployment_policy import policy_from_record

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
    deployment_http_verified = (completed
                                and plan.get('target') == compilation['target_plan']['target']
                                and health_result_matches_plan(plan, result))
    obligations = target_verification_obligations(
        gate, observed, deployment_http_verified=deployment_http_verified)
    return {'status': 'recorded', 'static_consistency': gate,
            'required_obligations': obligations,
            'target_verification_status': ('failed' if any(item['status'] == 'failed' for item in obligations)
                                           else 'pending' if any(item['status'] == 'pending' for item in obligations)
                                           else 'unverified' if any(item['status'] == 'unverified' for item in obligations)
                                           else 'evidence_recorded' if obligations else 'not_required')}


def _evidence_chain(job: dict, decision_trace: dict, gate: dict, http_verified: bool) -> dict:
    """Link persisted source, decision, transform, and target evidence without a fresh probe."""
    compilation = job.get('compilation')
    transform = job.get('source_transform')
    plan_record = job.get('plan')
    target_plan = compilation.get('target_plan') if isinstance(compilation, dict) else None
    linked = False
    if isinstance(target_plan, dict) and isinstance(transform, dict) and isinstance(plan_record, dict):
        try:
            plan = DeploymentPlan(**plan_record)
            linked = bool(
                decision_trace['status'] == 'recorded'
                and gate['status'] == 'recorded'
                and http_verified
                and transform.get('schema_version') == 2
                and compilation.get('source_revision') == _digest(job.get('source_digest'))
                and compilation.get('architecture_decision_id') == decision_trace['decision_id']
                and transform.get('compilation_id') == compilation.get('compilation_id')
                and transform.get('decision_revision') == compilation.get('decision_revision')
                and transform.get('source_revision') == compilation.get('source_revision')
                and transform.get('target_plan_id') == target_plan.get('id')
                and transform.get('transformed_source_revision') == plan.source_digest
                and transform.get('executable_plan_digest') == executable_plan_digest(plan)
                and transform.get('resolved_target') == resolved_target_plan(target_plan, plan)
                and plan.target == job.get('target') == target_plan.get('target')
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            pass
    return {
        'status': 'linked' if linked else 'incomplete',
        'source_revision': _digest(job.get('source_digest')),
        'decision_id': decision_trace.get('decision_id'),
        'compilation_id': compilation.get('compilation_id') if isinstance(compilation, dict) else None,
        'target_plan_id': target_plan.get('id') if isinstance(target_plan, dict) else None,
        'transformed_source_revision': (_digest(transform.get('transformed_source_revision'))
                                        if isinstance(transform, dict) else None),
        'verification_ref': 'deployment_http' if linked else None,
    }


def _schema_migration_task_verified(job: dict, result: dict) -> bool:
    """Accept only an owned, journaled successful ECS migration task result."""
    migration = result.get('migration')
    journal = job.get('aws_migration_result')
    aws = job.get('aws')
    if (job.get('target') != 'aws-ecs-express'
            or job.get('aws_migration_status') != 'succeeded'
            or not isinstance(migration, dict) or not isinstance(journal, dict)
            or not isinstance(aws, dict)):
        return False
    job_id, attempts = job.get('id'), job.get('attempts')
    region, account = result.get('region'), result.get('account')
    if (not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{16}', job_id)
            or type(attempts) is not int or not 1 <= attempts <= 3
            or not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}-[a-z]+-\d', region)
            or not isinstance(account, str) or not re.fullmatch(r'\d{12}', account)
            or aws.get('region') != region or aws.get('expected_account') != account):
        return False
    attempt = f'{job_id}-a{attempts}'
    task_prefix = f'arn:aws:ecs:{region}:{account}:task/default/'
    definition_prefix = f'arn:aws:ecs:{region}:{account}:task-definition/sky-migrate-{attempt}:'
    task = migration.get('task_arn')
    definition = migration.get('task_definition_arn')
    image = migration.get('image')
    digest = migration.get('image_digest')
    bundle = migration.get('bundle_digest')
    expected_image = f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed:{attempt}-db'
    return bool(
        isinstance(task, str) and task.startswith(task_prefix)
        and re.fullmatch(r'[a-f0-9]{32}', task.removeprefix(task_prefix))
        and isinstance(definition, str) and definition.startswith(definition_prefix)
        and definition.removeprefix(definition_prefix).isdigit()
        and image == expected_image
        and isinstance(digest, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', digest)
        and isinstance(bundle, str) and re.fullmatch(r'[a-f0-9]{64}', bundle)
        and job.get('aws_migration_bundle_digest') == bundle
        and all(journal.get(key) == migration[key] for key in (
            'task_arn', 'task_definition_arn', 'image', 'image_digest', 'bundle_digest'))
    )


def _sqlite_integrity_evidence(job: dict, result: dict) -> dict | None:
    """Connect the bounded snapshot assertion to an owned successful SQL task.

    This proves assertions at import time, not an independent current DB audit.
    Legacy count-only bundles deliberately have no recognized protocol record.
    """
    if not _schema_migration_task_verified(job, result):
        return None
    conversion = job.get('sqlite_conversion')
    plan = job.get('plan')
    checks = [check for check in _items(job.get('consistency_checks'))
              if isinstance(check, dict) and check.get('id') == 'CV-04'
              and check.get('source') == 'reviewed_sqlite_migration']
    if not isinstance(conversion, dict) or not isinstance(plan, dict) or len(checks) != 1:
        return None
    check = checks[0]
    evidence = check.get('integrity')
    if check.get('status') != 'pass' or not isinstance(evidence, dict):
        return None
    counts, schema = conversion.get('row_counts'), conversion.get('schema')
    if (not isinstance(counts, dict) or not 1 <= len(counts) <= 8
            or not isinstance(schema, dict) or set(schema) != set(counts)
            or any(not isinstance(name, str) or type(count) is not int or count < 0
                   for name, count in counts.items()) or sum(counts.values()) > 1000):
        return None
    try:
        schema_digest = hashlib.sha256(json.dumps(
            schema, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    except (ValueError, TypeError):
        return None
    sql_digest = _digest(evidence.get('sql_sha256'))
    if sql_digest is None:
        return None
    bundle_digest = hashlib.sha256(
        b'0000_sky_sqlite_import.sql\0' + bytes.fromhex(sql_digest)).hexdigest()
    if (evidence.get('protocol') != 'sqlite-snapshot-multiset-v1'
            or _digest(job.get('source_digest')) is None
            or evidence.get('source_revision') != job['source_digest']
            or _digest(plan.get('source_digest')) is None
            or evidence.get('prepared_revision') != plan['source_digest']
            or _digest(conversion.get('source_sha256')) is None
            or evidence.get('snapshot_sha256') != conversion['source_sha256']
            or evidence.get('schema_sha256') != schema_digest
            or evidence.get('row_counts') != counts
            or evidence.get('bundle_digest') != bundle_digest
            or result['migration'].get('bundle_digest') != bundle_digest):
        return None
    # Allowlist metadata; never include source SQL or raw application values.
    return {key: evidence[key] for key in (
        'protocol', 'source_revision', 'prepared_revision', 'snapshot_sha256',
        'sql_sha256', 'schema_sha256', 'bundle_digest', 'row_counts')}


def _runtime_identity_verified(job: dict, result: dict) -> bool:
    evidence = result.get('image_identity')
    origin = result.get('rehearsal') or result.get('promotion')
    if (job.get('target') != 'aws-ecs-express' or not isinstance(evidence, dict)
            or not isinstance(origin, dict) or origin.get('platform') != 'linux/amd64'
            or evidence.get('protocol') != 'ecs-image-identity-v1' or evidence.get('status') != 'passed'):
        return False
    account, region, service = result.get('account'), result.get('region'), result.get('service')
    if (not isinstance(account, str) or not re.fullmatch(r'\d{12}', account)
            or not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}-[a-z]+-\d', region)
            or not isinstance(service, str) or not re.fullmatch(r'sky-[a-f0-9]{16}-a[1-3]', service)):
        return False
    arn = f'arn:aws:ecs:{region}:{account}:service/default/{service}'
    prefix = f'arn:aws:ecs:{region}:{account}:task/default/'
    tasks = evidence.get('task_arns')
    definition = evidence.get('task_definition_arn')
    definition_prefix = f'arn:aws:ecs:{region}:{account}:task-definition/'
    return bool(
        isinstance(tasks, list) and 1 <= len(tasks) <= 100
        and all(isinstance(task, str) and task.startswith(prefix)
                and re.fullmatch(r'[a-f0-9]{32}', task.removeprefix(prefix)) for task in tasks)
        and len(set(tasks)) == len(tasks)
        and isinstance(definition, str) and definition.startswith(definition_prefix)
        and re.fullmatch(r'[A-Za-z0-9_-]+:\d+', definition.removeprefix(definition_prefix))
        and definition == result.get('task_definition_arn')
        and evidence.get('service_arn') == result.get('service_arn') == arn
        and isinstance(evidence.get('checked_at'), str)
        and re.fullmatch(r'sha256:[a-f0-9]{64}', str(evidence.get('local_image_id')))
        and evidence['local_image_id'] == origin.get('image_id')
        and re.fullmatch(r'sha256:[a-f0-9]{64}', str(evidence.get('manifest_digest')))
        and evidence['manifest_digest'] == result.get('image_digest')
        and re.fullmatch(r'sha256:[a-f0-9]{64}', str(evidence.get('platform_manifest_digest')))
        and re.fullmatch(r'sha256:[a-f0-9]{64}', str(evidence.get('config_digest')))
        and evidence['local_image_id'] in {evidence['manifest_digest'],
                                          evidence['platform_manifest_digest'], evidence['config_digest']}
        and isinstance(evidence.get('runtime_digests'), list)
        and 1 <= len(evidence['runtime_digests']) <= 2
        and all(isinstance(digest, str) and digest in {evidence['manifest_digest'],
                                                     evidence['platform_manifest_digest']}
                for digest in evidence['runtime_digests'])
        and evidence.get('image') == result.get('image')
        and isinstance(result.get('image'), str)
        and result['image'].startswith(f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed:')
    )


def _release_rollback_verified(job: dict, result: dict) -> bool:
    """Check the saved result of restoring a prior ECS release, not a rehearsal."""
    record = job.get('release_rollback_verification')
    if (not isinstance(record, dict) or job.get('target') != 'aws-ecs-express'
            or job.get('release_rollback_state') != 'succeeded'
            or job.get('deployment_state') != 'superseded'
            or job.get('release_rollback_restore_pending') is not False
            or job.get('release_rollback_submitted') is not True):
        return False
    account, region = result.get('account'), result.get('region')
    service, owner = result.get('service'), result.get('owner_attempt')
    target_id = job.get('release_rollback_target_id')
    if (not isinstance(account, str) or not re.fullmatch(r'\d{12}', account)
            or not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}-[a-z]+-\d', region)
            or not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner)
            or service != 'sky-' + owner
            or result.get('service_arn') != f'arn:aws:ecs:{region}:{account}:service/default/{service}'
            or not isinstance(target_id, str) or not re.fullmatch(r'[a-f0-9]{16}', target_id)
            or record.get('target_job_id') != target_id
            or record.get('source') not in {'adapter', 'reconcile'}
            or record.get('url') != result.get('url')
            or record.get('image') == result.get('image')
            or not isinstance(result.get('images'), list)
            or record.get('image') not in result['images']):
        return False
    repository = f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed'
    deployment_prefix = f'arn:aws:ecs:{region}:{account}:service-deployment/default/{service}/'
    definition_prefix = f'arn:aws:ecs:{region}:{account}:task-definition/'
    deployment = record.get('service_deployment_arn')
    definition = record.get('task_definition_arn')
    checked_at = record.get('checked_at')
    try:
        timestamp = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(record.get('image'), str)
        and re.fullmatch(re.escape(repository) + r':[a-f0-9]{16}-a[1-3]', record['image'])
        and isinstance(deployment, str) and deployment.startswith(deployment_prefix)
        and re.fullmatch(r'[A-Za-z0-9_-]+', deployment.removeprefix(deployment_prefix))
        and isinstance(definition, str) and definition.startswith(definition_prefix)
        and re.fullmatch(r'[A-Za-z0-9_-]+:\d+', definition.removeprefix(definition_prefix))
        and timestamp.tzinfo is not None
    )


def deployment_certificate(job: dict, health_history: list[dict] | None = None) -> dict:
    """Build a safe, explicit evidence snapshot without modifying the job."""
    if job.get('mode') == 'static_site':
        result = job.get('result') if isinstance(job.get('result'), dict) else {}
        infrastructure = job.get('infrastructure_plan') if isinstance(job.get('infrastructure_plan'), dict) else {}
        compatibility = (infrastructure.get('compatibility')
                         if isinstance(infrastructure.get('compatibility'), dict) else {})
        decision_trace = _decision_trace(job, infrastructure, compatibility)
        compilation_status = 'unrecorded'
        if 'compilation' in job:
            try:
                verify_compilation(job['compilation'], job['architecture_decision'],
                                   job['application_ir'], policy_from_record(job['deployment_policy']),
                                   infrastructure)
            except (ValueError, TypeError, KeyError):
                compilation_status = 'incomplete'
            else:
                compilation_status = 'recorded' if decision_trace['status'] == 'recorded' else 'incomplete'
        complete = (job.get('status') == 'succeeded'
                    and result.get('stack_id') == job.get('static_stack_id')
                    and result.get('source_digest') == job.get('source_digest')
                    and bool(result.get('source_index_sha256')))
        latest = (health_history or [None])[-1]
        verification = [
            {'name': 'deployment_http', 'status': 'passed' if complete else 'unverified',
             'detail': '공개 CloudFront index.html의 SHA-256을 배포 사본과 대조했습니다.' if complete
             else '공개 응답과 원본 해시를 함께 확인한 완료 기록이 없습니다.'},
            {'name': 'latest_health', 'status': ('passed' if latest.get('healthy') else 'failed')
             if latest else 'unverified',
             'detail': '최근 별도 상태 검사 기록입니다.' if latest else '별도 상태 검사 기록이 없습니다.'},
        ]
        return {
            'schema_version': 1, 'kind': 'sky-record-snapshot',
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'job': {'id': job.get('id'), 'application_id': job.get('application_id'),
                    'status': job.get('status'), 'target': job.get('target'),
                    'deployment_state': job.get('deployment_state')},
            'source': {'uploaded_sha256': job.get('source_digest'), 'changed_paths': [],
                       'change_count': 0},
            'destination': {'url': result.get('url') if complete else None,
                            'stack_id': job.get('static_stack_id')},
            'artifact': {'index_sha256': result.get('source_index_sha256') if complete else None},
            'decision_trace': decision_trace,
            'compilation_status': compilation_status,
            'verification': verification,
            'unverified': [item['name'] for item in verification if item['status'] == 'unverified'],
            'cost': {'estimated_total': None, 'actual_total': None},
            'limitations': ['서명되지 않은 작업 기록입니다.',
                            '당시 HTTP 확인은 현재 가용성이나 JS 기능을 보증하지 않습니다.'],
        }
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    plan = job.get('plan') if isinstance(job.get('plan'), dict) else {}
    infrastructure = job.get('infrastructure_plan') if isinstance(job.get('infrastructure_plan'), dict) else {}
    compatibility = infrastructure.get('compatibility') if isinstance(infrastructure.get('compatibility'), dict) else {}
    rehearsal = result.get('rehearsal') if isinstance(result.get('rehearsal'), dict) else {}
    promotion = result.get('promotion') if isinstance(result.get('promotion'), dict) else {}
    image_digest = result.get('image_digest') if isinstance(result.get('image_digest'), str) else None
    registry_digest = image_digest if image_digest and re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest) else None
    completed = job.get('status') == 'succeeded' and bool(result.get('url'))
    # Legacy jobs may lack a saved executable endpoint. Modern compilations must
    # agree with the actual adapter health result before the report claims HTTP.
    endpoint_recorded = all(key in plan for key in ('target', 'port', 'health_path'))
    legacy_http = not endpoint_recorded and not isinstance(job.get('compilation'), dict)
    matched_http = (endpoint_recorded and plan.get('target') == job.get('target')
                    and health_result_matches_plan(plan, result))
    http_verified = bool(completed and (legacy_http or matched_http))
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
        {'name': 'deployment_http', 'status': 'passed' if http_verified else 'unverified',
         'detail': ('배포 작업이 실제 HTTP 응답을 확인한 뒤 완료로 기록했습니다. 현재 가용성은 별도 검사입니다.'
                    if http_verified else '실행 계획과 일치하는 배포 HTTP 확인 기록이 없습니다.')},
        {'name': 'local_rehearsal', 'status': 'passed' if rehearsal_passed else 'unverified',
         'detail': ('클라우드 업로드 전에 같은 태그의 이미지를 로컬에서 실행해 HTTP 200을 확인했습니다.'
                    if rehearsal_passed else '같은 산출물의 로컬 리허설 결과가 기록되지 않았습니다.')},
        {'name': 'cross_target_promotion', 'status': 'passed' if promoted else 'unverified',
         'detail': ('별도 Local Docker 배포에서 HTTP 검증한 이미지 ID를 AWS 업로드 태그와 대조했습니다.'
                    if promoted else '다른 배포 대상에서 검증한 이미지를 승격한 기록이 없습니다.')},
        {'name': 'registry_manifest', 'status': 'passed' if completed and registry_digest else 'unverified',
         'detail': ('ECR 이미지 태그의 매니페스트 다이제스트를 업로드 후와 배포 후에 확인했습니다.'
                    if completed and registry_digest else '레지스트리 매니페스트 다이제스트 확인 기록이 없습니다.')},
        {'name': 'image_identity',
         'status': 'passed' if completed and _runtime_identity_verified(job, result) else 'unverified',
         'detail': ('리허설 이미지 config → ECR 매니페스트 → 실행 ECS 태스크 다이제스트의 일치를 확인했습니다. '
                    '배포 당시의 기록이며 현재 태스크 재조회는 아닙니다.'
                    if completed and _runtime_identity_verified(job, result) else
                    '리허설 이미지부터 실행 중인 ECS 태스크까지 연결한 확인 기록이 없습니다.')},
        {'name': 'ai_model_execution', 'status': 'passed' if ai_recorded else 'unverified',
         'detail': ('Sky가 OpenAI Responses API의 완료 응답 ID와 모델명을 작업에 기록했습니다. 독립 서명 검증은 아닙니다.'
                    if ai_recorded else '실제 모델 호출과 고정 응답을 구분하는 출처 기록이 없습니다.')},
        {'name': 'rollback_rehearsal', 'status': 'unverified',
         'detail': '롤백을 실행하고 원래 릴리스로 복귀한 리허설 결과가 없습니다.'},
    ]
    if job.get('release_rollback_state') is not None or job.get('release_rollback_verification') is not None:
        rollback_status = ('passed' if _release_rollback_verified(job, result) else
                           'failed' if job.get('release_rollback_state') == 'failed' else 'unverified')
        checks.append({'name': 'release_rollback_execution', 'status': rollback_status,
                       'checked_at': ((job.get('release_rollback_verification') or {}).get('checked_at')
                                      if rollback_status == 'passed' else None),
                       'detail': ('이전 ECS 릴리스 복귀와 HTTP 확인 기록이 작업에 남아 있습니다. 현재 상태의 재확인은 아닙니다.'
                                  if rollback_status == 'passed' else
                                  '이전 릴리스 복귀에 실패했습니다.' if rollback_status == 'failed' else
                                  '이전 릴리스 복귀의 검증 결과가 확정되지 않았습니다.')})
    if latest_health:
        checks.append({'name': 'latest_health',
                       'status': 'passed' if latest_health.get('healthy') is True else 'failed',
                       'checked_at': latest_health.get('checked_at'),
                       'detail': '기록된 마지막 상태 검사입니다. 현재 상태를 보증하지 않습니다.'})
    else:
        checks.append({'name': 'latest_health', 'status': 'unverified',
                       'detail': '배포 후 별도 상태 검사 기록이 없습니다.'})
    if infrastructure.get('database') or job.get('postgres'):
        migration_verified = bool(completed and _schema_migration_task_verified(job, result))
        checks.append({'name': 'schema_migration',
                       'status': 'passed' if migration_verified else 'unverified',
                       'detail': ('소유한 ECS 일회성 SQL 태스크의 성공 결과와 작업 기록이 일치합니다. DB 내용을 독립 조회한 증거는 아닙니다.'
                                  if migration_verified else '소유한 ECS SQL 태스크의 성공 기록을 확인할 수 없습니다.')})
        integrity = _sqlite_integrity_evidence(job, result) if completed else None
        checks.append({'name': 'cross_environment_data_migration',
                       'status': 'passed' if integrity else 'unverified',
                       'evidence': integrity,
                       'detail': ('승인한 SQLite 스냅샷의 스키마·행 수·값·중복 검사가 SQL 이전 트랜잭션에서 통과했습니다. '
                                  '현재 DB 재조회나 이후 앱 쓰기 결과의 검증은 아닙니다.' if integrity else
                                  '승인한 스냅샷의 값 검증과 실제 SQL 태스크를 연결한 기록이 없습니다.')})
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
        checks.append({'name': 'websocket_session_continuity', 'status': 'unverified',
                       'detail': '새 연결의 메시지 왕복은 기존 플레이어 연결·진행 중인 게임 상태의 유지를 증명하지 않습니다. '
                                 '이 배포 리비전에 연결된 갱신·재시작·롤백 세션 증거가 필요합니다.'})

    changes = job.get('changes') if isinstance(job.get('changes'), list) else []
    changed_paths = sorted({item['path'] for item in changes
                            if isinstance(item, dict) and isinstance(item.get('path'), str)})
    decision_trace = _decision_trace(job, infrastructure, compatibility)
    valid_evidence_ids = {item['id'] for item in decision_trace['source_evidence']}
    for check in checks:
        if 'source_evidence_ids' in check:
            check['source_evidence_ids'] = [identifier for identifier in check['source_evidence_ids']
                                            if identifier in valid_evidence_ids]
    gate_snapshot = _gate_snapshot(job, infrastructure, completed)
    evidence_chain = _evidence_chain(job, decision_trace, gate_snapshot, http_verified)
    unresolved_checks = [item['check_id'] for item in gate_snapshot['required_obligations']
                         if item['status'] in {'pending', 'unverified'}]
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
        'verification_gates': gate_snapshot,
        'evidence_chain': evidence_chain,
        'verification': checks,
        'unverified': [item['name'] for item in checks if item['status'] == 'unverified'] + unresolved_checks,
        'rollback': {'previous_job_id': job.get('replaces_job_id'),
                     'state': job.get('release_rollback_state'), 'rehearsed': False,
                     'target_job_id': job.get('release_rollback_target_id')},
        'cost': {'estimated_total': None, 'actual_total': None,
                 'detail': '이 작업 전체의 비용 견적과 실제 청구액은 기록되지 않았습니다.'},
        'limitations': ['서버의 작업 기록에서 생성한 읽기 전용 스냅샷입니다.',
                        '서명·외부 보관·현재 클라우드 상태의 증거가 아닙니다.'],
    }
