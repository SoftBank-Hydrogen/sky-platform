"""ZIP jobs for native AWS profiles, separate from the container-only AI contract."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.native import AwsEc2Adapter, AwsLambdaAdapter, lambda_bundle
from adapters.aws.native_image import NativeImagePublisher
from application.deployment_core import DeploymentPlan, source_digest
from application.deployment_core import analyze as analyze_project_rules
from application.infrastructure import inspect_infrastructure

NATIVE_TARGETS = {"aws-lambda", "aws-ec2"}


def native_profile(project: Path, target: str, handler: str = "handler.handler") -> dict:
    if target not in NATIVE_TARGETS:
        raise ValueError("Unknown native AWS target")
    profile = inspect_infrastructure(project)
    signals = dict(profile.source_signals)
    unsupported = set(profile.requirements)
    forbidden = {"host-kernel-control", "host-device-access", "websocket", "persistent-http-server"}
    if target == "aws-ec2":
        forbidden.remove("persistent-http-server")
    if unsupported or forbidden.intersection(signals):
        raise ValueError(
            "이 실행 경로는 DB·영속 파일·워커·호스트 제어·장기 연결을 지원하지 않습니다: "
            + ", ".join(sorted(unsupported | forbidden.intersection(signals)))
        )
    result = {
        "target": target,
        "source_digest": source_digest(project),
        "infrastructure_profile": profile.as_dict(),
    }
    if target == "aws-lambda":
        # Bundle assessment never imports/executes the supplied Python source.
        artifact = lambda_bundle(project, handler)
        result.update(handler=handler, artifact_sha256=hashlib.sha256(artifact).hexdigest())
    else:
        plan = replace(analyze_project_rules(project), target="aws-ec2")
        if plan.required_env:
            raise ValueError("현재 EC2 경로는 사용자 환경값·비밀값 연결을 지원하지 않습니다.")
        result["plan"] = asdict(plan)
    return json.loads(json.dumps(result))


class NativeDeploymentsMixin:
    def native_unavailable_reason(self, target):
        try:
            self.native_adapter({"target": target, "aws": asdict(self.aws_settings)})
            if target == "aws-ec2" and (
                not os.environ.get("SKY_EC2_SUBNET_ID") or not shutil.which("docker")
            ):
                return "EC2에는 SKY_EC2_SUBNET_ID와 Docker 빌더가 필요합니다."
            import boto3  # noqa: F401
        except (ValueError, ImportError, AwsConfigurationError):
            return "AWS 계정·리전, 필수 IAM 권한 경계 및 boto3 설정이 필요합니다."
        return None

    def native_adapter(self, job, checkpoint=None):
        adapter = AwsLambdaAdapter if job["target"] == "aws-lambda" else AwsEc2Adapter
        return adapter(AwsSettings(**job["aws"]), checkpoint=checkpoint)

    def create_native_job(
        self, job_id, project, application_id, target, *, handler="handler.handler", owner=None
    ):
        reason = self.native_unavailable_reason(target)
        if reason:
            raise ValueError(reason)
        assessment = native_profile(project, target, handler)
        assessment["boundary"] = os.environ.get("SKY_AWS_ROLE_BOUNDARY_ARN", "")
        if target == "aws-ec2":
            assessment["subnet_id"] = os.environ["SKY_EC2_SUBNET_ID"]
        with self.lock:
            self.ensure_application_available(application_id, target)
            if any(
                j.get("application_id") == application_id
                and j.get("target") == target
                and j.get("deployment_state") != "deleted"
                for j in self.jobs.values()
            ):
                raise ValueError("기존 네이티브 릴리스를 종료한 뒤 새 배포를 시작하세요.")
            self.jobs[job_id] = {
                "id": job_id,
                "application_id": application_id,
                "attempt_id": job_id + "-a1",
                "mode": "native_aws",
                "target": target,
                "requested_target": target,
                "public": True,
                "status": "running",
                "deployment_state": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "project": str(project),
                "source_digest": assessment["source_digest"],
                "native_plan": assessment,
                "plan": None,
                "attempts": 1,
                "steps": 0,
                "events": [],
                "changes": [],
                "diff": "",
                "aws": asdict(self.aws_settings),
                **(owner.record() if owner else {}),
            }
            self.save(job_id)

    def run_native(self, job_id):
        with self.lock:
            snapshot = json.loads(json.dumps(self.jobs[job_id]))

        def checkpoint(receipt):
            with self.lock:
                self.jobs[job_id]["native_receipt"] = receipt
                self.save(job_id)

        def image_checkpoint(receipt):
            with self.lock:
                self.jobs[job_id]["native_image"] = receipt
                self.save(job_id)

        try:
            project = Path(snapshot["project"])
            assessment = snapshot["native_plan"]
            if (
                source_digest(project) != snapshot["source_digest"]
                or os.environ.get("SKY_AWS_ROLE_BOUNDARY_ARN", "") != assessment["boundary"]
            ):
                raise ValueError("소스 또는 IAM 권한 경계가 접수 이후 변경됐습니다.")
            checked = native_profile(
                project, snapshot["target"], assessment.get("handler", "handler.handler")
            )
            if checked != {k: v for k, v in assessment.items() if k not in {"boundary", "subnet_id"}}:
                raise ValueError("접수한 배포 판단과 현재 소스가 다릅니다.")
            adapter = self.native_adapter(snapshot, checkpoint)
            self.event(job_id, "provision", "소스에 묶인 네이티브 AWS 릴리스를 배포합니다.")
            if snapshot["target"] == "aws-lambda":
                result = adapter.deploy(
                    project,
                    snapshot["application_id"],
                    snapshot["attempt_id"],
                    handler=assessment["handler"],
                    public_access=True,
                )
            else:
                adapter.check_runtime_permissions()
                publisher = NativeImagePublisher(
                    adapter, image_checkpoint, lambda stage, message: self.event(job_id, stage, message)
                )
                artifact = publisher.publish(
                    project,
                    DeploymentPlan(**assessment["plan"]),
                    snapshot["application_id"],
                    snapshot["attempt_id"],
                )
                result = adapter.deploy(
                    artifact["image"],
                    snapshot["application_id"],
                    snapshot["attempt_id"],
                    subnet_id=assessment["subnet_id"],
                    port=assessment["plan"]["port"],
                    public_access=True,
                    stateless=True,
                    health_path=assessment["plan"]["health_path"],
                )
            expected_digest = (
                base64.b64encode(bytes.fromhex(assessment["artifact_sha256"])).decode()
                if snapshot["target"] == "aws-lambda"
                else artifact["image"]
            )
            expected_host = (
                r"https://[a-z0-9]+\.lambda-url\." + re.escape(snapshot["aws"]["region"]) + r"\.on\.aws/"
                if snapshot["target"] == "aws-lambda"
                else r"https://[a-z0-9]+\.cloudfront\.net/"
            )
            if (
                result.get("target") != snapshot["target"]
                or result.get("application_id") != snapshot["application_id"]
                or result.get("attempt_id") != snapshot["attempt_id"]
                or result.get("account") != snapshot["aws"]["expected_account"]
                or result.get("region") != snapshot["aws"]["region"]
                or result.get("artifact_digest") != expected_digest
                or not isinstance(result.get("url"), str)
                or not re.fullmatch(expected_host, result["url"])
            ):
                raise ValueError("실행 결과의 대상·소스 산출물·계정 또는 URL이 접수와 다릅니다.")
            if (
                result.get("status") != "verified"
                or result.get("artifact_verified") is not True
                or result.get("http_verified") is not True
            ):
                raise ValueError("실행 산출물과 실제 HTTP 응답이 모두 검증되지 않았습니다.")
            with self.lock:
                self.jobs[job_id].update(status="succeeded", result=result, deployment_state="active")
                self.save(job_id)
            self.event(job_id, "verified", "실행 산출물과 공개 HTTPS 응답을 확인했습니다.")
        except Exception as exc:  # noqa: BLE001 - persist uncertain worker results without leaking SDK errors
            with self.lock:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["deployment_state"] = (
                    "needs_attention" if job.get("native_receipt") or job.get("native_image") else "deleted"
                )
                self.save(job_id)
            # SDK/build stderr can include secrets; show type, not its unrestricted message.
            self.event(
                job_id, "error", "네이티브 배포 실패 (" + type(exc).__name__ + "). 생성 기록을 확인하세요."
            )

    def retire_native(self, job_id):
        with self.lock:
            snapshot = json.loads(json.dumps(self.jobs[job_id]))
        try:
            adapter = self.native_adapter(snapshot)
            if snapshot.get("native_receipt") and snapshot["native_receipt"].get("status") != "deleted":
                result = adapter.destroy(snapshot["native_receipt"])
                with self.lock:
                    self.jobs[job_id]["native_receipt"] = result
                    self.save(job_id)
            if snapshot.get("native_image"):

                def checkpoint(receipt):
                    with self.lock:
                        self.jobs[job_id]["native_image"] = receipt
                        self.save(job_id)

                NativeImagePublisher(adapter, checkpoint, lambda *_args: None).destroy(
                    snapshot["native_image"]
                )
            with self.lock:
                self.jobs[job_id].update(deployment_state="deleted", retired_at=datetime.now(UTC).isoformat())
                self.save(job_id)
            self.event(job_id, "retired", "작업 소유 스택·이미지 저장소 삭제를 확인했습니다.")
        except Exception as exc:  # noqa: BLE001 - persist uncertain worker results without leaking SDK errors
            with self.lock:
                self.jobs[job_id].update(deployment_state="delete_failed", retire_error=type(exc).__name__)
                self.save(job_id)
