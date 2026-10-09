"""Compile an explicitly selected static bundle with the shared decision contract."""

from __future__ import annotations

import json
from pathlib import Path

from application.infrastructure import inspect_infrastructure
from engine.application_ir import ApplicationIR, Component, Requirement
from engine.architecture_decision import architecture_decision, verify_architecture_decision
from engine.candidates import static_hosting_candidate
from engine.compatibility import evidence_id
from engine.compilation import compile_decision, verify_compilation
from engine.deployment_policy import deployment_policy, policy_from_record
from engine.evidence_record import source_evidence
from engine.static_site import assess_static_site


def static_compilation(project: Path, source_revision: str, *, requested_target: str = "aws-s3-cloudfront") -> dict:
    """Recompute the source-bound choice without calling AWS or changing files."""
    if requested_target not in {"auto", "aws-s3-cloudfront"}:
        raise ValueError("Unsupported static selection request")
    profile = inspect_infrastructure(project)
    assessment = assess_static_site(project, profile)
    if assessment.status != "eligible":
        raise ValueError("Static compilation requires an eligible source bundle")
    identifier = evidence_id("static-assets", "index.html")
    ir = ApplicationIR(
        schema_version=2,
        source_revision=source_revision,
        components=(Component("source-bundle", "static_site", ("R-static-assets",)),),
        requirements=(Requirement("R-static-assets", "static-assets", (identifier,)),),
        evidence=(source_evidence(identifier, "index.html", "static-assets", source_revision),),
        database_engines=(),
        declared_image_platform=None,
        topology_status="resolved_static",
        unknowns=("browser_behavior",),
    ).as_dict()
    policy = deployment_policy(requested_target, True)
    candidate = static_hosting_candidate(assessment, configured=True, public_access=True)
    candidate["selected"] = True
    plan = {
        "target": "aws-s3-cloudfront",
        "planner": "static-source-rule" if requested_target == "auto" else "user",
        "rationale": "검사된 정적 파일을 공개 HTTPS 주소로 배포합니다.",
        "resources": ["private S3 bucket", "CloudFront distribution"],
        "compatibility": {
            "target": "aws-s3-cloudfront",
            "compatible": True,
            "access_mode": "public",
            "constraint_results": [{
                "rule_id": "STATIC-SOURCE-01",
                "status": "satisfied",
                "reason": "루트 index.html과 정적 파일만 확인했습니다.",
                "evidence_ids": [identifier],
            }],
        },
        "candidates": [candidate],
    }
    decision = architecture_decision(ir, policy, plan).as_dict()
    compilation = compile_decision(decision, ir, policy, plan)
    return {
        "application_ir": ir,
        "deployment_policy": policy.as_dict(),
        "infrastructure_plan": plan,
        "architecture_decision": decision,
        "compilation": compilation,
    }


def verify_static_compilation(job: dict, project: Path, source_revision: str) -> bool:
    """Check every new record before provisioning; accept unchanged historical jobs."""
    fields = (
        "application_ir", "deployment_policy", "infrastructure_plan",
        "architecture_decision", "compilation",
    )
    new_record_fields = (
        "application_ir", "deployment_policy", "architecture_decision", "compilation",
    )
    present = [field for field in new_record_fields if field in job]
    if not present:
        return False
    if len(present) != len(new_record_fields) or "infrastructure_plan" not in job:
        raise ValueError("Incomplete static architecture record")
    expected = static_compilation(
        project, source_revision, requested_target=job.get("requested_target", "aws-s3-cloudfront")
    )
    if any(json.dumps(job[field], sort_keys=True) != json.dumps(expected[field], sort_keys=True)
           for field in fields):
        raise ValueError("Stored static architecture disagrees with its source")
    policy = policy_from_record(job["deployment_policy"])
    verify_architecture_decision(
        job["architecture_decision"], job["application_ir"], policy, job["infrastructure_plan"]
    )
    verify_compilation(
        job["compilation"], job["architecture_decision"], job["application_ir"],
        policy, job["infrastructure_plan"],
    )
    return True
