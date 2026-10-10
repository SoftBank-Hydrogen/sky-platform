from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from assets import ASSET_ROOT
from engine.application_ir import application_ir
from engine.architecture_decision import architecture_decision, verify_architecture_decision
from engine.compilation import compile_decision, verify_compilation
from engine.capability_registry import target_capability_model
from engine.candidates import compare_targets, static_hosting_candidate
from engine.static_site import assess_static_site
from engine.deployment_policy import deployment_policy, policy_from_record
from application.analysis import AISettings, analyze_project, redact
from application.agent import DeploymentAgent, DeploymentCancelled, DeploymentTools, NeedsEnvironment, OpenAIDeployAgent
from adapters.aws.ecs import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from adapters.aws.static_site import AwsStaticSiteAdapter
from adapters.aws.network import ServiceNetworkRequest, discover_default_network
from application.certificate import deployment_certificate
from application.diagnosis import deployment_diagnosis
from application.github_deployments import GitHubDeploymentsMixin
from adapters.gcp.cloud_run import CloudRunAdapter, CloudRunSettings
from application.deployment_core import MAX_UPLOAD, DeploymentPlan, extract_project, folder_upload_to_zip, source_digest, validate_environment
from application.source_transform import source_transform_record, verify_source_transform
from application.client_urls import check_browser_client_urls
from application.consistency import (
    check_database_consistency, check_port_consistency, check_source_change_scope,
    check_target_resource_consistency, check_websocket_state_consistency, require_health_result)
from application.source_secrets import (reject_plaintext_cloud_secret_names,
                                        reject_plaintext_cloud_secrets, reject_supplied_secrets_in_source)
from application.verification_gates import static_consistency_gate
from adapters.local.docker import LocalDockerAdapter
from adapters.local.compose import LocalComposeAdapter
from adapters.onprem.vm import RemoteVmComposeAdapter, VmSettings
from application.health import check_deployment
from application.monitoring import MonitoringMixin
from application.static_deployments import StaticDeploymentsMixin
from application.infrastructure import (OpenAIInfrastructurePlanner,
                                      deployment_access_mode, explicit_infrastructure_plan,
                                      infrastructure_compatibility,
                                      inspect_infrastructure,
                                      plan_infrastructure, preflight_sqlite_conversion,
                                      validate_infrastructure)
from application.local_sqlite import preflight_local_sqlite
from adapters.database.migrations import collect_sql_migrations
from application.network_operations import NetworkOperations
from adapters.aws.postgres import (AwsPostgresProvisioner, PostgresRequest,
                                discover_existing_postgres, inspect_postgres_backup_status,
                                postgres_settings_for_application)
from application.postgres_operations import PostgresOperations
from application.postgres_retirement_operations import PostgresRetirementOperations
from application.snapshot_operations import SnapshotOperations
from adapters.state.directory import StateDirectoryLock
from application.state_recovery import StateRecoveryMixin, postgres_request_from_job


def dockerfile_diff(source: Path, plan: dict) -> str:
    previous = (source / "Dockerfile").read_text() if plan.get("dockerfile_source") == "existing" else ""
    return "".join(difflib.unified_diff(previous.splitlines(keepends=True),
                                        plan["dockerfile"].splitlines(keepends=True),
                                        fromfile="Dockerfile (uploaded)" if previous else "/dev/null",
                                        tofile="Dockerfile"))


class App(GitHubDeploymentsMixin, StateRecoveryMixin, MonitoringMixin, StaticDeploymentsMixin):
    def __init__(self, root: Path, ai_settings: AISettings | None = None, agent_factory=OpenAIDeployAgent,
                 cloud_settings: CloudRunSettings | None = None, aws_settings: AwsSettings | None = None,
                 monitor_interval: int = 300, infrastructure_planner_factory=OpenAIInfrastructurePlanner,
                 github_poll_interval: int = 60):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}
        self.active_groups = set()
        self.health_history = {}
        self.monitor_errors = {}
        if type(monitor_interval) is not int or (monitor_interval != 0 and not 60 <= monitor_interval <= 3600):
            raise ValueError('Monitoring interval must be 0 or 60–3600 seconds')
        self.monitor_interval = monitor_interval
        if type(github_poll_interval) is not int or (github_poll_interval != 0 and
                                                     not 60 <= github_poll_interval <= 3600):
            raise ValueError('GitHub polling interval must be 0 or 60–3600 seconds')
        self.github_poll_interval = github_poll_interval
        self.ai_settings = ai_settings if ai_settings is not None else AISettings.from_environment()
        self.agent_factory = agent_factory
        self.infrastructure_planner_factory = infrastructure_planner_factory
        self.cloud_settings = cloud_settings if cloud_settings is not None else CloudRunSettings.from_environment()
        self.aws_settings = aws_settings if aws_settings is not None else AwsSettings.from_environment()
        self.recovery_warnings = []
        self.postgres_operations = PostgresOperations(self.root / 'database-operations', self.aws_settings,
            max_baseline_730h_usd=os.environ.get('SKY_MAX_RDS_730H_USD'))
        self.recovery_warnings.extend(self.postgres_operations.recovery_warnings)
        self.postgres_retirement_operations = PostgresRetirementOperations(
            self.root / 'database-retirement-operations', self.aws_settings)
        self.recovery_warnings.extend(self.postgres_retirement_operations.recovery_warnings)
        self.snapshot_operations = SnapshotOperations(self.root / 'snapshot-operations', self.aws_settings)
        self.recovery_warnings.extend(self.snapshot_operations.recovery_warnings)
        self.network_operations = NetworkOperations(self.root / 'network-operations', self.aws_settings)
        self.recovery_warnings.extend(self.network_operations.recovery_warnings)
        self.restore()
        self.restore_github_sources()




    def summaries(self):
        with self.lock:
            return [{"id": job["id"], "status": job["status"], "created_at": job.get("created_at"),
                     "application_id": job.get("application_id", job["id"]),
                     "deployment_state": job.get("deployment_state", "active"),
                     "release_rollback_state": job.get('release_rollback_state'),
                     "analyzer": (job.get("plan") or {}).get("analyzer", job.get("mode", "static")),
                     "result": job.get("result"),
                     "last_health": (self.health_history.get(job['id']) or [None])[-1],
                     "monitor_error": self.monitor_errors.get(job['id'])}
                    for job in sorted(self.jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)]

    def releases(self, application_id):
        with self.lock:
            return [{"id": job["id"], "status": job["status"], "target": job.get("target", "local-docker"),
                     "created_at": job.get("created_at"), "result": job.get("result"),
                     "deployment_state": job.get("deployment_state", "active"),
                     "release_rollback_state": job.get('release_rollback_state')}
                    for job in sorted(self.jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)
                    if job.get("application_id", job["id"]) == application_id]

    def deployment_group(self, group_id):
        with self.lock:
            children = sorted((job for job in self.jobs.values() if job.get('group_id') == group_id),
                              key=lambda job: job['group_order'])
            if not children:
                raise ValueError('배포 묶음을 찾을 수 없습니다.')
            states = [job['status'] for job in children]
            if 'waiting_input' in states:
                status = 'waiting_input'
            elif 'running' in states:
                status = 'running'
            elif 'planned' in states:
                status = 'interrupted' if 'interrupted' in states else 'running'
            elif all(state == 'succeeded' for state in states):
                status = 'succeeded'
            elif 'succeeded' in states:
                status = 'partial_success'
            elif 'interrupted' in states:
                status = 'interrupted'
            else:
                status = 'failed'
            return {'id': group_id, 'application_id': children[0]['application_id'],
                    'status': status, 'source_digest': children[0]['source_digest'],
                    'targets': [{'target': job['target'], 'job_id': job['id'],
                                 'status': job['status'],
                                 'promoted_from': (job.get('result') or {}).get('promotion', {}).get('source_job_id'),
                                 'deployment_state': job.get('deployment_state'),
                                 'url': (job.get('result') or {}).get('url')
                                 if job.get('deployment_state', 'active') == 'active' else None}
                                for job in children]}

    def start_group_worker(self, group_id):
        with self.lock:
            if group_id in self.active_groups:
                return False
            self.active_groups.add(group_id)
        try:
            threading.Thread(target=self.run_group, args=(group_id,), daemon=True).start()
        except Exception:
            with self.lock:
                self.active_groups.discard(group_id)
            raise
        return True

    def run_group(self, group_id):
        with self.lock:
            self.active_groups.add(group_id)
        try:
            self._run_group(group_id)
        finally:
            with self.lock:
                self.active_groups.discard(group_id)

    def _run_group(self, group_id):
        with self.lock:
            children = sorted((job for job in self.jobs.values() if job.get('group_id') == group_id),
                              key=lambda job: job['group_order'])
        for child in children:
            with self.lock:
                current = self.jobs[child['id']]
                if current['status'] == 'waiting_input':
                    return
                if current['status'] != 'planned':
                    continue
                current['status'] = 'running'
                self.save(current['id'])
            try:
                prior_local = next((job for job in children
                                    if job['group_order'] < child['group_order']
                                    and job['target'] == 'local-docker'
                                    and job['status'] == 'succeeded'), None)
                if (child['target'] == 'aws-ecs-express' and prior_local
                        and (prior_local.get('result') or {}).get('image_id')):
                    self.run_promoted_aws(child['id'], prior_local['id'])
                else:
                    self.run_agent(child['id'])
            except Exception as exc:
                with self.lock:
                    current = self.jobs[child['id']]
                    current['status'] = 'interrupted'
                    current.setdefault('events', []).append({
                        'time': datetime.now(timezone.utc).isoformat(), 'stage': 'interrupted',
                        'message': '배포 실행기가 중단됐습니다. 대상 상태를 확인하세요: '
                                   + redact(str(exc))[:200]})
                    self.save(child['id'])
                return
            with self.lock:
                if self.jobs[child['id']]['status'] == 'waiting_input':
                    return

    def resume_promoted_aws(self, job_id: str, environment: dict) -> None:
        source_job_id = self.jobs[job_id].get('promotion_source_job_id')
        try:
            self.run_promoted_aws(job_id, source_job_id, environment)
        finally:
            group_id = self.jobs[job_id].get('group_id')
            if group_id and self.jobs[job_id].get('status') != 'waiting_input':
                self.start_group_worker(group_id)

    def run_promoted_aws(self, job_id: str, local_job_id: str,
                         environment: dict | None = None) -> None:
        """Deploy a verified Local image to AWS without editing or rebuilding it."""
        job = self.jobs[job_id]
        local = self.jobs.get(local_job_id) if isinstance(local_job_id, str) else None
        attempt_id = f'{job_id}-a1'
        adapter = None
        submitted_environment = environment
        validated_environment = None
        try:
            if self.cancel_requested(job_id):
                raise DeploymentCancelled()
            if 'architecture_decision' in job and 'deployment_policy' not in job:
                raise ValueError('Architecture decision requires a deployment policy')
            if 'deployment_policy' in job:
                policy = policy_from_record(job['deployment_policy'])
                policy.require(
                    job.get('target'), (job.get('infrastructure_plan') or {}).get('compatibility', {}).get('access_mode'))
                if 'architecture_decision' in job:
                    verify_architecture_decision(job['architecture_decision'], job.get('application_ir'),
                                                 policy, job.get('infrastructure_plan'))
                if 'compilation' in job:
                    verify_compilation(job['compilation'], job['architecture_decision'],
                                       job.get('application_ir'), policy, job.get('infrastructure_plan'))
            if (not isinstance(local, dict) or job.get('target') != 'aws-ecs-express'
                    or job.get('status') != 'running' or job.get('attempts') != 0
                    or not job.get('group_id') or local.get('group_id') != job['group_id']
                    or local.get('target') != 'local-docker'
                    or local.get('group_order', -1) >= job.get('group_order', -1)
                    or local.get('status') != 'succeeded'):
                raise ValueError('같은 배포 묶음에서 성공한 Local 작업만 AWS에 승격할 수 있습니다.')
            project = Path(job['project'])
            work = self.root / job_id / 'work'
            local_work = self.root / local_job_id / 'work'
            local_plan = local.get('plan') or {}
            result = local.get('result') or {}
            local_attempt = result.get('image', '').removeprefix('sky/').removesuffix(':latest')
            if (source_digest(project) != job['source_digest']
                    or not local_plan.get('source_digest')
                    or source_digest(local_work) != local_plan['source_digest']
                    or result.get('platform') != 'linux/amd64'
                    or local_attempt != f"{local_job_id}-a{local.get('attempts')}"):
                raise ValueError('로컬 검증 산출물의 소스 또는 이미지 출처를 확인할 수 없습니다.')
            validate_infrastructure(inspect_infrastructure(local_work), 'aws-ecs-express')
            if work.exists():
                if (work.is_symlink() or not work.is_dir()
                        or job.get('promotion_source_job_id') != local_job_id
                        or job.get('work_digest') != source_digest(work)):
                    raise ValueError('입력 대기 이후 AWS 작업용 소스의 무결성을 확인할 수 없습니다.')
            else:
                if job.get('promotion_source_job_id') is not None:
                    raise ValueError('AWS 승격 작업용 소스가 사라졌습니다. 새 배포를 시작하세요.')
                shutil.copytree(local_work, work)
            if source_digest(work) != local_plan['source_digest']:
                raise ValueError('AWS로 복사한 작업용 소스가 로컬 검증 소스와 다릅니다.')
            plan = DeploymentPlan(**{**local_plan, 'target': 'aws-ecs-express'})
            if job.get('plan') is not None and job['plan'] != asdict(plan):
                raise ValueError('입력 대기 이후 AWS 배포 계획이 변경됐습니다.')
            if 'compilation' in job:
                if job.get('source_transform') is not None:
                    verify_source_transform(job['source_transform'], job['compilation'],
                                            project, work, plan)
                else:
                    job['source_transform'] = source_transform_record(
                        job['compilation'], project, work, plan)
                checks = [
                    check_database_consistency(job['infrastructure_plan'], inspect_infrastructure(work)),
                    check_source_change_scope(job['source_transform'], project,
                                              npm_lock_sync=local.get('npm_lock_sync')),
                    check_port_consistency(plan),
                    check_target_resource_consistency(job['compilation'], job['infrastructure_plan'], job['target']),
                ]
                state_check = check_websocket_state_consistency(job['compilation'])
                if state_check is not None:
                    checks.append(state_check)
                gate = static_consistency_gate(job['compilation'], job['architecture_decision'], checks)
                if job.get('static_consistency_gate') is not None and job['static_consistency_gate'] != gate:
                    raise ValueError('저장된 정적 검증 게이트가 컴파일 결과와 다릅니다.')
                job['consistency_checks'] = checks
                job['static_consistency_gate'] = gate
            promotion = {'source_job_id': local_job_id, 'attempt_id': local_attempt,
                         'image': result['image'], 'image_id': result['image_id'],
                         'platform': result['platform']}
            reject_plaintext_cloud_secret_names(plan.required_env, 'aws-ecs-express')
            if plan.required_env and environment is None:
                with self.lock:
                    if job.get('cancel_requested'):
                        raise DeploymentCancelled()
                    job.update(plan=asdict(plan), diff=dockerfile_diff(project, asdict(plan)),
                               promotion_source_job_id=local_job_id, work_digest=source_digest(work),
                               npm_lock_sync=local.get('npm_lock_sync'),
                               status='waiting_input', missing_environment=sorted(plan.required_env),
                               input_reason='AWS에 사용할 환경변수 값을 다시 입력하세요. Sky 작업 기록에는 저장하지 않지만 AWS 서비스 설정에 전달됩니다.')
                    self.save(job_id)
                self.event(job_id, 'waiting_input', 'AWS 승격에 필요한 환경변수 입력을 기다립니다.')
                return
            validated_environment = validate_environment(environment, plan.required_env)
            if set(validated_environment) != set(plan.required_env):
                raise ValueError('AWS 승격에는 계획에 선언된 환경변수만 입력하세요.')
            reject_supplied_secrets_in_source(work, validated_environment)
            reject_plaintext_cloud_secrets(validated_environment, 'aws-ecs-express')
            check_browser_client_urls(validated_environment, 'aws-ecs-express')
            with self.lock:
                if job.get('cancel_requested'):
                    raise DeploymentCancelled()
                job['plan'] = asdict(plan)
                job['diff'] = dockerfile_diff(project, job['plan'])
                job['promotion_source_job_id'] = local_job_id
                job['work_digest'] = source_digest(work)
                job['npm_lock_sync'] = local.get('npm_lock_sync')
                job['attempts'] = 1
                self.save(job_id)
            self.event(job_id, 'promotion', '로컬 HTTP 검증을 통과한 이미지를 AWS로 승격합니다.')
            context = self.root / job_id / 'attempt-1'
            shutil.copytree(work, context)
            def checkpoint(**updates):
                with self.lock:
                    job.update(updates)
                    self.save(job_id)
            adapter = AwsExpressAdapter(lambda stage, message: self.event(job_id, stage, message),
                                        AwsSettings(**job['aws']), existing=job.get('prior_result'),
                                        checkpoint=checkpoint, promoted_image=promotion)
            result = adapter.deploy(context, plan, attempt_id, validated_environment)
            if 'compilation' in job:
                require_health_result(asdict(plan), result)
            with self.lock:
                job.update(status='succeeded', result=result, deployment_state='active',
                           missing_environment=[], input_reason=None)
                self.save(job_id)
                previous = self.jobs.get(job.get('replaces_job_id'))
                if previous and previous.get('deployment_state', 'active') == 'active':
                    if result.get('previous_task_definition_arn') and previous.get('result'):
                        previous['result']['task_definition_arn'] = result['previous_task_definition_arn']
                    previous['deployment_state'] = 'superseded'
                    self.save(previous['id'])
            self.event(job_id, 'succeeded', '동일 이미지 승격 및 AWS HTTP 응답 확인 완료')
        except DeploymentCancelled:
            with self.lock:
                job.update(status='cancelled', cancel_requested=False)
                self.save(job_id)
            self.event(job_id, 'cancelled', 'AWS 승격 시작 전에 취소됐습니다.')
        except Exception as exc:
            message = str(exc)
            values = (validated_environment or submitted_environment or {}).values()
            for value in sorted(set(values), key=len, reverse=True):
                if value:
                    message = message.replace(value, '[REDACTED]')
            self.event(job_id, 'error', redact(message)[:500])
            if adapter is not None:
                try:
                    adapter.cleanup_failure(attempt_id)
                except Exception:
                    self.event(job_id, 'cleanup', 'AWS 실패 리소스 정리 결과를 확인하지 못했습니다.')
            with self.lock:
                job['status'] = 'failed'
                if adapter is not None and adapter.updated_existing:
                    job['aws_update_submitted'] = True
                    job['aws_update_failed_at'] = datetime.now(timezone.utc).isoformat()
                    previous = self.jobs.get(job.get('replaces_job_id'))
                    if previous:
                        previous['deployment_state'] = 'needs_attention'
                        self.save(previous['id'])
                self.save(job_id)
        finally:
            if validated_environment is not None:
                validated_environment.clear()
            if submitted_environment is not None:
                submitted_environment.clear()

    def create_deployment_group(self, project, application_id, targets, public, source=None):
        """Reserve stateless target jobs from one checked upload before starting any adapter."""
        if (not isinstance(targets, list) or len(targets) < 2 or len(targets) > 3
                or len(set(targets)) != len(targets)
                or any(target not in {'local-docker', 'aws-ecs-express', 'cloud-run'}
                       for target in targets)):
            raise ValueError('서로 다른 배포 대상 2~3개를 선택하세요.')
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,30}', application_id):
            raise ValueError('올바른 앱 ID가 필요합니다.')
        policy = deployment_policy(tuple(targets), public)
        profile = inspect_infrastructure(project)
        digest = source_digest(project)
        plans = []
        for target in targets:
            reason = (self.cloud_settings.unavailable_reason() if target == 'cloud-run' else
                      self.aws_settings.unavailable_reason() if target == 'aws-ecs-express' else None)
            if reason:
                raise ValueError(f'{target}: {reason}')
            validate_infrastructure(profile, target)
            plan = explicit_infrastructure_plan(target, profile)
            plan['compatibility'] = infrastructure_compatibility(profile, target, public_access=public)
            if plan['compatibility']['access_mode'] is None:
                raise ValueError(f'{target}의 공개 접근 설정을 지원하지 않습니다.')
            policy.require(target, plan['compatibility']['access_mode'])
            plans.append(plan)
        group_id = uuid.uuid4().hex[:16]
        jobs = []
        created = []
        try:
            for order, (target, plan) in enumerate(zip(targets, plans)):
                job_id = uuid.uuid4().hex[:16]
                directory = self.root / job_id
                directory.mkdir()
                created.append(directory)
                (directory / '.uncommitted-upload').touch(mode=0o600)
                copied_source = directory / 'source'
                shutil.copytree(project, copied_source)
                if source_digest(copied_source) != digest:
                    raise ValueError('다중 대상 작업용 소스가 업로드 원본과 다릅니다.')
                job = {'id': job_id, 'mode': 'agent', 'target': target,
                       'requested_target': target, 'infrastructure_plan': plan,
                       'application_id': application_id, 'public': plan['compatibility']['access_mode'] == 'public',
                       'status': 'planned', 'created_at': datetime.now(timezone.utc).isoformat(),
                       'plan': None, 'diff': '', 'changes': [], 'steps': 0, 'attempts': 0,
                       'project': str(copied_source), 'infrastructure_profile': profile.as_dict(),
                       'application_ir': application_ir(profile, digest).as_dict(),
                       'deployment_policy': policy.as_dict(),
                       'events': [], 'source_digest': digest,
                       'group_id': group_id, 'group_order': order}
                job['architecture_decision'] = architecture_decision(
                    job['application_ir'], policy, plan).as_dict()
                job['compilation'] = compile_decision(
                    job['architecture_decision'], job['application_ir'], policy, plan)
                if target == 'cloud-run':
                    job['cloud'] = asdict(self.cloud_settings)
                elif target == 'aws-ecs-express':
                    job['aws'] = asdict(self.aws_settings)
                if source is not None:
                    job['github_source'] = source
                jobs.append(job)
            with self.lock:
                for target in targets:
                    self.ensure_application_available(application_id, target)
                for job in jobs:
                    if job['target'] == 'local-docker' and source and source.get('subscription_id'):
                        previous = [old for old in self.jobs.values()
                                    if old.get('application_id') == application_id
                                    and old.get('target') == 'local-docker'
                                    and old.get('status') == 'succeeded'
                                    and old.get('deployment_state', 'active') == 'active'
                                    and old.get('github_source', {}).get('subscription_id') == source['subscription_id']]
                        if previous:
                            job['git_replaces_local_job_id'] = max(
                                previous, key=lambda item: item.get('created_at', ''))['id']
                    if job['target'] == 'aws-ecs-express':
                        previous = [old for old in self.jobs.values()
                                    if old.get('application_id') == application_id
                                    and old.get('target') == 'aws-ecs-express'
                                    and old.get('status') == 'succeeded'
                                    and old.get('deployment_state', 'active') == 'active'
                                    and old.get('result')]
                        if previous:
                            latest = max(previous, key=lambda item: item.get('created_at', ''))
                            if latest['result'].get('database') is not None:
                                raise ValueError('기존 PostgreSQL AWS 서비스는 다중 대상 배포로 업데이트할 수 없습니다.')
                            job['prior_result'] = latest['result']
                            job['replaces_job_id'] = latest['id']
                for job in jobs:
                    self.jobs[job['id']] = job
                    self.save(job['id'])
            for directory in created:
                self.clear_upload_marker(directory)
            return self.deployment_group(group_id)
        except Exception:
            with self.lock:
                for job in jobs:
                    self.jobs.pop(job['id'], None)
            for directory in created:
                shutil.rmtree(directory, ignore_errors=True)
                if directory.exists():
                    self.recovery_warnings.append('다중 대상 접수 실패 파일 정리 필요: ' + directory.name)
            raise

    def ensure_application_available(self, application_id, target):
        # The caller holds self.lock while reserving the new job.
        if target in {'auto', 'aws-ecs-express'} and self.postgres_retirement_operations.blocks_deployment(application_id):
            raise ValueError(f'{application_id}의 PostgreSQL 폐기 기록이 있어 AWS 배포를 시작할 수 없습니다.')
        if any(job.get("application_id") == application_id and job.get("target") == target
               and job.get("status") in {"planned", "provisioning", "running", "waiting_input"} for job in self.jobs.values()):
            raise ValueError(f"{application_id}의 {target} 배포가 이미 진행 중입니다.")
        if any(job.get("application_id") == application_id and job.get("target") == target
               and job.get("deployment_state") in {"deleting", "needs_attention"} for job in self.jobs.values()):
            raise ValueError(f"{application_id}의 {target} 서비스 상태를 먼저 확인해야 합니다.")
        if any(job.get('application_id') == application_id and job.get('target') == target
               and job.get('aws_image_cleanup_state') == 'running' for job in self.jobs.values()):
            raise ValueError(f'{application_id}의 실패 이미지 정리가 진행 중입니다.')
        if any(job.get('application_id') == application_id and job.get('target') == target
               and job.get('release_rollback_state') == 'running' for job in self.jobs.values()):
            raise ValueError(f'{application_id}의 이전 릴리스 롤백이 진행 중입니다.')

    def event(self, job_id, stage, message):
        with self.lock:
            job = self.jobs[job_id]
            job["events"].append({"time": datetime.now(timezone.utc).isoformat(),
                                  "stage": stage, "message": message})
            self.save(job_id)

    def start_job_worker(self, job_id, worker, environment=None):
        args = (job_id,) if environment is None else (job_id, environment)
        try:
            threading.Thread(target=worker, args=args, daemon=True).start()
        except Exception:
            if environment is not None:
                environment.clear()
            with self.lock:
                job = self.jobs[job_id]
                job['status'] = 'interrupted'
                job['events'].append({
                    'time': datetime.now(timezone.utc).isoformat(),
                    'stage': 'interrupted',
                    'message': '배포 작업을 시작하지 못했습니다. 실행 결과를 확인하고 새 배포를 시작하세요.'})
                self.save(job_id)
            return False
        return True

    def run_agent(self, job_id, environment=None):
        environment = {} if environment is None else environment
        tools = None
        def checkpoint(**updates):
            with self.lock:
                job = self.jobs[job_id]
                if job.get('cancel_requested') and ('attempts' in updates or updates.get('status') in {'waiting_input', 'succeeded', 'failed'}):
                    raise DeploymentCancelled()
                if "change" in updates:
                    job.setdefault("changes", []).append(updates.pop("change"))
                job.update(updates)
                if "plan" in updates and updates["plan"] is None:
                    job["diff"] = ""
                if job.get("plan"):
                    job["diff"] = dockerfile_diff(Path(job["project"]), job["plan"])
                self.save(job_id)
        try:
            job = self.jobs[job_id]
            if (job.get('source_digest') is not None
                    and source_digest(Path(job['project'])) != job['source_digest']):
                raise ValueError('업로드한 앱 소스가 변경됐습니다. 새 배포를 시작하세요.')
            target = job.get("target", "local-docker")
            policy = (policy_from_record(job['deployment_policy'])
                      if 'deployment_policy' in job else None)
            if 'architecture_decision' in job and policy is None:
                raise ValueError('Architecture decision requires a deployment policy')
            if policy is not None:
                policy.require(
                    target, (job.get('infrastructure_plan') or {}).get('compatibility', {}).get('access_mode'),
                    new_managed_database=job.get('postgres_creation_id') is not None,
                    data_migration=job.get('sqlite_conversion') is not None)
                if 'architecture_decision' in job:
                    verify_architecture_decision(job['architecture_decision'], job.get('application_ir'),
                                                 policy, job.get('infrastructure_plan'))
                if 'compilation' in job:
                    verify_compilation(job['compilation'], job['architecture_decision'],
                                       job.get('application_ir'), policy, job.get('infrastructure_plan'))
            adapter_factory = LocalDockerAdapter
            if target == 'local-docker' and job.get('group_id'):
                with self.lock:
                    aws_in_group = any(other.get('group_id') == job['group_id']
                                       and other.get('target') == 'aws-ecs-express'
                                       and other.get('group_order', -1) > job.get('group_order', -1)
                                       for other in self.jobs.values())
                if aws_in_group:
                    adapter_factory = lambda event: LocalDockerAdapter(event, platform='linux/amd64')
            if target == 'local-docker' and job.get('local_sqlite_binding'):
                adapter_factory = lambda event: LocalDockerAdapter(
                    event, sqlite_binding=job['local_sqlite_binding'])
            if target == 'onprem-compose':
                adapter_factory = lambda event: LocalComposeAdapter(
                    event, self.root / job_id, sqlite_binding=job.get('local_sqlite_binding'))
            if target == 'onprem-vm':
                adapter_factory = lambda event: RemoteVmComposeAdapter(
                    event, self.root / job_id, VmSettings(**job['vm']))
            if target == "cloud-run":
                settings = CloudRunSettings(**job["cloud"])
                adapter_factory = lambda event: CloudRunAdapter(event, settings, public=job.get("public", False))
            elif target == "aws-ecs-express":
                settings = AwsSettings(**job["aws"])
                adapter_factory = lambda event: AwsExpressAdapter(event, settings, existing=job.get('prior_result'),
                                                                    checkpoint=checkpoint,
                                                                    **({'rehearsal': True} if job.get('postgres') is None else {}))
            tools = DeploymentTools(Path(job["project"]), self.root / job_id / "work", job_id,
                                    environment, lambda s, m: self.event(job_id, s, m), checkpoint,
                                    attempts=job.get("attempts", 0), adapter_factory=adapter_factory, target=target,
                                    infrastructure_plan=job.get('infrastructure_plan'),
                                    postgres_request=postgres_request_from_job(job),
                                    sqlite_conversion=job.get('sqlite_conversion'),
                                    local_sqlite_binding=job.get('local_sqlite_binding'),
                                    deployment_policy=policy,
                                    new_managed_database=job.get('postgres_creation_id') is not None,
                                    compilation=job.get('compilation'),
                                    architecture_decision=job.get('architecture_decision'),
                                    npm_lock_sync=job.get('npm_lock_sync'),
                                    cancel_check=lambda: self.cancel_requested(job_id),
                                    require_existing_work=bool(job.get('steps', 0)),
                                    expected_work_digest=job.get('work_digest'))
            self.event(job_id, "starting", "AI가 작업용 소스에서 배포를 준비합니다.")
            result = DeploymentAgent(self.agent_factory(self.ai_settings), tools,
                                     steps=job.get("steps", 0),
                                     max_steps=40 if job.get('sqlite_conversion') else 24).run()
            checkpoint(status="succeeded", result=result, missing_environment=[], deployment_state="active")
            if job.get('replaces_job_id'):
                with self.lock:
                    previous = self.jobs.get(job['replaces_job_id'])
                    if previous and previous.get('deployment_state', 'active') == 'active':
                        if (target == 'aws-ecs-express' and result.get('previous_task_definition_arn')
                                and previous.get('result')):
                            previous['result']['task_definition_arn'] = result['previous_task_definition_arn']
                        previous['deployment_state'] = 'superseded'
                        self.save(previous['id'])
            self.event(job_id, "succeeded", "실제 HTTP 응답 확인 완료")
            if target == 'local-docker' and job.get('git_replaces_local_job_id'):
                self.retire_replaced_github_local(job_id)
        except DeploymentCancelled:
            checkpoint(status="cancelled", missing_environment=[], cancel_requested=False)
            self.event(job_id, "cancelled", "배포 시도 전에 사용자가 작업을 취소했습니다.")
        except NeedsEnvironment as exc:
            # Previous values are not persisted, so ask for their names again on resume too.
            names = sorted(set(exc.names) | set(environment))
            try:
                digest = source_digest(tools.work)
                checkpoint(status="waiting_input", missing_environment=names,
                           input_reason=exc.reason, work_digest=digest)
            except DeploymentCancelled:
                checkpoint(status="cancelled", missing_environment=[], cancel_requested=False)
                self.event(job_id, "cancelled", "배포 시도 전에 사용자가 작업을 취소했습니다.")
            except (OSError, ValueError):
                checkpoint(status="failed", missing_environment=[])
                self.event(job_id, "error", "작업용 소스의 무결성을 확인하지 못했습니다. 새 배포를 시작하세요.")
            else:
                self.event(job_id, "waiting_input", exc.reason)
        except Exception as exc:
            if self.cancel_requested(job_id) and not self.jobs[job_id].get('attempts'):
                checkpoint(status="cancelled", missing_environment=[], cancel_requested=False)
                self.event(job_id, "cancelled", "배포 시도 전에 사용자가 작업을 취소했습니다.")
                return
            message = str(exc)
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            self.event(job_id, "error", message)
            try:
                checkpoint(status="failed")
            except DeploymentCancelled:
                checkpoint(status="cancelled", missing_environment=[], cancel_requested=False)
                self.event(job_id, "cancelled", "배포 시도 전에 사용자가 작업을 취소했습니다.")
                return
            if self.jobs[job_id].get('aws_update_submitted') and not self.jobs[job_id].get('aws_update_failed_at'):
                checkpoint(aws_update_failed_at=datetime.now(timezone.utc).isoformat())
            if (self.jobs[job_id].get('aws_update_submitted') or any(
                    event.get('stage') == 'update_submitting' for event in self.jobs[job_id].get('events', []))) \
                    and self.jobs[job_id].get('replaces_job_id'):
                with self.lock:
                    previous = self.jobs.get(self.jobs[job_id]['replaces_job_id'])
                    if previous and previous.get('deployment_state', 'active') == 'active':
                        previous['deployment_state'] = 'needs_attention'
                        self.save(previous['id'])
        finally:
            environment.clear()
            if tools is not None:
                tools.environment.clear()
            group_id = self.jobs.get(job_id, {}).get('group_id')
            if group_id and self.jobs[job_id].get('status') != 'waiting_input':
                self.start_group_worker(group_id)

    def cancel_requested(self, job_id):
        with self.lock:
            return bool(self.jobs[job_id].get('cancel_requested'))

    def run_postgres_then_agent(self, job_id: str) -> None:
        """Wait for a separately journaled RDS creation; never retry it on restart."""
        try:
            job = self.jobs[job_id]
            request = postgres_request_from_job(job)
            if request is None:
                raise ValueError('PostgreSQL 생성 작업에 연결 정보가 없습니다.')
            creation_id = job.get('postgres_creation_id')
            if not creation_id:
                raise ValueError('DB 생성 시도 연결 기록이 없습니다. 앱은 자동 배포하지 않습니다.')
            deadline = time.monotonic() + 3600
            while True:
                operation = self.postgres_operations.get(request.application_id)
                if operation.get('creation_id') != creation_id:
                    raise ValueError('DB 생성 시도가 배포 작업과 다릅니다. 앱은 자동 배포하지 않습니다.')
                if operation['status'] == 'succeeded':
                    if operation['database_id'] != request.database_id:
                        raise ValueError('생성된 DB 식별자가 배포 계획과 다릅니다.')
                    self.postgres_operations.require_successful_creation(request, creation_id)
                    if source_digest(Path(job['project'])) != job.get('source_digest'):
                        raise ValueError('업로드한 앱 소스가 변경됐습니다. 기존 DB 사용으로 새 배포를 시작하세요.')
                    database = AwsPostgresProvisioner(request).inspect_current()
                    self.postgres_operations.require_deployable(
                        request.application_id, database['database_id'])
                    with self.lock:
                        job = self.jobs[job_id]
                        if job['status'] != 'provisioning':
                            return
                        job['status'] = 'running'
                        self.save(job_id)
                    self.event(job_id, 'database_ready', '소유 PostgreSQL 생성 완료. 앱 배포를 시작합니다.')
                    self.run_agent(job_id)
                    return
                if operation['status'] != 'running':
                    raise ValueError('DB 생성 결과가 불확실합니다. 생성 상태를 재확인하세요. 앱은 자동 배포하지 않습니다.')
                if time.monotonic() > deadline:
                    raise TimeoutError('DB 생성 대기 시간이 초과됐습니다. 상태를 재확인하세요. 앱은 자동 배포하지 않습니다.')
                time.sleep(3)
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                if job['status'] not in {'provisioning', 'running'}:
                    return
                job['status'] = 'interrupted'
                self.save(job_id)
            self.event(job_id, 'database_attention', redact(str(exc))[:500])

    def resume_postgres_deployment(self, job_id: str) -> dict:
        """Explicitly continue an untouched app job after its RDS create is confirmed."""
        def eligible(job):
            return (job and job.get('status') == 'interrupted'
                    and job.get('mode') == 'agent'
                    and job.get('target') == 'aws-ecs-express'
                    and job.get('infrastructure_plan', {}).get('database', {}).get('binding') == 'create'
                    and isinstance(job.get('postgres_creation_id'), str)
                    and isinstance(job.get('source_digest'), str)
                    and re.fullmatch(r'[a-f0-9]{64}', job['source_digest'])
                    and job.get('attempts') == 0 and job.get('steps') == 0
                    and not job.get('changes') and job.get('plan') is None
                    and not job.get('result') and not job.get('cancel_requested')
                    and not job.get('aws_update_submitted') and not job.get('persistence_failed')
                    and not (self.root / job_id / 'work').exists())

        with self.lock:
            job = self.jobs.get(job_id)
            if not eligible(job):
                raise ValueError('DB 생성 후 앱 배포를 안전하게 재개할 수 없는 작업입니다.')
            request = postgres_request_from_job(job)
            if request is None:
                raise ValueError('저장된 DB 연결 요청을 확인하지 못했습니다.')
            creation_id = job['postgres_creation_id']
            project = Path(job['project'])
            expected_digest = job['source_digest']
        if source_digest(project) != expected_digest:
            raise ValueError('업로드한 앱 소스가 변경됐습니다. 기존 DB 사용으로 새 배포를 시작하세요.')
        self.postgres_operations.require_successful_creation(request, creation_id)
        database = AwsPostgresProvisioner(request).inspect_current()
        self.postgres_operations.require_deployable(request.application_id, database['database_id'])
        with self.lock:
            job = self.jobs.get(job_id)
            if not eligible(job):
                raise ValueError('배포 작업 상태가 변경됐습니다. 이력을 다시 확인하세요.')
            self.ensure_application_available(request.application_id, 'aws-ecs-express')
            if any(other is not job and other.get('application_id') == request.application_id
                   and other.get('target') == 'aws-ecs-express'
                   and other.get('status') == 'succeeded'
                   and other.get('deployment_state', 'active') == 'active'
                   for other in self.jobs.values()):
                raise ValueError('같은 앱의 다른 AWS 릴리스가 활성 상태입니다. 이전 작업을 재개할 수 없습니다.')
            self.postgres_operations.require_successful_creation(request, creation_id)
            if source_digest(project) != expected_digest:
                raise ValueError('업로드한 앱 소스가 변경됐습니다. 기존 DB 사용으로 새 배포를 시작하세요.')
            job['status'] = 'running'
            self.save(job_id)
        self.event(job_id, 'database_manual_resume',
                   '생성된 소유 PostgreSQL을 재확인했습니다. 요청에 따라 앱 배포만 시작합니다.')
        try:
            threading.Thread(target=self.run_agent, args=(job_id,), daemon=True).start()
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['status'] = 'interrupted'
                self.save(job_id)
            self.event(job_id, 'database_attention',
                       '앱 배포 작업을 시작하지 못했습니다: ' + redact(str(exc))[:300])
            return {'id': job_id, 'status': 'interrupted'}
        return {'id': job_id, 'status': 'running'}

    def resume_unstarted_deployment(self, job_id: str) -> dict:
        """Explicitly restart a job that never reached an AI step or deployment attempt."""
        def eligible(job):
            return (job and job.get('mode') == 'agent' and job.get('status') == 'interrupted'
                    and job.get('target') in {'local-docker', 'onprem-compose', 'onprem-vm', 'cloud-run', 'aws-ecs-express'}
                    and job.get('attempts') == 0 and job.get('steps') == 0
                    and job.get('changes') == [] and job.get('plan') is None
                    and job.get('result') is None and not job.get('cancel_requested')
                    and not job.get('persistence_failed') and not job.get('aws_update_submitted')
                    and isinstance(job.get('infrastructure_plan'), dict)
                    and job['infrastructure_plan'].get('target') == job.get('target')
                    and job['infrastructure_plan'].get('database') is None
                    and job.get('postgres') is None and job.get('postgres_creation_id') is None
                    and job.get('prior_result') is None
                    and isinstance(job.get('source_digest'), str)
                    and re.fullmatch(r'[a-f0-9]{64}', job['source_digest']))

        if not self.ai_settings.available:
            raise ValueError('AI 배포를 재개하려면 서버에 OPENAI_API_KEY를 설정하세요.')
        with self.lock:
            job = self.jobs.get(job_id)
            if not eligible(job):
                raise ValueError('배포 시도 전 상태를 안전하게 재개할 수 없는 작업입니다.')
            project = Path(job['project'])
            expected_digest = job['source_digest']
        work = self.root / job_id / 'work'
        def unchanged():
            return (source_digest(project) == expected_digest
                    and not work.is_symlink()
                    and (not work.exists() or
                         (work.is_dir() and source_digest(work) == expected_digest)))
        if not unchanged():
            raise ValueError('업로드 또는 작업용 소스가 변경됐습니다. 새 배포를 시작하세요.')
        with self.lock:
            job = self.jobs.get(job_id)
            if not eligible(job) or not unchanged():
                raise ValueError('배포 작업이나 소스 상태가 변경됐습니다. 이력을 다시 확인하세요.')
            self.ensure_application_available(job['application_id'], job['target'])
            if (job['target'] == 'aws-ecs-express' and any(
                    other is not job and other.get('application_id') == job['application_id']
                    and other.get('target') == 'aws-ecs-express'
                    and other.get('status') == 'succeeded'
                    and other.get('deployment_state', 'active') == 'active'
                    for other in self.jobs.values())):
                raise ValueError('같은 앱의 AWS 릴리스가 활성 상태입니다. 이전 작업을 재개할 수 없습니다.')
            job['status'] = 'running'
            job['events'].append({'time': datetime.now(timezone.utc).isoformat(),
                                  'stage': 'manual_resume',
                                  'message': '배포 시도 전 중단된 작업을 다시 시작했습니다.'})
            self.save(job_id)
        started = self.start_job_worker(job_id, self.run_agent)
        return {'id': job_id, 'status': 'running' if started else 'interrupted'}

    def run(self, job_id, environment=None):
        environment = {} if environment is None else environment
        try:
            job = self.jobs[job_id]
            result = LocalDockerAdapter(lambda s, m: self.event(job_id, s, m)).deploy(
                Path(job["project"]), DeploymentPlan(**job["plan"]), job_id, environment)
            with self.lock:
                job.update(status="succeeded", result=result)
                self.save(job_id)
        except Exception as exc:
            message = str(exc)
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            self.event(job_id, "error", message)
            with self.lock:
                self.jobs[job_id]["status"] = "failed"
                self.save(job_id)
        finally:
            environment.clear()

    def retire_aws(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            result = job['result']
            attempt_id = result.get('owner_attempt') or result['service'].removeprefix('sky-')
            adapter = AwsExpressAdapter(lambda stage, message: self.event(job_id, stage, message),
                                        AwsSettings(**job['aws']))
            adapter.retire(result, attempt_id)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', 'ECS 서비스와 해당 ECR 이미지 태그의 삭제를 확인했습니다.')

    def retire_local(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            adapter = LocalDockerAdapter(lambda stage, message: self.event(job_id, stage, message))
            if job['status'] == 'succeeded':
                adapter.retire(job['result'], job_id)
            else:
                for number in range(1, job['attempts'] + 1):
                    attempt = f'{job_id}-a{number}'
                    adapter.retire({'container': f'sky-{attempt}',
                                    'image': f'sky/{attempt}:latest'}, job_id)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', '로컬 Docker 컨테이너와 이미지 태그의 삭제를 확인했습니다.')

    def retire_compose(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            adapter = LocalComposeAdapter(
                lambda stage, message: self.event(job_id, stage, message), self.root / job_id)
            if job['status'] == 'succeeded' and job.get('result'):
                adapter.retire(job['result'], job_id)
            elif job.get('status') in {'failed', 'interrupted'} and not job.get('result'):
                for number in range(1, job['attempts'] + 1):
                    adapter.retire_orphan(f'{job_id}-a{number}')
            else:
                raise ValueError('정리할 Compose 배포가 아닙니다.')
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', 'Compose 서비스와 이미지 태그의 삭제를 확인했습니다.')

    def retire_vm(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            adapter = RemoteVmComposeAdapter(
                lambda stage, message: self.event(job_id, stage, message),
                self.root / job_id, VmSettings(**job['vm']))
            if job['status'] == 'succeeded' and job.get('result'):
                adapter.retire(job['result'], job_id)
            elif job.get('status') in {'failed', 'interrupted'} and not job.get('result'):
                for number in range(1, job['attempts'] + 1):
                    adapter.retire_orphan(f'{job_id}-a{number}')
            else:
                raise ValueError('정리할 VM 배포가 아닙니다.')
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', 'VM Compose 서비스와 이미지 태그의 삭제를 확인했습니다.')

    def retire_cloud(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            result = job['result']
            attempt_id = result['service'].removeprefix('sky-')
            CloudRunAdapter(lambda stage, message: self.event(job_id, stage, message),
                            CloudRunSettings(**job['cloud'])).retire(result, attempt_id)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', 'Cloud Run 서비스와 해당 Artifact Registry 이미지의 삭제를 확인했습니다.')

    def reconcile_aws_update(self, job_id):
        with self.lock:
            failed = self.jobs.get(job_id)
            previous = self.jobs.get(failed.get('replaces_job_id')) if failed else None
            if (not failed or failed.get('status') not in {'failed', 'interrupted'} or not failed.get('aws_update_submitted')
                    or failed.get('target') != 'aws-ecs-express' or not previous
                    or previous.get('deployment_state') != 'needs_attention'
                    or previous.get('status') != 'succeeded'):
                raise ValueError('재확인할 AWS 업데이트가 아닙니다.')
            failed_snapshot = json.loads(json.dumps(failed))
            previous_snapshot = json.loads(json.dumps(previous))
        prior_result = previous_snapshot['result']
        adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(**previous_snapshot['aws']))
        deployments = json.loads(adapter.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                              '--service', prior_result['service']], private=True, quiet=True))
        items = deployments.get('serviceDeployments', [])
        latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
        if (not latest.get('serviceDeploymentArn')
                or latest.get('status') not in {'SUCCESSFUL', 'ROLLBACK_SUCCESSFUL'}):
            return {'reconciled': False, 'reason': 'AWS 새 배포 또는 롤백이 아직 완료되지 않았습니다.'}
        no_new_deployment = latest['serviceDeploymentArn'] == failed_snapshot.get('aws_previous_deployment_arn')
        if no_new_deployment:
            failed_at = failed_snapshot.get('aws_update_failed_at')
            try:
                settled = datetime.fromisoformat(failed_at)
                if settled.tzinfo is None or datetime.now(timezone.utc) - settled < timedelta(minutes=10):
                    raise ValueError('AWS 배포 이력 반영을 기다리는 중입니다. 실패 또는 재시작 후 10분 뒤 다시 확인하세요.')
            except (TypeError, ValueError) as exc:
                return {'reconciled': False, 'reason': str(exc) if str(exc).startswith('AWS 배포') else
                        '업데이트 결과를 확인할 시간이 기록되지 않았습니다.'}
        service_data = json.loads(adapter.aws(['ecs', 'describe-express-gateway-service',
                                               '--service-arn', prior_result['service_arn'], '--include', 'TAGS'],
                                              private=True, quiet=True)).get('service', {})
        tags = {item.get('key'): item.get('value') for item in service_data.get('tags', [])}
        owner_attempt = prior_result.get('owner_attempt') or prior_result['service'].removeprefix('sky-')
        if (service_data.get('serviceArn') != prior_result['service_arn']
                or service_data.get('status', {}).get('statusCode') != 'ACTIVE'
                or service_data.get('currentDeployment')
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-attempt') != owner_attempt):
            return {'reconciled': False, 'reason': 'ECS 서비스 소유권 또는 완료 상태를 확인할 수 없습니다.'}
        active_images = {config.get('primaryContainer', {}).get('image')
                         for config in service_data.get('activeConfigurations', [])}
        candidate_image = failed_snapshot.get('aws_candidate_image')
        attempt = failed_snapshot['id'] + '-a' + str(failed_snapshot.get('attempts', 0))
        expected_image = prior_result['image'].rsplit(':', 1)[0] + ':' + attempt
        if candidate_image != expected_image:
            raise ValueError('업데이트 이미지 식별자가 예상과 다릅니다.')
        if (not no_new_deployment and latest['status'] == 'SUCCESSFUL'
                and active_images == {candidate_image}):
            active_configs = service_data.get('activeConfigurations', [])
            task_arn = active_configs[0].get('taskDefinitionArn') if len(active_configs) == 1 else None
            task_prefix = (f"arn:aws:ecs:{prior_result['region']}:{prior_result['account']}:task-definition/")
            if (not isinstance(task_arn, str) or not task_arn.startswith(task_prefix)
                    or not re.fullmatch(r'[A-Za-z0-9_-]+:\d+', task_arn.removeprefix(task_prefix))):
                return {'reconciled': False, 'reason': '새 릴리스의 ECS 태스크 정의를 확인할 수 없습니다.'}
            candidate_result = {**prior_result, 'image': candidate_image,
                                'images': [*(prior_result.get('images') or [prior_result['image']]), candidate_image],
                                'task_definition_arn': task_arn,
                                'previous_task_definition_arn': prior_result.get('task_definition_arn'),
                                'health_url': prior_result['url'].rstrip('/') + failed_snapshot['plan']['health_path']}
            candidate = {**failed_snapshot, 'status': 'succeeded', 'result': candidate_result,
                         'deployment_state': 'active'}
            health = check_deployment(candidate)
            if health['healthy']:
                with self.lock:
                    if (self.jobs[job_id].get('status') not in {'failed', 'interrupted'}
                            or self.jobs[previous_snapshot['id']].get('deployment_state') != 'needs_attention'):
                        raise ValueError('재확인 도중 작업 상태가 변경됐습니다.')
                    self.jobs[job_id].update(status='succeeded', result=candidate_result,
                                             deployment_state='active')
                    self.save(job_id)
                    self.jobs[previous_snapshot['id']]['deployment_state'] = 'superseded'
                    self.save(previous_snapshot['id'])
                self.event(job_id, 'reconciled', 'AWS 새 릴리스의 이미지와 HTTP 200을 확인해 성공으로 복구했습니다.')
                return {'reconciled': True, 'release': 'new', 'health': health}
        previous_ready = (active_images == {prior_result['image']}
                          and (no_new_deployment or latest['status'] in {'SUCCESSFUL', 'ROLLBACK_SUCCESSFUL'}))
        health = check_deployment(previous_snapshot) if previous_ready else {
            'healthy': False, 'reason': 'AWS 이전 이미지가 유일한 활성 구성인지 확인할 수 없습니다.'}
        if health['healthy']:
            with self.lock:
                if self.jobs[previous_snapshot['id']].get('deployment_state') != 'needs_attention':
                    raise ValueError('재확인 도중 작업 상태가 변경됐습니다.')
                self.jobs[previous_snapshot['id']]['deployment_state'] = 'active'
                self.save(previous_snapshot['id'])
                self.jobs[job_id]['aws_reconciled'] = 'previous'
                self.save(job_id)
            self.event(job_id, 'reconciled', '이전 릴리스의 이미지와 HTTP 200을 확인했습니다.')
            return {'reconciled': True, 'release': 'previous', 'health': health}
        return {'reconciled': False, 'reason': '활성 이미지 또는 HTTP 응답을 확인할 수 없습니다.', 'health': health}

    def inspect_interrupted_aws_migration(self, job_id: str) -> dict:
        """Record one owned ECS task outcome without restarting an interrupted deployment."""
        from adapters.aws.migrations import inspect_migration_task

        with self.lock:
            job = self.jobs.get(job_id)
            if (not job or job.get('mode') != 'agent'
                    or job.get('target') != 'aws-ecs-express'
                    or job.get('status') not in {'failed', 'interrupted'}
                    or job.get('result') is not None
                    or type(job.get('attempts')) is not int
                    or not 1 <= job['attempts'] <= 3
                    or not job.get('aws_migration_task_arn')
                    or not job.get('aws_migration_task_definition_arn')):
                raise ValueError('재확인할 중단된 AWS SQL 마이그레이션 작업이 아닙니다.')
            snapshot = json.loads(json.dumps(job))
        request = postgres_request_from_job(snapshot)
        if request is None or request.application_id != snapshot.get('application_id'):
            raise ValueError('작업의 PostgreSQL 소유 정보를 확인하지 못했습니다.')
        settings = AwsSettings(**snapshot['aws'])
        if settings.region != request.region or settings.expected_account != request.account:
            raise AwsConfigurationError('작업의 AWS 계정·리전과 PostgreSQL 소유 정보가 다릅니다.')
        adapter = AwsExpressAdapter(lambda *_: None, settings)
        caller = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
        if caller.get('Account') != request.account:
            raise AwsConfigurationError('현재 AWS 계정이 마이그레이션 소유 계정과 다릅니다.')
        outcome = inspect_migration_task(adapter, request.application_id, request.account,
            request.region, job_id + '-a' + str(snapshot['attempts']),
            snapshot['aws_migration_task_arn'], snapshot['aws_migration_task_definition_arn'])
        inspected = {**outcome, 'checked_at': datetime.now(timezone.utc).isoformat()}
        with self.lock:
            current = self.jobs.get(job_id)
            if (not current or current.get('status') not in {'failed', 'interrupted'}
                    or current.get('attempts') != snapshot['attempts']
                    or current.get('aws_migration_task_arn') != snapshot['aws_migration_task_arn']
                    or current.get('aws_migration_task_definition_arn') != snapshot['aws_migration_task_definition_arn']):
                raise ValueError('재확인 중 배포 작업 상태가 변경됐습니다.')
            current['aws_migration_inspection'] = inspected
            current['events'].append({'time': inspected['checked_at'], 'stage': 'migration_inspected',
                'message': 'AWS SQL 마이그레이션 태스크 결과: ' + outcome['status']
                           + '. 앱 배포는 자동 재개하지 않습니다.'})
            self.save(job_id)
        return inspected

    def cleanup_interrupted_aws_migration(self, job_id: str) -> dict:
        """Explicitly retire a verified stopped migration's unique AWS artifacts."""
        from adapters.aws.migrations import cleanup_interrupted_migration

        def recorded_success(job):
            completed = job.get('aws_migration_result') or {}
            return (job.get('aws_migration_status') == 'succeeded'
                    and all(completed.get(key) for key in
                            ('task_arn', 'task_definition_arn', 'image', 'image_digest'))
                    and completed.get('task_arn') == job.get('aws_migration_task_arn')
                    and completed.get('task_definition_arn') == job.get('aws_migration_task_definition_arn')
                    and completed.get('image') == job.get('aws_migration_image')
                    and completed.get('image_digest') == job.get('aws_migration_image_digest'))

        def eligible(job):
            if not job:
                return False
            inspection = job.get('aws_migration_inspection') or {}
            inspected = (inspection.get('status') in {'succeeded', 'failed'}
                         and inspection.get('task_arn') and inspection.get('task_definition_arn')
                         and inspection.get('task_arn') == job.get('aws_migration_task_arn')
                         and inspection.get('task_definition_arn') == job.get('aws_migration_task_definition_arn'))
            deployment = job.get('result') or {}
            migration = deployment.get('migration') or {}
            incomplete_success = (job.get('status') == 'succeeded'
                                  and migration.get('cleanup_complete') is False
                                  and recorded_success(job))
            interrupted = (job.get('status') in {'failed', 'interrupted'}
                           and job.get('result') is None
                           and (inspected or recorded_success(job)))
            return (job and job.get('mode') == 'agent'
                    and job.get('target') == 'aws-ecs-express'
                    and type(job.get('attempts')) is int and 1 <= job['attempts'] <= 3
                    and job.get('aws_migration_cleanup_state') not in {'running', 'done'}
                    and (interrupted or incomplete_success))

        with self.lock:
            job = self.jobs.get(job_id)
            if not eligible(job):
                raise ValueError('정리할 수 있는 중단된 AWS SQL 마이그레이션이 아닙니다.')
            snapshot = json.loads(json.dumps(job))
            job['aws_migration_cleanup_state'] = 'running'
            job.pop('aws_migration_cleanup_error', None)
            self.save(job_id)
        try:
            request = postgres_request_from_job(snapshot)
            if request is None or request.application_id != snapshot.get('application_id'):
                raise ValueError('작업의 PostgreSQL 소유 정보를 확인하지 못했습니다.')
            settings = AwsSettings(**snapshot['aws'])
            if settings.region != request.region or settings.expected_account != request.account:
                raise AwsConfigurationError('작업의 AWS 계정·리전과 PostgreSQL 소유 정보가 다릅니다.')
            adapter = AwsExpressAdapter(lambda *_: None, settings)
            caller = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
            if caller.get('Account') != request.account:
                raise AwsConfigurationError('현재 AWS 계정이 마이그레이션 소유 계정과 다릅니다.')
            def definition_inactive_checkpoint():
                with self.lock:
                    current = self.jobs[job_id]
                    if (current.get('aws_migration_cleanup_state') != 'running'
                            or current.get('aws_migration_task_definition_arn') != snapshot['aws_migration_task_definition_arn']):
                        raise ValueError('정리 중 작업 기록이 변경됐습니다.')
                    current['aws_migration_cleanup_definition_inactive'] = True
                    self.save(job_id)
            result = cleanup_interrupted_migration(adapter, request,
                job_id + '-a' + str(snapshot['attempts']), snapshot['aws_migration_task_arn'],
                snapshot['aws_migration_task_definition_arn'], snapshot['aws_migration_image'],
                snapshot['aws_migration_image_digest'],
                definition_inactive=bool(snapshot.get('aws_migration_cleanup_definition_inactive')),
                verified_outcome=recorded_success(snapshot),
                checkpoint=definition_inactive_checkpoint)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['aws_migration_cleanup_state'] = 'failed'
                self.jobs[job_id]['aws_migration_cleanup_error'] = redact(str(exc))[:300]
                self.save(job_id)
            raise
        with self.lock:
            current = self.jobs[job_id]
            current['aws_migration_cleanup_state'] = 'done'
            current['aws_migration_cleanup_image_deleted'] = result['image_deleted']
            if current.get('status') == 'succeeded':
                current['result']['migration']['cleanup_complete'] = True
            self.save(job_id)
        self.event(job_id, 'migration_cleanup', 'SQL 마이그레이션의 전용 ECS 정의와 ECR 태그를 정리했습니다.')
        return result

    def cleanup_abandoned_aws_image(self, job_id):
        with self.lock:
            failed = self.jobs.get(job_id)
            previous = self.jobs.get(failed.get('replaces_job_id')) if failed else None
            if (not failed or failed.get('status') not in {'failed', 'interrupted'}
                    or failed.get('target') != 'aws-ecs-express' or failed.get('aws_reconciled') != 'previous'
                    or failed.get('aws_image_cleanup_state') in {'running', 'done'}
                    or not previous or previous.get('status') != 'succeeded'
                    or previous.get('deployment_state') != 'active'
                    or any(other is not failed and other is not previous
                           and other.get('application_id') == failed.get('application_id')
                           and other.get('target') == 'aws-ecs-express'
                           and other.get('status') in {'provisioning', 'running', 'waiting_input'} for other in self.jobs.values())):
                raise ValueError('정리할 수 있는 실패 AWS 이미지가 아닙니다.')
            failed['aws_image_cleanup_state'] = 'running'
            self.save(job_id)
            failed_snapshot = json.loads(json.dumps(failed))
            previous_snapshot = json.loads(json.dumps(previous))
        try:
            health = check_deployment(previous_snapshot)
            if not health['healthy']:
                raise ValueError('기존 AWS 릴리스의 실제 실행 상태를 확인할 수 없습니다: ' + health['reason'])
            adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(**previous_snapshot['aws']))
            attempt = failed_snapshot['id'] + '-a' + str(failed_snapshot.get('attempts', 0))
            result = adapter.cleanup_abandoned_image(previous_snapshot['result'],
                                                      failed_snapshot.get('aws_candidate_image'), attempt)
        except Exception:
            with self.lock:
                self.jobs[job_id]['aws_image_cleanup_state'] = 'failed'
                self.save(job_id)
            raise
        with self.lock:
            self.jobs[job_id]['aws_image_cleanup_state'] = 'done'
            self.save(job_id)
        self.event(job_id, 'cleanup', '이전 릴리스가 실행 중임을 확인하고 실패한 ECR 이미지 태그를 삭제했습니다.')
        return result

    def request_aws_update_rollback(self, job_id):
        with self.lock:
            failed = self.jobs.get(job_id)
            previous = self.jobs.get(failed.get('replaces_job_id')) if failed else None
            if (not failed or failed.get('status') not in {'failed', 'interrupted'}
                    or failed.get('target') != 'aws-ecs-express' or not failed.get('aws_update_submitted')
                    or failed.get('aws_reconciled') or not previous
                    or previous.get('status') != 'succeeded'
                    or previous.get('deployment_state') != 'needs_attention'):
                raise ValueError('롤백을 요청할 수 있는 AWS 업데이트가 아닙니다.')
            failed_snapshot = json.loads(json.dumps(failed))
            previous_snapshot = json.loads(json.dumps(previous))
        adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(**previous_snapshot['aws']))
        attempt = failed_snapshot['id'] + '-a' + str(failed_snapshot.get('attempts', 0))
        result = adapter.request_update_rollback(previous_snapshot['result'],
                                                 failed_snapshot.get('aws_candidate_image'),
                                                 failed_snapshot.get('aws_previous_deployment_arn'), attempt)
        with self.lock:
            if self.jobs[previous_snapshot['id']].get('deployment_state') != 'needs_attention':
                raise ValueError('롤백 요청 중 이전 릴리스 상태가 변경됐습니다.')
            self.jobs[job_id]['aws_rollback_requested'] = True
            self.jobs[job_id]['aws_rollback_deployment_arn'] = result['service_deployment_arn']
            self.save(job_id)
        self.event(job_id, 'rollback_requested', '진행 중인 ECS 배포의 이전 리비전 롤백을 요청했습니다. 완료 후 결과를 재확인하세요.')
        return result

    def start_release_rollback(self, job_id, target_job_id=None):
        with self.lock:
            current = self.jobs.get(job_id)
            target_job_id = target_job_id or (current.get('replaces_job_id') if current else None)
            previous = self.jobs.get(target_job_id)
            current_result = current.get('result') if current else None
            previous_result = previous.get('result') if previous else None
            if (not current or current.get('status') != 'succeeded'
                    or current.get('target') != 'aws-ecs-express'
                    or current.get('deployment_state', 'active') != 'active'
                    or current.get('release_rollback_state') == 'running'
                    or not previous or previous.get('status') != 'succeeded'
                    or previous.get('deployment_state') != 'superseded'
                    or previous.get('application_id') != current.get('application_id')
                    or previous.get('target') != 'aws-ecs-express'
                    or not isinstance(current_result, dict) or not isinstance(previous_result, dict)
                    or not previous_result.get('task_definition_arn')
                    or any(previous_result.get(key) != current_result.get(key)
                           for key in ('service', 'service_arn', 'account', 'region', 'url'))
                    or previous_result.get('image') not in (current_result.get('images') or [])
                    or any(other is not current and other is not previous
                           and other.get('application_id') == current.get('application_id')
                           and other.get('target') == 'aws-ecs-express'
                           and (other.get('status') in {'provisioning', 'running', 'waiting_input'}
                                or other.get('deployment_state') in {'deleting', 'needs_attention'}
                                or other.get('aws_image_cleanup_state') == 'running')
                           for other in self.jobs.values())):
                raise ValueError('이전 릴리스로 되돌릴 수 있는 활성 AWS 배포가 아닙니다.')
            current['release_rollback_state'] = 'running'
            current['release_rollback_target_id'] = previous['id']
            current.pop('release_rollback_submitted', None)
            current.pop('release_rollback_verification', None)
            current.pop('release_rollback_failed_at', None)
            self.save(job_id)
        threading.Thread(target=self.run_release_rollback, args=(job_id,), daemon=True).start()

    def finish_release_rollback(self, job_id, previous_id, verification=None):
        with self.lock:
            current = self.jobs[job_id]
            previous = self.jobs[previous_id]
            if verification is not None:
                current['release_rollback_verification'] = verification
            current['release_rollback_state'] = 'succeeded'
            current['deployment_state'] = 'superseded'
            current['release_rollback_restore_pending'] = True
            self.save(job_id)
            previous['result']['images'] = list(dict.fromkeys(
                [*(previous['result'].get('images') or [previous['result']['image']]),
                 *(current['result'].get('images') or [])]))
            previous['deployment_state'] = 'active'
            self.save(previous_id)
            current['release_rollback_restore_pending'] = False
            self.save(job_id)
        self.event(job_id, 'release_rollback_succeeded', '이전 릴리스의 이미지와 HTTP 200을 확인했습니다.')

    def run_release_rollback(self, job_id):
        with self.lock:
            current = json.loads(json.dumps(self.jobs[job_id]))
            previous = json.loads(json.dumps(self.jobs[current['release_rollback_target_id']]))
        def checkpoint(**updates):
            with self.lock:
                self.jobs[job_id].update(updates)
                self.save(job_id)
        try:
            adapter = AwsExpressAdapter(lambda stage, message: self.event(job_id, stage, message),
                                        AwsSettings(**current['aws']))
            outcome = adapter.rollback_release(current['result'], previous['result'],
                                               (previous.get('plan') or {}).get('health_path', '/'), checkpoint)
            verification = None
            if (isinstance(outcome, dict) and outcome.get('state') == 'successful'
                    and outcome.get('url') == previous['result'].get('url')
                    and outcome.get('image') == previous['result'].get('image')
                    and isinstance(outcome.get('service_deployment_arn'), str)):
                verification = {
                    'target_job_id': previous['id'], 'source': 'adapter',
                    'service_deployment_arn': outcome['service_deployment_arn'],
                    'image': outcome['image'], 'url': outcome['url'],
                    'task_definition_arn': previous['result'].get('task_definition_arn'),
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                }
            self.finish_release_rollback(job_id, previous['id'], verification)
        except Exception as exc:
            self.event(job_id, 'release_rollback_failed', str(exc)[:300])
            with self.lock:
                job = self.jobs[job_id]
                job['release_rollback_state'] = ('needs_attention' if job.get('release_rollback_submitted') else 'failed')
                if job.get('release_rollback_submitted'):
                    job['deployment_state'] = 'needs_attention'
                    job['release_rollback_failed_at'] = datetime.now(timezone.utc).isoformat()
                self.save(job_id)

    def reconcile_release_rollback(self, job_id):
        with self.lock:
            current = self.jobs.get(job_id)
            previous = self.jobs.get(current.get('release_rollback_target_id')) if current else None
            if (not current or current.get('release_rollback_state') != 'needs_attention'
                    or not current.get('release_rollback_submitted') or not previous
                    or current.get('deployment_state') != 'needs_attention'
                    or previous.get('deployment_state') != 'superseded'):
                raise ValueError('재확인할 이전 릴리스 롤백이 아닙니다.')
            current_snapshot = json.loads(json.dumps(current))
            previous_snapshot = json.loads(json.dumps(previous))
        result = current_snapshot['result']
        adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(**current_snapshot['aws']))
        listed = json.loads(adapter.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                         '--service', result['service']], private=True, quiet=True))
        items = listed.get('serviceDeployments', [])
        latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
        arn = latest.get('serviceDeploymentArn')
        old_arn = current_snapshot.get('release_rollback_previous_deployment_arn')
        no_new = arn == old_arn
        if not arn or latest.get('status') not in {'SUCCESSFUL', 'ROLLBACK_SUCCESSFUL'}:
            return {'reconciled': False, 'reason': 'ECS 배포 또는 롤백이 아직 완료되지 않았습니다.'}
        if no_new:
            try:
                failed_at = datetime.fromisoformat(current_snapshot['release_rollback_failed_at'])
                if failed_at.tzinfo is None or datetime.now(timezone.utc) - failed_at < timedelta(minutes=10):
                    return {'reconciled': False, 'reason': 'AWS 배포 이력 반영을 10분간 기다립니다.'}
            except (KeyError, TypeError, ValueError):
                return {'reconciled': False, 'reason': '롤백 실패 시점을 확인할 수 없습니다.'}
        service_data = json.loads(adapter.aws(['ecs', 'describe-express-gateway-service',
                                               '--service-arn', result['service_arn'], '--include', 'TAGS'],
                                              private=True, quiet=True)).get('service', {})
        tags = {item.get('key'): item.get('value') for item in service_data.get('tags', [])}
        owner = result.get('owner_attempt') or result['service'].removeprefix('sky-')
        configs = service_data.get('activeConfigurations', [])
        active = configs[0] if len(configs) == 1 else {}
        previous_result = previous_snapshot['result']
        previous_task_arn = (previous_result.get('task_definition_arn')
                             or (result.get('previous_task_definition_arn')
                                 if current_snapshot.get('replaces_job_id') == previous_snapshot['id'] else None))
        if (service_data.get('serviceArn') != result['service_arn']
                or service_data.get('status', {}).get('statusCode') != 'ACTIVE'
                or service_data.get('currentDeployment')
                or len(configs) != 1
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-attempt') != owner):
            return {'reconciled': False, 'reason': 'ECS 서비스 소유권 또는 완료 상태를 확인할 수 없습니다.'}
        if (current_snapshot.get('replaces_job_id') == previous_snapshot['id']
                and previous_result.get('task_definition_arn') and result.get('previous_task_definition_arn')
                and previous_result['task_definition_arn'] != result['previous_task_definition_arn']):
            return {'reconciled': False, 'reason': '이전 릴리스의 태스크 정의 기록이 일치하지 않습니다.'}
        if (not no_new and latest['status'] == 'SUCCESSFUL'
                and active.get('primaryContainer', {}).get('image') == previous_result['image']
                and previous_task_arn and active.get('taskDefinitionArn') == previous_task_arn):
            previous_snapshot['deployment_state'] = 'active'
            health = check_deployment(previous_snapshot)
            if health['healthy']:
                verification = {
                    'target_job_id': previous_snapshot['id'], 'source': 'reconcile',
                    'service_deployment_arn': arn,
                    'image': previous_result['image'], 'url': previous_result['url'],
                    'task_definition_arn': previous_task_arn,
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                }
                self.finish_release_rollback(job_id, previous_snapshot['id'], verification)
                return {'reconciled': True, 'release': 'previous', 'health': health}
        if (active.get('primaryContainer', {}).get('image') == result['image']
                and result.get('task_definition_arn')
                and active.get('taskDefinitionArn') == result['task_definition_arn']
                and (no_new or latest['status'] == 'ROLLBACK_SUCCESSFUL')):
            current_snapshot['deployment_state'] = 'active'
            health = check_deployment(current_snapshot)
            if health['healthy']:
                with self.lock:
                    self.jobs[job_id]['release_rollback_state'] = 'failed'
                    self.jobs[job_id]['deployment_state'] = 'active'
                    self.save(job_id)
                self.event(job_id, 'release_rollback_reconciled', '기존 릴리스가 계속 실행 중임을 확인했습니다.')
                return {'reconciled': True, 'release': 'current', 'health': health}
        return {'reconciled': False, 'reason': '실행 중인 ECS 이미지를 확정할 수 없습니다.'}


def handler_for(app: App):
    class Handler(BaseHTTPRequestHandler):
        def json_response(self, status, data):
            payload = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/health":
                # Service liveness only; user-app readiness is checked separately.
                self.json_response(200, {"status": "ok"})
                return
            if self.path == "/":
                content = (ASSET_ROOT / "static/index.html").read_text()
                payload = content.replace("__TOKEN__", app.token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.headers.get("X-Sky-Token") != app.token:
                self.json_response(403, {"error": "Invalid session token"})
                return
            if self.path == "/api/config":
                self.json_response(200, {"ai_available": app.ai_settings.available,
                    "ai_model": app.ai_settings.model if app.ai_settings.available else None,
                    "monitor_interval": app.monitor_interval,
                    "github_poll_interval": app.github_poll_interval,
                    "targets": [{"id": "auto", "name": "자동 선택 (AI·정적 규칙)", "available": app.ai_settings.available},
                                {"id": "local-docker", "name": "Local Docker", "available": True},
                                {"id": "onprem-compose", "name": "On-prem Compose (same PC)",
                                 "available": LocalComposeAdapter.unavailable_reason() is None,
                                 "reason": LocalComposeAdapter.unavailable_reason()},
                                {"id": "onprem-vm", "name": "On-prem Linux VM (SSH)",
                                 "available": RemoteVmComposeAdapter.unavailable_reason() is None,
                                 "reason": RemoteVmComposeAdapter.unavailable_reason()},
                                {"id": "cloud-run", "name": "Google Cloud Run",
                                 "available": app.cloud_settings.unavailable_reason() is None,
                                 "reason": app.cloud_settings.unavailable_reason()},
                                {"id": "aws-ecs-express", "name": "AWS ECS Express Mode",
                                 "available": app.aws_settings.unavailable_reason() is None,
                                 "reason": app.aws_settings.unavailable_reason()},
                                {"id": "aws-s3-cloudfront", "name": "AWS S3 + CloudFront (정적 사이트)",
                                 "available": AwsStaticSiteAdapter.unavailable_reason(app.aws_settings) is None,
                                 "reason": AwsStaticSiteAdapter.unavailable_reason(app.aws_settings)}],
                    "recovery_warnings": app.recovery_warnings})
                return
            if self.path == "/api/jobs":
                self.json_response(200, app.summaries())
                return
            if self.path == "/api/github/sources":
                self.json_response(200, app.github_source_summaries())
                return
            if re.fullmatch(r'/api/deployment-groups/[a-f0-9]{16}', self.path):
                try:
                    self.json_response(200, app.deployment_group(self.path.rsplit('/', 1)[-1]))
                except ValueError as exc:
                    self.json_response(404, {'error': str(exc)})
                return
            if self.path == "/api/aws/default-network":
                try:
                    self.json_response(200, discover_default_network(app.aws_settings))
                except (ValueError, AwsConfigurationError) as exc:
                    self.json_response(400, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/network/operation", self.path):
                application_id = self.path.split('/')[3]
                try:
                    self.json_response(200, app.network_operations.get(application_id))
                except ValueError as exc:
                    self.json_response(404, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/backups", self.path):
                application_id = self.path.split('/')[3]
                try:
                    self.json_response(200, inspect_postgres_backup_status(
                        application_id, app.aws_settings))
                except (ValueError, AwsConfigurationError) as exc:
                    self.json_response(400, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/snapshots/[a-z][a-z0-9-]{2,254}/operation", self.path):
                application_id, snapshot_id = self.path.split('/')[3], self.path.split('/')[5]
                try:
                    self.json_response(200, app.snapshot_operations.get(application_id, snapshot_id))
                except ValueError as exc:
                    self.json_response(404, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres", self.path):
                application_id = self.path.split('/')[3]
                try:
                    self.json_response(200, discover_existing_postgres(application_id, app.aws_settings))
                except (ValueError, AwsConfigurationError) as exc:
                    self.json_response(400, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/operation", self.path):
                application_id = self.path.split('/')[3]
                try:
                    operation = app.postgres_operations.get(application_id)
                    if app.postgres_retirement_operations.blocks_deployment(application_id):
                        retirement = app.postgres_retirement_operations.get(application_id)
                        operation = {**operation,
                            'status': 'retired' if retirement['status'] == 'succeeded' else 'needs_attention',
                            'message': 'PostgreSQL 폐기 기록이 있습니다. 폐기 상태를 확인하세요.'}
                    self.json_response(200, operation)
                except ValueError as exc:
                    self.json_response(404, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/retirement/operation", self.path):
                application_id = self.path.split('/')[3]
                try:
                    self.json_response(200, app.postgres_retirement_operations.get(application_id))
                except ValueError as exc:
                    self.json_response(404, {"error": str(exc)})
                return
            if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/releases", self.path):
                application_id = self.path.split('/')[3]
                self.json_response(200, app.releases(application_id))
                return
            if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/health", self.path):
                job_id = self.path.split('/')[3]
                with app.lock:
                    job = app.jobs.get(job_id)
                    snapshot = json.loads(json.dumps(job)) if job else None
                if not snapshot:
                    self.json_response(404, {"error": "Not found"})
                elif snapshot.get('status') != 'succeeded':
                    self.json_response(409, {"error": "Only completed deployments can be checked"})
                elif snapshot.get('deployment_state', 'active') in {'deleting', 'deleted'}:
                    self.json_response(409, {"error": "배포 종료 중이거나 이미 종료됐습니다."})
                else:
                    self.json_response(200, app.check_and_record_health(job_id))
                return
            if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/certificate", self.path):
                job_id = self.path.split('/')[3]
                with app.lock:
                    job = app.jobs.get(job_id)
                    snapshot = json.loads(json.dumps(job)) if job else None
                    history = json.loads(json.dumps(app.health_history.get(job_id, [])))
                if snapshot:
                    self.json_response(200, deployment_certificate(snapshot, history))
                else:
                    self.json_response(404, {"error": "Not found"})
                return
            if self.path.startswith("/api/jobs/"):
                with app.lock:
                    job_id = self.path.rsplit("/", 1)[-1]
                    job = app.jobs.get(job_id)
                    response = ({**job, 'diagnosis': deployment_diagnosis(job),
                                 'health_history': app.health_history.get(job_id, []),
                                 'last_health': (app.health_history.get(job_id) or [None])[-1],
                                 'monitor_error': app.monitor_errors.get(job_id)}
                                if job else {"error": "Not found"})
                    self.json_response(200 if job else 404, response)
                return
            self.json_response(404, {"error": "Not found"})

        def do_POST(self):
            if self.headers.get("X-Sky-Token") != app.token:
                self.json_response(403, {"error": "Invalid session token"})
                return
            try:
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/websocket-probe", self.path):
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('WebSocket 검사 요청에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.check_and_record_websocket(self.path.split('/')[3]))
                    return
                if self.path == '/api/github/deployments':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 2048:
                        raise ValueError('GitHub 배포 요청은 2 KiB 이하여야 합니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {
                            'repository_url', 'branch', 'application_id', 'targets',
                            'public', 'auto_deploy'}):
                        raise ValueError('GitHub 저장소와 배포 설정이 필요합니다.')
                    self.json_response(202, app.create_github_deployment(
                        payload['repository_url'], payload['branch'], payload['application_id'],
                        payload['targets'], payload['public'], payload['auto_deploy']))
                    return
                if re.fullmatch(r'/api/github/sources/[a-f0-9]{16}/(pause|resume|check|retry|disconnect)', self.path):
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('GitHub 연결 작업에는 본문이 없어야 합니다.')
                    source_id, operation = self.path.split('/')[4:6]
                    if operation == 'check':
                        self.json_response(200, app.poll_github_source(source_id))
                    elif operation == 'retry':
                        self.json_response(202, app.poll_github_source(source_id, retry_failed=True))
                    elif operation == 'disconnect':
                        self.json_response(200, app.remove_github_source(source_id))
                    else:
                        self.json_response(200, app.set_github_source_enabled(
                            source_id, operation == 'resume'))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/network/plan", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 256:
                        raise ValueError('서비스 네트워크 계획 입력은 256바이트 이하여야 합니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {'vpc_id'}
                            or not isinstance(payload['vpc_id'], str)):
                        raise ValueError('VPC ID가 필요합니다.')
                    request = ServiceNetworkRequest(application_id, app.aws_settings.expected_account,
                                                    app.aws_settings.region, payload['vpc_id'])
                    self.json_response(200, app.network_operations.plan(request))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/network/create", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 256:
                        raise ValueError('서비스 네트워크 생성 요청 본문이 올바르지 않습니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {'plan_id'}
                            or not isinstance(payload['plan_id'], str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{24,64}', payload['plan_id'])):
                        raise ValueError('유효한 네트워크 계획 ID가 필요합니다.')
                    self.json_response(202, app.network_operations.start(application_id, payload['plan_id']))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/network/reconcile", self.path):
                    application_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('서비스 네트워크 재확인 요청에는 본문이 없어야 합니다.')
                    self.json_response(200, app.network_operations.reconcile(application_id))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/plan", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1024:
                        raise ValueError('PostgreSQL 생성 계획 입력은 1 KiB 이하여야 합니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {'vpc_id', 'subnet_ids'}
                            or not isinstance(payload['vpc_id'], str)
                            or not isinstance(payload['subnet_ids'], list)
                            or any(not isinstance(value, str) for value in payload['subnet_ids'])):
                        raise ValueError('VPC ID와 서브넷 ID 목록이 필요합니다.')
                    settings = postgres_settings_for_application(
                        application_id, payload['vpc_id'], app.aws_settings)
                    request = PostgresRequest(application_id, settings.expected_account, settings.region,
                        payload['vpc_id'], tuple(payload['subnet_ids']), settings.service_security_group)
                    request.validate()
                    self.json_response(200, app.postgres_operations.plan(request))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/create", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 256:
                        raise ValueError('PostgreSQL 생성 요청 본문이 올바르지 않습니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {'plan_id'}
                            or not isinstance(payload['plan_id'], str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{24,64}', payload['plan_id'])):
                        raise ValueError('유효한 생성 계획 ID가 필요합니다.')
                    self.json_response(202, app.postgres_operations.start(application_id, payload['plan_id']))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/reconcile", self.path):
                    application_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('PostgreSQL 생성 재확인 요청에는 본문이 없어야 합니다.')
                    self.json_response(200, app.postgres_operations.reconcile(application_id))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/failed-create/plan", self.path):
                    application_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('실패 스택 정리 계획 조회에는 본문이 없어야 합니다.')
                    self.json_response(200, app.postgres_operations.cleanup_plan(application_id))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/failed-create/start", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 512:
                        raise ValueError('실패 스택 정리 요청 본문이 올바르지 않습니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict)
                            or set(payload) != {'plan_id', 'confirm_stack_id'}
                            or not isinstance(payload['plan_id'], str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{24,64}', payload['plan_id'])
                            or not isinstance(payload['confirm_stack_id'], str)
                            or len(payload['confirm_stack_id']) > 256):
                        raise ValueError('유효한 계획 ID와 스택 ARN이 필요합니다.')
                    self.json_response(202, app.postgres_operations.cleanup_start(
                        application_id, payload['plan_id'], payload['confirm_stack_id']))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/retirement/plan", self.path):
                    application_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('DB 폐기 계획 조회에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.postgres_retirement_operations.plan(application_id))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/postgres/retirement/start", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 256:
                        raise ValueError('DB 폐기 요청 본문이 올바르지 않습니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict)
                            or set(payload) != {'plan_id', 'confirm_database_id'}
                            or not isinstance(payload['plan_id'], str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{24,64}', payload['plan_id'])
                            or not isinstance(payload['confirm_database_id'], str)
                            or not re.fullmatch(r'sky-[a-z][a-z0-9-]{2,30}', payload['confirm_database_id'])):
                        raise ValueError('유효한 폐기 계획 ID와 정확한 DB ID가 필요합니다.')
                    with app.lock:
                        if any(job.get('application_id') == application_id
                               and job.get('status') in {'provisioning', 'running', 'waiting_input'}
                               for job in app.jobs.values()):
                            raise ValueError('이 앱의 배포가 진행 중입니다. 완료 후 다시 시도하세요.')
                        result = app.postgres_retirement_operations.start(
                            application_id, payload['plan_id'], payload['confirm_database_id'])
                    self.json_response(202, result)
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/snapshots/plan", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 512:
                        raise ValueError('스냅샷 계획 입력은 512바이트 이하여야 합니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {'snapshot_id'}
                            or not isinstance(payload['snapshot_id'], str)):
                        raise ValueError('수동 스냅샷 ID가 필요합니다.')
                    self.json_response(200, app.snapshot_operations.plan(
                        application_id, payload['snapshot_id']))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/snapshots/create", self.path):
                    application_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 256:
                        raise ValueError('스냅샷 생성 요청 본문이 올바르지 않습니다.')
                    payload = json.loads(self.rfile.read(size))
                    if (not isinstance(payload, dict) or set(payload) != {'plan_id'}
                            or not isinstance(payload['plan_id'], str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{24,64}', payload['plan_id'])):
                        raise ValueError('유효한 스냅샷 계획 ID가 필요합니다.')
                    self.json_response(202, app.snapshot_operations.start(application_id, payload['plan_id']))
                    return
                if re.fullmatch(r"/api/applications/[a-z][a-z0-9-]{2,30}/snapshots/[a-z][a-z0-9-]{2,254}/reconcile", self.path):
                    application_id, snapshot_id = self.path.split('/')[3], self.path.split('/')[5]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('스냅샷 재확인 요청에는 본문이 없어야 합니다.')
                    self.json_response(200, app.snapshot_operations.reconcile(application_id, snapshot_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/rollback-release/reconcile", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('릴리스 롤백 재확인 요청에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.reconcile_release_rollback(job_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/rollback-release", self.path):
                    job_id = self.path.split('/')[3]
                    size = int(self.headers.get('Content-Length', '0'))
                    if size < 0 or size > 512:
                        raise ValueError('릴리스 롤백 요청 본문은 512바이트 이하이어야 합니다.')
                    payload = json.loads(self.rfile.read(size)) if size else {}
                    if (not isinstance(payload, dict) or set(payload) not in (set(), {'target_job_id'})
                            or ('target_job_id' in payload and
                                (not isinstance(payload['target_job_id'], str)
                                 or not re.fullmatch(r'[a-f0-9]{16}', payload['target_job_id'])))):
                        raise ValueError('롤백 대상 작업 ID가 올바르지 않습니다.')
                    app.start_release_rollback(job_id, payload.get('target_job_id'))
                    self.json_response(202, {'id': job_id, 'release_rollback_state': 'running'})
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/rollback", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('롤백 요청에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.request_aws_update_rollback(job_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/cleanup-image", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('이미지 정리 요청에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.cleanup_abandoned_aws_image(job_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/migration/inspect", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('SQL 마이그레이션 재확인 요청에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.inspect_interrupted_aws_migration(job_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/migration/cleanup", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('SQL 마이그레이션 정리 요청에는 본문을 넣을 수 없습니다.')
                    self.json_response(200, app.cleanup_interrupted_aws_migration(job_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/reconcile", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('AWS 상태 재확인 요청에는 본문을 넣을 수 없습니다.')
                    with app.lock:
                        static_site = app.jobs.get(job_id, {}).get('mode') == 'static_site'
                    self.json_response(200, app.reconcile_static_site(job_id) if static_site
                                       else app.reconcile_aws_update(job_id))
                    return
                if re.fullmatch(r"/api/jobs/[a-f0-9]{16}/retire", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('배포 종료 요청에는 본문을 넣을 수 없습니다.')
                    with app.lock:
                        job = app.jobs.get(job_id)
                        orphan_local = (job and job.get('mode') == 'agent'
                                        and job.get('target') in {'local-docker', 'onprem-compose', 'onprem-vm'}
                                        and job.get('status') in {'failed', 'interrupted'}
                                        and not job.get('result')
                                        and type(job.get('attempts')) is int
                                        and 1 <= job['attempts'] <= 3)
                        orphan_static = (job and job.get('mode') == 'static_site'
                                         and job.get('status') in {'failed', 'interrupted'}
                                         and job.get('static_stack_id'))
                        successful = (job and job.get('status') == 'succeeded'
                                      and job.get('target') in {'aws-ecs-express', 'aws-s3-cloudfront', 'local-docker', 'onprem-compose', 'onprem-vm', 'cloud-run'}
                                      and job.get('result'))
                        if (not (orphan_local or orphan_static or successful)
                                or job.get('deployment_state', 'active') not in (
                                    {'active', 'delete_failed', 'needs_attention'}
                                    if job.get('target') == 'aws-s3-cloudfront' else {'active', 'delete_failed'})
                                or job.get('release_rollback_state') in {'running', 'needs_attention'}
                                or (job.get('target') in {'aws-ecs-express', 'aws-s3-cloudfront'} and any(other is not job and other.get('application_id') == job.get('application_id')
                                       and other.get('target') == job.get('target')
                                       and (other.get('status') in {'provisioning', 'running', 'waiting_input'}
                                            or other.get('aws_image_cleanup_state') == 'running')
                                       for other in app.jobs.values()))):
                            message = ('종료할 수 있는 AWS 배포가 아닙니다.' if job and job.get('target') == 'aws-ecs-express'
                                       else '종료할 수 있는 배포가 아닙니다.')
                            self.json_response(409, {'error': message})
                            return
                        github_source_id = (job.get('github_source') or {}).get('subscription_id')
                        if github_source_id and app.github_sources.get(github_source_id, {}).get('enabled'):
                            self.json_response(409, {'error': '앱 종료 전에 GitHub 자동 배포를 일시 중지하거나 연결 해제하세요.'})
                            return
                        target = job['target']
                        job['deployment_state'] = 'deleting'
                        job.pop('retire_error', None)
                        app.save(job_id)
                    threading.Thread(target=app.retire_static_site if target == 'aws-s3-cloudfront' else
                                     app.retire_aws if target == 'aws-ecs-express' else
                                     app.retire_cloud if target == 'cloud-run' else
                                     app.retire_compose if target == 'onprem-compose' else
                                     app.retire_vm if target == 'onprem-vm' else app.retire_local,
                                     args=(job_id,), daemon=True).start()
                    self.json_response(202, {'id': job_id, 'deployment_state': 'deleting'})
                    return
                if self.path == "/api/compatibility":
                    public_flag = self.headers.get('X-Public-Access', 'false')
                    if public_flag not in {'true', 'false'}:
                        raise ValueError('공개 접근 선택 값이 올바르지 않습니다.')
                    size = int(self.headers.get('Content-Length', '0'))
                    content_type = self.headers.get('Content-Type', '')
                    folder_upload = content_type.lower().startswith('multipart/form-data;')
                    if not 0 < size <= MAX_UPLOAD + (1024 * 1024 if folder_upload else 0):
                        raise ValueError('업로드는 20 MiB 이하여야 합니다.')
                    with tempfile.TemporaryDirectory(prefix='sky-compatibility-') as temporary:
                        archive = Path(temporary) / 'app.zip'
                        body = self.rfile.read(size)
                        if folder_upload:
                            folder_upload_to_zip(body, content_type, archive)
                        else:
                            archive.write_bytes(body)
                        project = extract_project(archive, Path(temporary) / 'source')
                        profile = inspect_infrastructure(project)
                        static_site = assess_static_site(project, profile)
                        local_sqlite_mount = self.headers.get('X-Local-Sqlite-Mount')
                        local_sqlite_binding = None
                        if local_sqlite_mount is not None:
                            local_sqlite_binding = preflight_local_sqlite(
                                project, profile, self.headers.get('X-Application-Id', ''),
                                local_sqlite_mount)
                        digest = source_digest(project)
                        availability = {'local-docker': None,
                                        'onprem-compose': LocalComposeAdapter.unavailable_reason(),
                                        'onprem-vm': RemoteVmComposeAdapter.unavailable_reason(),
                                        'aws-ecs-express': app.aws_settings.unavailable_reason(),
                                        'cloud-run': app.cloud_settings.unavailable_reason()}
                        reports, candidates = compare_targets(
                            profile, availability, public_access=public_flag == 'true',
                            local_sqlite=local_sqlite_binding is not None, include_compose=True)
                        static_configuration_reason = AwsStaticSiteAdapter.unavailable_reason(app.aws_settings)
                        candidates.append(static_hosting_candidate(
                            static_site, configured=static_configuration_reason is None,
                            public_access=public_flag == 'true',
                            configuration_reason=static_configuration_reason))
                        policy = deployment_policy('auto', public_flag == 'true')
                    self.json_response(200, {'source_digest': digest,
                                             'application_ir': application_ir(profile, digest).as_dict(),
                                             'deployment_policy': policy.as_dict(),
                                             'capability_models': {
                                                 target: target_capability_model(target).as_dict()
                                                 for target in (*availability, 'aws-s3-cloudfront', 'aws-ecs-standard')
                                             },
                                             'inspection': {
                                                 'requirements': list(profile.requirements),
                                                 'evidence_files': list(profile.evidence),
                                                 'scanned_files': profile.scanned_files,
                                             },
                                             'static_site': {**static_site.as_dict(),
                                                 'target': 'aws-s3-cloudfront',
                                                 'adapter_status': 'available' if
                                                 static_configuration_reason is None
                                                 else 'unavailable'},
                                             'reports': reports,
                                             'local_sqlite_binding': local_sqlite_binding,
                                             'candidates': candidates})
                    return
                if self.path == '/api/deployment-groups':
                    if not app.ai_settings.available:
                        self.json_response(503, {'error': 'AI 배포를 사용하려면 OPENAI_API_KEY가 필요합니다.'})
                        return
                    targets = self.headers.get('X-Deploy-Targets', '').split(',')
                    application_id = self.headers.get('X-Application-Id', '')
                    public_flag = self.headers.get('X-Public-Access', 'false')
                    if public_flag not in {'true', 'false'}:
                        raise ValueError('공개 접근 선택이 올바르지 않습니다.')
                    if any(name.lower().startswith(('x-postgres-', 'x-sqlite-', 'x-local-sqlite-'))
                           for name in self.headers):
                        raise ValueError('다중 대상 배포는 현재 데이터베이스 연결·이전을 지원하지 않습니다.')
                    if 'aws-ecs-express' in targets and public_flag != 'true':
                        raise ValueError('AWS ECS Express를 포함하려면 인터넷 공개를 허용하세요.')
                    size = int(self.headers.get('Content-Length', '0'))
                    content_type = self.headers.get('Content-Type', '')
                    folder_upload = content_type.lower().startswith('multipart/form-data;')
                    if not 0 < size <= MAX_UPLOAD + (1024 * 1024 if folder_upload else 0):
                        raise ValueError('업로드 크기는 20 MiB 이하여야 합니다.')
                    with tempfile.TemporaryDirectory(prefix='.group-upload-', dir=app.root) as folder:
                        directory = Path(folder)
                        archive = directory / 'source.zip'
                        upload = self.rfile.read(size)
                        if len(upload) != size:
                            raise ValueError('업로드가 완료되지 않았습니다.')
                        if folder_upload:
                            folder_upload_to_zip(upload, content_type, archive)
                        else:
                            archive.write_bytes(upload)
                        project = extract_project(archive, directory / 'source')
                        group = app.create_deployment_group(
                            project, application_id, targets, public_flag == 'true')
                    try:
                        app.start_group_worker(group['id'])
                    except Exception:
                        with app.lock:
                            for child in group['targets']:
                                job = app.jobs[child['job_id']]
                                job['status'] = 'interrupted'
                                app.save(job['id'])
                    self.json_response(202, app.deployment_group(group['id']))
                    return
                if re.fullmatch(r'/api/deployment-groups/[a-f0-9]{16}/continue', self.path):
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('계속 요청에는 본문이 없어야 합니다.')
                    group_id = self.path.split('/')[3]
                    group = app.deployment_group(group_id)
                    if (group['status'] != 'interrupted'
                            or not any(child['status'] == 'planned' for child in group['targets'])):
                        raise ValueError('대기 중인 대상이 있는 중단된 배포 묶음만 계속할 수 있습니다.')
                    app.start_group_worker(group_id)
                    self.json_response(202, app.deployment_group(group_id))
                    return
                if self.path == "/api/static-deployments":
                    if self.headers.get("X-Public-Access") != "true":
                        raise ValueError("CloudFront 정적 사이트는 공개 HTTPS 배포 동의가 필요합니다.")
                    reason = AwsStaticSiteAdapter.unavailable_reason(app.aws_settings)
                    if reason:
                        raise ValueError(reason)
                    application_id = self.headers.get("X-Application-Id", "")
                    if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", application_id):
                        raise ValueError("앱 ID는 소문자로 시작하는 3~31자의 소문자·숫자·하이픈이어야 합니다.")
                    size = int(self.headers.get("Content-Length", "0"))
                    content_type = self.headers.get("Content-Type", "")
                    folder_upload = content_type.lower().startswith("multipart/form-data;")
                    if not 0 < size <= MAX_UPLOAD + (1024 * 1024 if folder_upload else 0):
                        raise ValueError("업로드 크기는 20 MiB 이하여야 합니다.")
                    job_id = uuid.uuid4().hex[:16]
                    directory = app.root / job_id
                    directory.mkdir()
                    try:
                        (directory / ".uncommitted-upload").touch(mode=0o600)
                        archive = directory / "source.zip"
                        upload = self.rfile.read(size)
                        if len(upload) != size:
                            raise ValueError("업로드가 완료되지 않았습니다.")
                        if folder_upload:
                            folder_upload_to_zip(upload, content_type, archive)
                        else:
                            archive.write_bytes(upload)
                        try:
                            project = extract_project(archive, directory / "source")
                        finally:
                            archive.unlink(missing_ok=True)
                        app.create_static_job(
                            job_id, project, application_id, requested_target="aws-s3-cloudfront"
                        )
                        app.clear_upload_marker(directory)
                    except Exception:
                        if not (directory / "job.json").is_file():
                            with app.lock:
                                app.jobs.pop(job_id, None)
                            shutil.rmtree(directory, ignore_errors=True)
                        raise
                    started = app.start_job_worker(job_id, app.run_static_site)
                    self.json_response(202, {"id": job_id, "status": "running" if started else "interrupted"})
                    return
                if self.path == "/api/deployments":
                    if not app.ai_settings.available:
                        self.json_response(503, {"error": "AI 배포를 사용하려면 서버에 OPENAI_API_KEY를 설정하세요."})
                        return
                    requested_target = self.headers.get("X-Deploy-Target", "local-docker")
                    if requested_target not in {"auto", "local-docker", "onprem-compose", "onprem-vm", "cloud-run", "aws-ecs-express"}:
                        raise ValueError("Unsupported deployment target")
                    target = requested_target
                    public_flag = self.headers.get("X-Public-Access", "false")
                    if public_flag not in {"true", "false"}:
                        raise ValueError("Invalid public access selection")
                    if target == "cloud-run" and app.cloud_settings.unavailable_reason():
                        raise ValueError(app.cloud_settings.unavailable_reason())
                    if target == 'onprem-compose' and LocalComposeAdapter.unavailable_reason():
                        raise ValueError(LocalComposeAdapter.unavailable_reason())
                    if target == 'onprem-vm' and RemoteVmComposeAdapter.unavailable_reason():
                        raise ValueError(RemoteVmComposeAdapter.unavailable_reason())
                    if target == "aws-ecs-express":
                        if app.aws_settings.unavailable_reason():
                            raise ValueError(app.aws_settings.unavailable_reason())
                    if target != 'auto' and deployment_access_mode(target, public_flag == 'true') is None:
                        raise ValueError('AWS ECS Express 대상은 인터넷 공개 선택이 필요합니다.')
                    size = int(self.headers.get("Content-Length", "0"))
                    content_type = self.headers.get('Content-Type', '')
                    folder_upload = content_type.lower().startswith('multipart/form-data;')
                    if not 0 < size <= MAX_UPLOAD + (1024 * 1024 if folder_upload else 0):
                        raise ValueError("Upload must be smaller than 20 MiB (plus folder form overhead)")
                    job_id = uuid.uuid4().hex[:16]
                    application_id = self.headers.get("X-Application-Id", "app-" + job_id)
                    if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", application_id):
                        raise ValueError("Application ID must be 3-31 lowercase letters, digits or hyphens, starting with a letter")
                    postgres_flag = self.headers.get('X-Postgres-Existing', 'false')
                    if postgres_flag not in {'true', 'false'}:
                        raise ValueError('기존 PostgreSQL 선택 값이 올바르지 않습니다.')
                    sqlite_flag = self.headers.get('X-Sqlite-Convert', 'false')
                    if sqlite_flag not in {'true', 'false'}:
                        raise ValueError('SQLite 변환 선택 값이 올바르지 않습니다.')
                    local_sqlite_mount = self.headers.get('X-Local-Sqlite-Mount')
                    if local_sqlite_mount is not None:
                        if target not in {'auto', 'local-docker', 'onprem-compose'} or sqlite_flag == 'true' or postgres_flag == 'true':
                            raise ValueError('Local SQLite 볼륨은 단일 로컬 배포에서만 사용합니다.')
                        if len(local_sqlite_mount) > 200 or not local_sqlite_mount.startswith('/'):
                            raise ValueError('Local SQLite 볼륨 경로는 컨테이너 내부의 절대 경로여야 합니다.')
                    create_plan_id = self.headers.get('X-Postgres-Create-Plan')
                    if create_plan_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{24,64}', create_plan_id):
                        raise ValueError('유효한 PostgreSQL 생성 계획 ID가 필요합니다.')
                    if create_plan_id is not None and postgres_flag == 'true':
                        raise ValueError('기존 DB 사용과 신규 DB 생성을 동시에 선택할 수 없습니다.')
                    policy = deployment_policy(
                        requested_target, public_flag == 'true',
                        new_managed_database_approved=create_plan_id is not None,
                        allow_data_migration=sqlite_flag == 'true',
                    )
                    postgres_headers = ('X-Postgres-Vpc-Id', 'X-Postgres-Subnet-Ids')
                    supplied_postgres_network = tuple(name in self.headers for name in postgres_headers)
                    if postgres_flag != 'true' and any(supplied_postgres_network):
                        raise ValueError('PostgreSQL 연결 정보에는 기존 DB 명시적 선택이 필요합니다.')
                    postgres_request = None
                    aws_settings_for_job = app.aws_settings
                    if postgres_flag == 'true' or create_plan_id is not None:
                        settings = app.aws_settings
                        if (target not in {'auto', 'aws-ecs-express'} or public_flag != 'true'
                                or not settings.expected_account):
                            raise ValueError('PostgreSQL 경로에는 공개 AWS 대상과 계정 고정이 필요합니다.')
                        if settings.unavailable_reason():
                            raise ValueError(settings.unavailable_reason())
                        if create_plan_id is not None:
                            postgres_request = app.postgres_operations.reviewed_request(
                                application_id, create_plan_id)
                            vpc_id = postgres_request.vpc_id
                        else:
                            if supplied_postgres_network == (True, False) or supplied_postgres_network == (False, True):
                                raise ValueError('PostgreSQL VPC와 서브넷 입력은 함께 지정하세요.')
                            if supplied_postgres_network == (False, False):
                                discovered = discover_existing_postgres(application_id, settings)
                                vpc_id = discovered['vpc_id']
                                subnet_ids = tuple(discovered['subnet_ids'])
                            else:
                                vpc_id = self.headers['X-Postgres-Vpc-Id']
                                subnet_ids = tuple(part.strip() for part in
                                                   self.headers['X-Postgres-Subnet-Ids'].split(','))
                        aws_settings_for_job = postgres_settings_for_application(
                            application_id, vpc_id, settings)
                        if create_plan_id is not None:
                            if (postgres_request.account != settings.expected_account
                                    or postgres_request.region != settings.region
                                    or postgres_request.service_security_group != aws_settings_for_job.service_security_group):
                                raise ValueError('검토한 DB 계획과 현재 AWS 계정·네트워크 설정이 다릅니다.')
                        else:
                            postgres_request = PostgresRequest(application_id, settings.expected_account,
                                settings.region, vpc_id, subnet_ids,
                                aws_settings_for_job.service_security_group)
                        postgres_request.validate()
                        if target == 'auto':
                            target = 'aws-ecs-express'
                    directory = app.root / job_id
                    directory.mkdir()
                    try:
                        (directory / '.uncommitted-upload').touch(mode=0o600)
                        archive = directory / "source.zip"
                        upload = self.rfile.read(size)
                        if len(upload) != size:
                            raise ValueError('Incomplete upload')
                        if folder_upload:
                            folder_upload_to_zip(upload, content_type, archive)
                        else:
                            archive.write_bytes(upload)
                        try:
                            project = extract_project(archive, directory / "source")
                        finally:
                            archive.unlink(missing_ok=True)
                        infrastructure_profile = inspect_infrastructure(project)
                        if (target == 'auto' and public_flag == 'true'
                                and postgres_request is None and local_sqlite_mount is None
                                and sqlite_flag == 'false'
                                and AwsStaticSiteAdapter.unavailable_reason(app.aws_settings) is None
                                and assess_static_site(project, infrastructure_profile).status == 'eligible'):
                            app.create_static_job(
                                job_id, project, application_id, requested_target='auto'
                            )
                            app.clear_upload_marker(directory)
                            started = app.start_job_worker(job_id, app.run_static_site)
                            self.json_response(202, {
                                'id': job_id, 'status': 'running' if started else 'interrupted'
                            })
                            return
                        sqlite_conversion = None
                        local_sqlite_binding = None
                        deployment_profile = infrastructure_profile
                        if local_sqlite_mount is not None:
                            if create_plan_id is not None:
                                raise ValueError('Local SQLite 볼륨과 RDS 생성은 함께 사용할 수 없습니다.')
                            local_sqlite_binding = preflight_local_sqlite(
                                project, infrastructure_profile, application_id, local_sqlite_mount)
                            if target == 'auto':
                                target = 'local-docker'
                        if sqlite_flag == 'true':
                            if postgres_request is None:
                                raise ValueError('SQLite 자동 이전은 PostgreSQL RDS 선택이 필요합니다.')
                            sqlite_conversion, deployment_profile = preflight_sqlite_conversion(
                                project, infrastructure_profile)
                        validate_infrastructure(deployment_profile, target,
                                                postgres=postgres_request is not None,
                                                local_sqlite=local_sqlite_binding is not None)
                        if postgres_request is not None:
                            if sqlite_conversion is None:
                                collect_sql_migrations(project)
                            if create_plan_id is None:
                                database = AwsPostgresProvisioner(postgres_request).inspect_current()
                                app.postgres_operations.require_deployable(
                                    application_id, database['database_id'])
                        if target == 'auto':
                            availability = {'local-docker': None,
                                            'aws-ecs-express': app.aws_settings.unavailable_reason(),
                                            'cloud-run': app.cloud_settings.unavailable_reason()}
                            _, candidates = compare_targets(infrastructure_profile, availability,
                                                            public_access=public_flag == 'true')
                            available_targets = [item['id'] for item in candidates if item['status'] == 'eligible']
                            if not available_targets:
                                raise ValueError('현재 설정에서 감지된 요구와 호환되는 자동 배포 대상이 없습니다.')
                            infrastructure_plan = plan_infrastructure(project, available_targets,
                                public_flag == 'true', app.infrastructure_planner_factory(app.ai_settings))
                            target = infrastructure_plan['target']
                            validate_infrastructure(infrastructure_profile, target)
                            infrastructure_plan['candidates'] = [
                                {**item, 'selected': item['id'] == target} for item in candidates]
                        else:
                            infrastructure_plan = explicit_infrastructure_plan(
                                target, deployment_profile,
                                existing_postgres_id=database['database_id']
                                if postgres_request is not None and create_plan_id is None else None,
                                create_postgres_id=postgres_request.database_id
                                if create_plan_id is not None else None)
                            if requested_target == 'auto' and postgres_request is not None:
                                infrastructure_plan['planner'] = 'policy'
                                infrastructure_plan['rationale'] = (
                                    'PostgreSQL 연결에는 AWS ECS Express만 지원됩니다. 앱의 PostgreSQL 근거와 ' +
                                    ('검토된 생성 계획을 확인해 AWS를 선택했습니다. DB는 생성 후 앱 실패에도 보존됩니다.'
                                     if create_plan_id is not None else
                                     'RDS 소유권을 확인해 AWS를 선택했습니다. DB는 새로 생성하지 않으며 앱 종료 후에도 보존됩니다.'))
                            if local_sqlite_binding is not None:
                                infrastructure_plan['planner'] = 'user-confirmed-storage'
                                infrastructure_plan['rationale'] = (
                                    '사용자가 로컬 대상과 SQLite DB 디렉터리의 영속 볼륨 경로를 확인했습니다. '
                                    '볼륨은 앱 종료 후에도 보존합니다.')
                                infrastructure_plan['sqlite_volume'] = local_sqlite_binding
                        infrastructure_plan['compatibility'] = infrastructure_compatibility(
                            deployment_profile, target, postgres=postgres_request is not None,
                            local_sqlite=local_sqlite_binding is not None,
                            public_access=public_flag == 'true')
                        if sqlite_conversion is not None:
                            infrastructure_plan['conversion_pending'] = 'sqlite-to-postgresql'
                        access_mode = infrastructure_plan['compatibility']['access_mode']
                        if access_mode is None:
                            raise ValueError('선택한 배포 대상의 공개 범위를 지원하지 않습니다.')
                        policy.require(
                            target, access_mode,
                            new_managed_database=create_plan_id is not None,
                            data_migration=sqlite_conversion is not None,
                        )
                        with app.lock:
                            app.ensure_application_available(application_id, target)
                            if local_sqlite_binding is not None and any(
                                old.get('application_id') == application_id
                                and old.get('target') in {'local-docker', 'onprem-compose'}
                                and old.get('status') == 'succeeded'
                                and old.get('deployment_state', 'active') == 'active'
                                for old in app.jobs.values()
                            ):
                                raise ValueError(
                                    '기존 Local 배포가 실행 중입니다. SQLite 데이터 보호를 위해 종료 후 재배포하세요. '
                                    '앱 볼륨은 종료 후에도 남습니다.')
                            latest = None
                            if target == 'aws-ecs-express':
                                previous = [old for old in app.jobs.values()
                                            if old.get('application_id') == application_id
                                            and old.get('target') == 'aws-ecs-express'
                                            and old.get('status') == 'succeeded'
                                            and old.get('deployment_state', 'active') == 'active'
                                            and old.get('result')]
                                if previous:
                                    if create_plan_id is not None:
                                        raise ValueError('활성 AWS 릴리스가 있는 앱에는 신규 DB 생성·배포를 시작할 수 없습니다.')
                                    latest = max(previous, key=lambda item: item.get('created_at', ''))
                                    if postgres_request is None and latest['result'].get('database') is not None:
                                        raise ValueError('기존 PostgreSQL 서비스 업데이트에는 동일한 DB 연결 요청이 필요합니다.')
                                    if postgres_request is not None and latest['result'].get('database') != database:
                                        raise ValueError('기존 AWS 서비스의 PostgreSQL 연결 기록이 현재 DB와 다릅니다.')
                            digest = source_digest(project)
                            app.jobs[job_id] = {"id": job_id, "mode": "agent", "target": target,
                                "requested_target": requested_target, "infrastructure_plan": infrastructure_plan,
                                "application_id": application_id,
                                "public": access_mode == 'public',
                                "status": "provisioning" if create_plan_id is not None else "running",
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "plan": None, "diff": "", "changes": [], "steps": 0, "attempts": 0,
                                "project": str(project), "infrastructure_profile": infrastructure_profile.as_dict(),
                                "application_ir": application_ir(infrastructure_profile, digest).as_dict(),
                                "deployment_policy": policy.as_dict(),
                                "events": []}
                            app.jobs[job_id]['architecture_decision'] = architecture_decision(
                                app.jobs[job_id]['application_ir'], policy, infrastructure_plan).as_dict()
                            app.jobs[job_id]['compilation'] = compile_decision(
                                app.jobs[job_id]['architecture_decision'],
                                app.jobs[job_id]['application_ir'], policy, infrastructure_plan)
                            if sqlite_conversion is not None:
                                app.jobs[job_id]['sqlite_conversion'] = sqlite_conversion
                            if local_sqlite_binding is not None:
                                app.jobs[job_id]['local_sqlite_binding'] = local_sqlite_binding
                            app.jobs[job_id]['source_digest'] = digest
                            if target == "cloud-run":
                                app.jobs[job_id]["cloud"] = asdict(app.cloud_settings)
                            elif target == "onprem-vm":
                                app.jobs[job_id]["vm"] = asdict(VmSettings.from_environment())
                            elif target == "aws-ecs-express":
                                app.jobs[job_id]["aws"] = asdict(aws_settings_for_job)
                                if postgres_request is not None:
                                    app.jobs[job_id]['postgres'] = {
                                        **asdict(postgres_request),
                                        'subnet_ids': list(postgres_request.subnet_ids)}
                                if latest is not None:
                                    app.jobs[job_id]['prior_result'] = latest['result']
                                    app.jobs[job_id]['replaces_job_id'] = latest['id']
                            app.save(job_id)
                        app.clear_upload_marker(directory)
                    except Exception:
                        if not (directory / 'job.json').is_file():
                            with app.lock:
                                app.jobs.pop(job_id, None)
                            try:
                                shutil.rmtree(directory)
                            except OSError:
                                app.recovery_warnings.append(
                                    '접수 실패 업로드 디렉터리를 정리하지 못했습니다: ' + job_id)
                        raise
                    if create_plan_id is not None:
                        try:
                            operation = app.postgres_operations.start(application_id, create_plan_id)
                            creation_id = operation.get('creation_id')
                            if not isinstance(creation_id, str) or not re.fullmatch(r'[a-f0-9]{16}', creation_id):
                                raise ValueError('DB 생성 시도 ID를 확인하지 못했습니다.')
                            with app.lock:
                                app.jobs[job_id]['postgres_creation_id'] = creation_id
                                app.save(job_id)
                            threading.Thread(target=app.run_postgres_then_agent,
                                             args=(job_id,), daemon=True).start()
                        except Exception as exc:
                            with app.postgres_operations.lock:
                                creation_recorded = application_id in app.postgres_operations.operations
                            status = 'interrupted' if creation_recorded else 'failed'
                            with app.lock:
                                app.jobs[job_id]['status'] = status
                                app.save(job_id)
                            app.event(job_id, 'database_attention' if creation_recorded else 'database_plan_rejected',
                                      ('DB 생성 요청의 결과가 불확실합니다. 생성 상태를 재확인하세요: '
                                       if creation_recorded else
                                       'DB 생성 요청 전 계획 검증에 실패했습니다. 가격 계획을 다시 확인하세요: ')
                                      + redact(str(exc))[:300])
                            self.json_response(202, {"id": job_id, "status": status})
                            return
                        self.json_response(202, {"id": job_id, "status": "provisioning"})
                    else:
                        started = app.start_job_worker(job_id, app.run_agent)
                        self.json_response(202, {"id": job_id, "status": "running" if started else "interrupted"})
                    return
                if self.path.startswith("/api/deployments/") and self.path.endswith("/resume"):
                    job_id = self.path.split('/')[-2]
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        raise ValueError("Environment input must be under 64 KiB")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict) or set(payload) != {"environment"}:
                        raise ValueError("Expected an environment object")
                    with app.lock:
                        job = app.jobs.get(job_id)
                        if not job or job.get('mode') != 'agent' or job['status'] != 'waiting_input':
                            self.json_response(409, {"error": "환경변수 입력을 기다리는 배포가 아닙니다."})
                            return
                        work = app.root / job_id / 'work'
                        expected_digest = job.get('work_digest')
                        try:
                            unchanged = (work.is_dir() and not work.is_symlink()
                                         and (expected_digest is None or source_digest(work) == expected_digest))
                        except (OSError, ValueError):
                            unchanged = False
                        if not unchanged:
                            self.json_response(409, {"error": "입력 대기 이후 작업용 소스가 없거나 변경됐습니다. 새 배포를 시작하세요."})
                            return
                        environment = validate_environment(payload['environment'], job['missing_environment'])
                        promoted = 'promotion_source_job_id' in job
                        if promoted and (job.get('target') != 'aws-ecs-express'
                                         or job.get('attempts') != 0
                                         or not isinstance(job.get('plan'), dict)
                                         or set(environment) != set(job['plan'].get('required_env', []))):
                            environment.clear()
                            self.json_response(409, {'error': 'AWS 이미지 승격 입력 상태가 변경됐습니다. 새 배포를 시작하세요.'})
                            return
                        job.update(status="running", environment_names=sorted(environment), missing_environment=[])
                        app.save(job_id)
                    worker = app.resume_promoted_aws if promoted else app.run_agent
                    started = app.start_job_worker(job_id, worker, environment)
                    self.json_response(202, {"id": job_id, "status": "running" if started else "interrupted"})
                    return
                if re.fullmatch(r"/api/deployments/[a-f0-9]{16}/resume-postgres", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('DB 생성 후 앱 배포 재개에는 본문이 없어야 합니다.')
                    self.json_response(202, app.resume_postgres_deployment(job_id))
                    return
                if re.fullmatch(r"/api/deployments/[a-f0-9]{16}/resume-unstarted", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get('Content-Length', '0')) != 0:
                        raise ValueError('배포 시도 전 작업 재개에는 본문이 없어야 합니다.')
                    self.json_response(202, app.resume_unstarted_deployment(job_id))
                    return
                if re.fullmatch(r"/api/deployments/[a-f0-9]{16}/cancel", self.path):
                    job_id = self.path.split('/')[3]
                    if int(self.headers.get("Content-Length", "0")) != 0:
                        raise ValueError("Cancellation request must be empty")
                    with app.lock:
                        job = app.jobs.get(job_id)
                        if not job or job.get('mode') != 'agent' or job['status'] not in {'waiting_input', 'running'}:
                            self.json_response(409, {"error": "입력 대기 또는 배포 시도 전 작업만 취소할 수 있습니다."})
                            return
                        if job['status'] == 'running' and (job.get('attempts', 0) != 0 or job.get('cancel_requested')):
                            self.json_response(409, {"error": "이미 배포 시도가 시작됐거나 취소 요청이 접수됐습니다. 결과를 확인하세요."})
                            return
                        pending = job['status'] == 'running'
                        if pending:
                            job['cancel_requested'] = True
                        else:
                            job.update(status='cancelled', missing_environment=[])
                        job['events'].append({"time": datetime.now(timezone.utc).isoformat(),
                                              "stage": "cancel_requested" if pending else "cancelled",
                                              "message": "배포 시도 전 취소를 요청했습니다." if pending else "사용자가 입력 대기 작업을 취소했습니다."})
                        app.save(job_id)
                    self.json_response(202 if pending else 200,
                                       {"id": job_id, "status": "cancelling" if pending else "cancelled"})
                    return
                if self.path == "/api/analyze":
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_UPLOAD:
                        raise ValueError("Upload a ZIP smaller than 20 MiB")
                    job_id = uuid.uuid4().hex[:16]
                    directory = app.root / job_id
                    directory.mkdir()
                    try:
                        (directory / '.uncommitted-upload').touch(mode=0o600)
                        archive = directory / "source.zip"
                        archive.write_bytes(self.rfile.read(size))
                        try:
                            project = extract_project(archive, directory / "source")
                        finally:
                            archive.unlink(missing_ok=True)
                        plan = analyze_project(project, self.headers.get("X-Analysis-Mode", "static"), app.ai_settings)
                        diff = dockerfile_diff(project, asdict(plan))
                        with app.lock:
                            app.jobs[job_id] = {"id": job_id, "status": "planned",
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "plan": asdict(plan), "diff": diff, "project": str(project), "events": []}
                            app.save(job_id)
                        app.clear_upload_marker(directory)
                    except Exception:
                        if not (directory / 'job.json').is_file():
                            with app.lock:
                                app.jobs.pop(job_id, None)
                            try:
                                shutil.rmtree(directory)
                            except OSError:
                                app.recovery_warnings.append(
                                    '접수 실패 업로드 디렉터리를 정리하지 못했습니다: ' + job_id)
                        raise
                    self.json_response(201, app.jobs[job_id])
                    return
                if self.path.startswith("/api/deploy/"):
                    job_id = self.path.rsplit("/", 1)[-1]
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= size <= 65536:
                        raise ValueError("Deployment input exceeds 64 KiB")
                    try:
                        payload = json.loads(self.rfile.read(size)) if size else {}
                    except (ValueError, UnicodeError):
                        raise ValueError("Deployment input must be valid JSON") from None
                    if not isinstance(payload, dict) or set(payload) - {"environment"}:
                        raise ValueError("Expected an environment object")
                    with app.lock:
                        job = app.jobs.get(job_id)
                        if not job or job["status"] != "planned":
                            self.json_response(409, {"error": "A planned job is required"})
                            return
                        environment = validate_environment(payload.get("environment"), job["plan"]["required_env"])
                        if source_digest(Path(job["project"])) != job["plan"]["source_digest"]:
                            self.json_response(409, {"error": "Source changed after analysis; analyze again"})
                            return
                        job["status"] = "running"
                        job["environment_names"] = sorted(environment)
                        app.save(job_id)
                    started = app.start_job_worker(job_id, app.run, environment)
                    self.json_response(202, {"id": job_id, "status": "running" if started else "interrupted"})
                    return
                self.json_response(404, {"error": "Not found"})
            except Exception as exc:
                self.json_response(400, {"error": str(exc)})

    return Handler


def serve(product_name: str = "Sky", default_state_dir: str = ".sky"):
    parser = argparse.ArgumentParser(description=f"{product_name} local development server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address; use 0.0.0.0 behind the service load balancer")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state-dir", type=Path, default=Path(default_state_dir))
    parser.add_argument("--monitor-interval", type=int, default=300,
                        help="Seconds between health checks (60–3600; 0 disables monitoring)")
    parser.add_argument("--github-poll-interval", type=int, default=60,
                        help="Seconds between public GitHub branch checks (60–3600; 0 disables checks)")
    args = parser.parse_args()
    if args.monitor_interval != 0 and not 60 <= args.monitor_interval <= 3600:
        parser.error('--monitor-interval must be 0 or 60–3600 seconds')
    if args.github_poll_interval != 0 and not 60 <= args.github_poll_interval <= 3600:
        parser.error('--github-poll-interval must be 0 or 60–3600 seconds')
    with StateDirectoryLock(args.state_dir) as state_dir:
        app = App(state_dir, monitor_interval=args.monitor_interval,
                  github_poll_interval=args.github_poll_interval)
        server = ThreadingHTTPServer((args.host, args.port), handler_for(app))
        stop_monitor = threading.Event()
        if app.monitor_interval:
            threading.Thread(target=app.monitor_loop, args=(stop_monitor,), daemon=True).start()
        if app.github_poll_interval:
            threading.Thread(target=app.github_poll_loop,
                             args=(stop_monitor, app.github_poll_interval), daemon=True).start()
        print(f"{product_name}: http://{args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop_monitor.set()
            server.server_close()


def main():
    serve()


if __name__ == "__main__":
    main()
