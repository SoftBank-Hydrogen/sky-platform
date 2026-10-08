"""Evaluate only implemented deployment targets; never invent a recommendation."""

from __future__ import annotations


def evaluate_candidates(reports: list[dict]) -> list[dict]:
    """Separate constraint failures from missing credentials and unmeasured trade-offs."""
    candidates = []
    for report in reports:
        violations = [result for result in report["constraint_results"] if result["status"] == "violated"]
        database_binding_only = (
            report["target"] == "aws-ecs-express"
            and report["database_engines"] == ["postgresql"]
            and [item["rule_id"] for item in violations] == ["DATA-BINDING-01"]
        )
        if database_binding_only:
            status = "requires_database_binding"
            reasons = ["PostgreSQL DB 생성 또는 기존 DB 연결을 선택·검증해야 배포할 수 있습니다."]
            if not report["configured"]:
                reasons.append(report["configuration_reason"])
        elif violations:
            status = "rejected"
            reasons = [result["reason"] for result in violations]
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
                "status": status,
                "reasons": reasons,
                "violated_rule_ids": [result["rule_id"] for result in violations],
                "evidence_ids": sorted(
                    {identifier for result in violations for identifier in result["evidence_ids"]}
                ),
                "cost_estimate": None,
                "selected": False,
            }
        )
    return candidates
