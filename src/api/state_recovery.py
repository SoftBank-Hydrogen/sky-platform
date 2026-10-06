"""Persistence and restart recovery for deployment jobs."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from deployment.core import DeploymentPlan
from database.postgres import PostgresRequest


def postgres_request_from_job(job: dict) -> PostgresRequest | None:
    configuration = job.get('postgres')
    if configuration is None:
        return None
    if not isinstance(configuration, dict) or set(configuration) != {
            'application_id', 'account', 'region', 'vpc_id', 'subnet_ids',
            'service_security_group'} or not isinstance(configuration['subnet_ids'], list):
        raise ValueError('저장된 PostgreSQL 연결 요청이 올바르지 않습니다.')
    request = PostgresRequest(configuration['application_id'], configuration['account'],
                              configuration['region'], configuration['vpc_id'],
                              tuple(configuration['subnet_ids']),
                              configuration['service_security_group'])
    request.validate()
    aws = job.get('aws') or {}
    if (job.get('target') != 'aws-ecs-express'
            or job.get('application_id') != request.application_id
            or aws.get('region') != request.region
            or aws.get('expected_account') != request.account
            or aws.get('service_security_group') != request.service_security_group):
        raise ValueError('저장된 PostgreSQL 연결 요청이 AWS 배포 대상과 다릅니다.')
    return request


class StateRecoveryMixin:
    """Load and save jobs while preserving interrupted-deployment state."""

    def clean_uncommitted_uploads(self):
        for directory in sorted(self.root.iterdir()):
            if not re.fullmatch(r'[a-f0-9]{16}', directory.name):
                continue
            marker = directory / '.uncommitted-upload'
            if not marker.exists() and not marker.is_symlink():
                continue
            job_file = directory / 'job.json'
            if job_file.exists() or job_file.is_symlink():
                continue
            try:
                if (directory.is_symlink() or not directory.is_dir()
                        or marker.is_symlink() or not marker.is_file()):
                    raise ValueError('Unsafe upload marker')
                for entry in directory.iterdir():
                    if entry.is_symlink():
                        raise ValueError('Symlink in uncommitted upload')
                    if entry.name == '.uncommitted-upload' and entry.is_file():
                        continue
                    if entry.name == 'source.zip' and entry.is_file():
                        continue
                    if entry.name == 'source' and entry.is_dir():
                        continue
                    if re.fullmatch(r'\.job-[A-Za-z0-9_]+\.tmp', entry.name) and entry.is_file():
                        continue
                    raise ValueError('Unexpected file in uncommitted upload')
                shutil.rmtree(directory)
            except (OSError, ValueError):
                self.recovery_warnings.append(
                    '미접수 업로드 디렉터리를 안전하게 정리하지 못했습니다: ' + directory.name)

    def clear_upload_marker(self, directory: Path):
        try:
            (directory / '.uncommitted-upload').unlink()
        except OSError:
            self.recovery_warnings.append(
                '접수된 작업의 업로드 표시를 정리하지 못했습니다: ' + directory.name)

    def restore(self):
        self.clean_uncommitted_uploads()
        for path in sorted(self.root.glob("*/job.json")):
            try:
                job = json.loads(path.read_text())
                if not isinstance(job, dict):
                    raise ValueError("Invalid job record")
                job_id = path.parent.name
                if not re.fullmatch(r"[a-f0-9]{16}", job_id) or job.get("id") != job_id:
                    raise ValueError("Invalid job identity")
                if ("application_id" in job and not re.fullmatch(
                        r"[a-z][a-z0-9-]{2,30}", job["application_id"])):
                    raise ValueError("Invalid application identity")
                project = Path(job["project"]).resolve()
                if not project.is_relative_to((path.parent / "source").resolve()):
                    raise ValueError("Invalid project path")
                if job.get("plan") is not None:
                    DeploymentPlan(**job["plan"])
                elif job.get("mode") != "agent":
                    raise ValueError("Missing deployment plan")
                postgres_request_from_job(job)
                if job.get('postgres_creation_id') is not None and (
                        not isinstance(job['postgres_creation_id'], str)
                        or not re.fullmatch(r'[a-f0-9]{16}', job['postgres_creation_id'])
                        or job.get('target') != 'aws-ecs-express'
                        or job.get('postgres') is None):
                    raise ValueError('Invalid PostgreSQL creation binding')
                if job["status"] not in {"planned", "provisioning", "running", "waiting_input", "succeeded", "failed", "interrupted", "cancelled"}:
                    raise ValueError("Invalid job status")
                if (not isinstance(job["events"], list) or any(
                        not isinstance(event, dict) or any(not isinstance(event.get(key), str)
                        for key in ("time", "stage", "message")) for event in job["events"])):
                    raise ValueError("Invalid events")
                job.setdefault("created_at", datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
                if not isinstance(job["created_at"], str):
                    raise ValueError("Invalid timestamp")
                self.jobs[job_id] = job
                health_file = path.parent / 'health.json'
                if health_file.is_file():
                    try:
                        history = json.loads(health_file.read_text())
                        if (not isinstance(history, list) or len(history) > 20
                                or any(not isinstance(item, dict)
                                       or type(item.get('healthy')) is not bool
                                       or not isinstance(item.get('checked_at'), str)
                                       or not isinstance(item.get('reason'), str)
                                       or item.get('source') not in {'automatic', 'manual'}
                                       for item in history)):
                            raise ValueError('Invalid health history')
                        self.health_history[job_id] = history
                    except (OSError, ValueError, TypeError):
                        self.recovery_warnings.append(f"상태 확인 기록을 불러오지 못했습니다: {job_id}")
                if job["status"] == "running":
                    job["status"] = "cancelled" if job.get('cancel_requested') and not job.get('attempts') else "interrupted"
                    if job.get('aws_update_submitted') and not job.get('aws_update_failed_at'):
                        job['aws_update_failed_at'] = datetime.now(timezone.utc).isoformat()
                    job["events"].append({"time": datetime.now(timezone.utc).isoformat(),
                        "stage": job['status'], "message": (
                            "서버 재시작 후 배포 시도 전 취소 요청을 확인했습니다."
                            if job['status'] == 'cancelled' else
                            "서버 재시작으로 완료 여부를 확인하지 못했습니다. 컨테이너 상태를 확인하세요. 자동 재배포는 하지 않습니다.")})
                    job.pop('cancel_requested', None)
                    self.save(job_id)
                if job['status'] == 'provisioning':
                    job['status'] = 'interrupted'
                    job['events'].append({'time': datetime.now(timezone.utc).isoformat(),
                        'stage': 'interrupted', 'message': '서버 재시작으로 DB 생성과 배포 연결을 중단했습니다. DB 생성 상태를 재확인하세요. 자동 배포하지 않습니다.'})
                    self.save(job_id)
                if job.get('deployment_state') == 'deleting':
                    job['deployment_state'] = 'delete_failed'
                    job['events'].append({"time": datetime.now(timezone.utc).isoformat(),
                        "stage": "retire_interrupted", "message": "서버 재시작으로 종료 확인이 중단됐습니다. 배포 종료를 다시 실행할 수 있습니다."})
                    self.save(job_id)
                if job.get('aws_image_cleanup_state') == 'running':
                    job['aws_image_cleanup_state'] = 'failed'
                    self.save(job_id)
                if job.get('aws_migration_cleanup_state') == 'running':
                    job['aws_migration_cleanup_state'] = 'failed'
                    self.save(job_id)
                if job.get('release_rollback_state') == 'running':
                    job['release_rollback_state'] = ('needs_attention' if job.get('release_rollback_submitted') else 'failed')
                    if job.get('release_rollback_submitted'):
                        job['deployment_state'] = 'needs_attention'
                        job.setdefault('release_rollback_failed_at', datetime.now(timezone.utc).isoformat())
                    self.save(job_id)
            except (OSError, ValueError, KeyError, TypeError):
                self.recovery_warnings.append(f"작업 기록을 불러오지 못했습니다: {path.parent.name}")
        for job in self.jobs.values():
            rollback_target = self.jobs.get(job.get('release_rollback_target_id') or job.get('replaces_job_id'))
            legacy_rollback = 'release_rollback_restore_pending' not in job
            if (rollback_target and job.get('release_rollback_state') == 'succeeded'
                    and job.get('deployment_state') == 'superseded'
                    and (job.get('release_rollback_restore_pending') or legacy_rollback)
                    and rollback_target.get('deployment_state') == 'superseded'
                    and not any(successor is not job
                                and successor.get('replaces_job_id') == rollback_target['id']
                                and successor.get('status') == 'succeeded'
                                and successor.get('created_at', '') > job.get('created_at', '')
                                for successor in self.jobs.values())):
                rollback_target['deployment_state'] = 'active'
                rollback_target['result']['images'] = list(dict.fromkeys(
                    [*(rollback_target['result'].get('images') or [rollback_target['result']['image']]),
                     *(job.get('result', {}).get('images') or [])]))
                self.save(rollback_target['id'])
            if job.get('release_rollback_restore_pending') and rollback_target and \
                    rollback_target.get('deployment_state') == 'active':
                job['release_rollback_restore_pending'] = False
                self.save(job['id'])
            previous = self.jobs.get(job.get('replaces_job_id'))
            if not previous or previous.get('deployment_state', 'active') != 'active':
                continue
            if (job.get('status') == 'succeeded' and job.get('result')
                    and job.get('deployment_state', 'active') == 'active'
                    and not job.get('release_rollback_state')):
                previous['deployment_state'] = 'superseded'
                self.save(previous['id'])
            elif ((job.get('aws_update_submitted') or any(event.get('stage') == 'update_submitting'
                      for event in job.get('events', []))) and not job.get('aws_reconciled')
                  and job.get('status') in {'failed', 'interrupted'}):
                previous['deployment_state'] = 'needs_attention'
                self.save(previous['id'])

    def save(self, job_id):
        path = self.root / job_id / "job.json"
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.job-', suffix='.tmp', dir=path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(self.jobs[job_id], output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            job = self.jobs[job_id]
            job['status'] = 'failed'
            job.pop('result', None)
            if not job.get('persistence_failed'):
                job['persistence_failed'] = True
                job.setdefault('events', []).append({
                    'time': datetime.now(timezone.utc).isoformat(), 'stage': 'error',
                    'message': '작업 기록을 저장하지 못했습니다. 배포 리소스 상태를 직접 확인하세요.'})
            raise RuntimeError('배포 작업 기록 저장에 실패했습니다.') from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
