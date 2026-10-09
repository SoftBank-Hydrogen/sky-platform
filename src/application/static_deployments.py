"""A separate job lifecycle for immutable, public static-site releases."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from adapters.aws.ecs import AwsSettings
from adapters.aws.static_site import AwsStaticSiteAdapter
from application.analysis import redact
from application.deployment_core import source_digest
from application.static_compilation import static_compilation, verify_static_compilation


class StaticDeploymentsMixin:
    def create_static_job(
        self, job_id: str, project: Path, application_id: str, *, requested_target: str
    ) -> None:
        """Persist one static release before starting its cloud worker."""
        attempt_id = job_id + "-a1"
        preflight = AwsStaticSiteAdapter(self.aws_settings).preflight(project, application_id, attempt_id)
        records = static_compilation(
            project, preflight["source_digest"], requested_target=requested_target
        )
        with self.lock:
            self.ensure_application_available(application_id, "aws-s3-cloudfront")
            if any(
                old.get("application_id") == application_id
                and old.get("target") == "aws-s3-cloudfront"
                and old.get("deployment_state", "active") != "deleted"
                for old in self.jobs.values()
            ):
                raise ValueError("기존 정적 사이트 릴리스를 종료한 뒤 새 배포를 시작하세요.")
            self.jobs[job_id] = {
                "id": job_id, "mode": "static_site", "target": "aws-s3-cloudfront",
                "requested_target": requested_target,
                "application_id": application_id, "attempt_id": attempt_id,
                "status": "running", "deployment_state": "active", "public": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "project": str(project), "source_digest": preflight["source_digest"],
                "static_preflight": preflight, "plan": None, "attempts": 1,
                "steps": 0, "changes": [], "diff": "", "events": [],
                "aws": asdict(self.aws_settings),
                **records,
            }
            self.save(job_id)

    def static_adapter(self, job: dict, checkpoint=None) -> AwsStaticSiteAdapter:
        return AwsStaticSiteAdapter(
            AwsSettings(**job["aws"]), checkpoint=checkpoint,
        )

    def run_static_site(self, job_id: str) -> None:
        with self.lock:
            snapshot = json.loads(json.dumps(self.jobs[job_id]))

        def checkpoint(**updates):
            with self.lock:
                current = self.jobs[job_id]
                current.update(updates)
                self.save(job_id)

        try:
            if source_digest(Path(snapshot["project"])) != snapshot["source_digest"]:
                raise ValueError("정적 사이트 소스가 작업 기록과 다릅니다.")
            verify_static_compilation(snapshot, Path(snapshot["project"]), snapshot["source_digest"])
            self.event(job_id, "provision", "전용 S3·CloudFront 스택을 생성합니다.")
            result = self.static_adapter(snapshot, checkpoint).deploy(
                Path(snapshot["project"]), snapshot["application_id"], snapshot["attempt_id"]
            )
            with self.lock:
                current = self.jobs[job_id]
                current.update(status="succeeded", result=result, deployment_state="active")
                self.save(job_id)
            self.event(job_id, "verified", "공개 HTTPS 주소에서 index.html 원본 해시를 확인했습니다.")
        except Exception as exc:
            with self.lock:
                current = self.jobs[job_id]
                current["status"] = "failed"
                if current.get("static_stack_name"):
                    current["deployment_state"] = "needs_attention"
                self.save(job_id)
            self.event(job_id, "error", redact(str(exc))[:300])

    def reconcile_static_site(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if (not job or job.get("mode") != "static_site"
                    or job.get("status") not in {"failed", "interrupted"}
                    or not job.get("static_stack_name")):
                raise ValueError("재확인할 정적 사이트 생성 기록이 없습니다.")
            snapshot = json.loads(json.dumps(job))
        result = self.static_adapter(snapshot).reconcile(
            snapshot["application_id"], snapshot["attempt_id"])
        with self.lock:
            current = self.jobs[job_id]
            current["static_stack_id"] = result["stack_id"]
            current["deployment_state"] = "needs_attention"
            self.save(job_id)
        self.event(job_id, "reconciled", "생성 요청의 스택 소유권과 현재 상태를 확인했습니다.")
        return {"reconciled": True, **result}

    def retire_static_site(self, job_id: str) -> None:
        with self.lock:
            snapshot = json.loads(json.dumps(self.jobs[job_id]))
        try:
            self.static_adapter(snapshot).retire(
                snapshot["application_id"], snapshot["attempt_id"], snapshot["static_stack_id"]
            )
        except Exception as exc:
            with self.lock:
                current = self.jobs[job_id]
                current["deployment_state"] = "delete_failed"
                current["retire_error"] = redact(str(exc))[:300]
                self.save(job_id)
            self.event(job_id, "retire_failed", redact(str(exc))[:300])
        else:
            with self.lock:
                current = self.jobs[job_id]
                current["deployment_state"] = "deleted"
                current["retired_at"] = datetime.now(timezone.utc).isoformat()
                current.pop("retire_error", None)
                self.save(job_id)
            self.event(job_id, "retired", "전용 S3 객체와 CloudFront 스택 삭제를 확인했습니다.")
