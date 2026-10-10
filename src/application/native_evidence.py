"""Read-only certificate projection for explicit native ZIP profiles."""

import base64
import re
from datetime import UTC, datetime


def native_certificate(job):
    plan = job.get("native_plan") or {}
    result = job.get("result") or {}
    receipt = job.get("native_receipt") or {}
    artifact = job.get("native_image") or {}
    bound = (
        bool(re.fullmatch(r"[a-f0-9]{64}", str(job.get("source_digest", ""))))
        and plan.get("source_digest") == job.get("source_digest")
        and plan.get("target") == job.get("target")
        and result.get("target") == job.get("target")
        and result.get("attempt_id") == job.get("attempt_id")
        and result.get("stack_id") == receipt.get("stack_id")
    )
    expected = artifact.get("image")
    if job.get("target") == "aws-lambda":
        try:
            expected = base64.b64encode(bytes.fromhex(plan["artifact_sha256"])).decode()
        except (ValueError, KeyError, TypeError):
            expected = None
    else:
        bound = bound and artifact.get("source_digest") == job.get("source_digest")
    complete = (
        bound
        and job.get("status") == "succeeded"
        and result.get("status") == "verified"
        and expected is not None
        and result.get("artifact_digest") == expected
        and receipt.get("artifact_digest") == expected
    )
    checks = [
        {"name": "source_binding", "status": "passed" if bound else "unverified"},
        {
            "name": "running_artifact",
            "status": "passed" if complete and result.get("artifact_verified") is True else "unverified",
        },
        {
            "name": "http_endpoint",
            "status": "passed" if complete and result.get("http_verified") is True else "unverified",
        },
        {"name": "rollback", "status": "unverified"},
        {"name": "continuous_availability", "status": "unverified"},
    ]
    return {
        "schema_version": 1,
        "kind": "sky-record-snapshot",
        "generated_at": datetime.now(UTC).isoformat(),
        "job": {
            key: job.get(key) for key in ("id", "application_id", "target", "status", "deployment_state")
        },
        "source": {"uploaded_sha256": job.get("source_digest"), "changed_paths": [], "change_count": 0},
        "destination": {
            "account": result.get("account"),
            "region": result.get("region"),
            "access_mode": "public",
            "url": result.get("url") if complete else None,
            "stack_id": receipt.get("stack_id"),
        },
        "artifact": {
            "native_digest": expected if complete else None,
            "local_image_id": artifact.get("rehearsal", {}).get("image_id"),
        },
        "decision_trace": {
            "status": "explicit_native_profile",
            "target": job.get("target"),
            "evidence": plan.get("infrastructure_profile") or {},
        },
        "compilation_status": "unrecorded",
        "verification": checks,
        "unverified": [check["name"] for check in checks if check["status"] == "unverified"],
        "cost": {"estimated_total": None, "actual_total": None},
        "limitations": [
            "명시적 네이티브 실행 기록입니다. 기존 V4 자동 선택·컴파일 계약의 완료 증거는 아닙니다.",
            "배포 당시 검증 기록이며 현재 가용성·서명·전체 앱 기능을 보증하지 않습니다.",
        ],
    }
