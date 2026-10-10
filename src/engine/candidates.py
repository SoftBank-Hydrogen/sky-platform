"""Evaluate only implemented deployment targets; never invent a recommendation."""

from __future__ import annotations

from engine.backend_identity import backend_identity
from engine.compatibility import InfrastructureProfile, infrastructure_compatibility
from engine.cost_exposure import cost_exposure
from engine.static_site import StaticSiteAssessment

SUPPORTED_TARGETS = ("local-docker", "aws-ecs-express", "cloud-run")


def compare_targets(
    profile: InfrastructureProfile,
    availability: dict[str, str | None],
    *,
    public_access: bool,
    local_sqlite: bool = False,
    include_compose: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Evaluate target variants; Compose is explicit-only until auto selection is verified."""
    reports = []
    targets = (
        (*SUPPORTED_TARGETS, "onprem-compose", "onprem-vm")
        if include_compose and "onprem-vm" in availability
        else ((*SUPPORTED_TARGETS, "onprem-compose") if include_compose else SUPPORTED_TARGETS)
    )
    for target in targets:
        report = infrastructure_compatibility(
            profile,
            target,
            public_access=public_access,
            local_sqlite=local_sqlite and target in {"local-docker", "onprem-compose"},
        )
        reason = availability[target]
        reports.append(
            {
                **report,
                "configured": reason is None,
                "configuration_reason": reason,
                "preview_eligible": report["compatible"] and not report["unknowns"] and reason is None,
                "cost": cost_exposure(target, database_required=bool(profile.database_engines)),
            }
        )
    return reports, evaluate_candidates(reports)


def evaluate_candidates(reports: list[dict]) -> list[dict]:
    """Separate constraint failures from missing credentials and unmeasured trade-offs."""
    candidates = []
    for report in reports:
        violations = [result for result in report["constraint_results"] if result["status"] == "violated"]
        unknown_rules = [result for result in report["constraint_results"] if result["status"] == "unknown"]
        database_binding_only = (
            report["target"] == "aws-ecs-express"
            and report["database_engines"] == ["postgresql"]
            and [item["rule_id"] for item in violations] == ["DATA-BINDING-01"]
        )
        if database_binding_only:
            status = "requires_database_binding"
            reasons = [
                "PostgreSQL DB 생성 또는 기존 DB 연결을 선택·검증해야 배포할 수 있습니다.",
                *report["unknowns"],
            ]
            if not report["configured"]:
                reasons.append(report["configuration_reason"])
        elif violations:
            status = "rejected"
            reasons = [result["reason"] for result in violations]
        elif report["unknowns"]:
            status = "needs_review"
            reasons = [*report["unknowns"]]
            if not report["configured"]:
                reasons.append(report["configuration_reason"])
        elif not report["configured"]:
            status = "requires_setup"
            reasons = [report["configuration_reason"]]
        else:
            status = "eligible"
            reasons = [
                "감지된 요구와 대상 기능이 충돌하지 않습니다. 실제 배포 성공은 아직 검증되지 않았습니다."
            ]
        candidates.append(
            {
                "id": report["target"],
                "provider": backend_identity(report["target"]).provider,
                "backend": backend_identity(report["target"]).backend,
                "sky_adapter_support": "implemented",
                "selection_mode": backend_identity(report["target"]).selection_mode,
                "structural_status": (
                    "incompatible" if violations else "unknown" if report["unknowns"] else "compatible"
                ),
                "execution_status": "implemented_unverified",
                "status": status,
                "reasons": reasons,
                "violated_rule_ids": [result["rule_id"] for result in violations],
                "unknown_rule_ids": [result["rule_id"] for result in unknown_rules],
                "reason_codes": sorted(
                    {result["rule_id"] for result in (*violations, *unknown_rules)}
                    | ({"SETUP_REQUIRED"} if status == "requires_setup" else set())
                    | ({"DATABASE_BINDING_REQUIRED"} if status == "requires_database_binding" else set())
                ),
                "evidence_ids": sorted(
                    {
                        identifier
                        for result in (*violations, *unknown_rules)
                        for identifier in result["evidence_ids"]
                    }
                ),
                "cost_estimate": None,
                "cost": report["cost"],
                "selected": False,
            }
        )
    return candidates


def static_hosting_candidate(
    assessment: StaticSiteAssessment,
    *,
    configured: bool,
    public_access: bool,
    configuration_reason: str | None = None,
) -> dict:
    """Evaluate static hosting without treating a source tree as a built bundle.

    The static backend can be selected from an upload after classification.
    GitHub and deployment-group paths still use their separate target lists.
    """
    identity = backend_identity("aws-s3-cloudfront")
    if assessment.status == "server_or_mixed":
        structural_status, status = "incompatible", "rejected"
        reasons = [*assessment.reasons]
        rule_ids = ["STATIC-SERVER-01"]
    elif assessment.status == "needs_build":
        structural_status, status = "potentially_compatible", "needs_build"
        reasons = [*assessment.reasons, "빌드 산출물과 API 의존성을 확인해야 합니다."]
        rule_ids = ["STATIC-BUILD-01"]
    elif assessment.status in {"needs_review", "unknown"}:
        structural_status, status = "unknown", "needs_review"
        reasons = [*assessment.reasons]
        rule_ids = ["STATIC-REVIEW-01"]
    else:
        structural_status, status = "compatible", "eligible"
        reasons = [*assessment.reasons]
        rule_ids = []
    if not public_access and status == "eligible":
        status = "rejected"
        reasons.append("현재 정적 호스팅 경로는 공개 HTTPS만 지원합니다.")
        rule_ids.append("ACCESS-01")
    if not configured and status == "eligible":
        status = "requires_setup"
        reasons.append(configuration_reason or "AWS 설정이 필요합니다.")
        rule_ids.append("SETUP_REQUIRED")
    return {
        "id": identity.target,
        "provider": identity.provider,
        "backend": identity.backend,
        "sky_adapter_support": identity.sky_adapter_support,
        "selection_mode": identity.selection_mode,
        "structural_status": structural_status,
        "execution_status": "implemented_unverified",
        "status": status,
        "reasons": reasons,
        "violated_rule_ids": rule_ids if status == "rejected" else [],
        "unknown_rule_ids": rule_ids if status in {"needs_review", "needs_build"} else [],
        "reason_codes": rule_ids,
        "evidence_ids": [],
        "evidence_files": list(assessment.evidence_files),
        "cost_estimate": None,
        "cost": cost_exposure("aws-s3-cloudfront"),
        "selected": False,
    }
