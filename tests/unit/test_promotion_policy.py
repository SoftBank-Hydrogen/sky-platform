from dataclasses import replace
from datetime import timedelta

import pytest

from application.database_promotion_workflow import approved_snapshot
from application.operating_review import deployment_observation_binding
from engine.promotion_policy import (
    CapacityPromotionCriteria,
    PromotionCostQuote,
    evaluate_capacity,
    timestamp,
)
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


def samples(records, values=None):
    history = []
    for offset in (300, 150, 0):
        history.append(
            {
                "healthy": True,
                "reason": "Actual probe recorded",
                "checked_at": (timestamp(NOW) - timedelta(seconds=offset)).isoformat(),
                "deployment_binding": deployment_observation_binding(records.job),
                "database_metrics": {
                    "schema_version": 1,
                    "collector_id": "owned-pool-monitor",
                    "database_binding": records.job["workload_database_binding"],
                    "values": values
                    or {
                        "connections": 9,
                        "connection_limit": 10,
                        "storage_bytes": 50,
                        "storage_limit_bytes": 100,
                        "latency_p95_ms": 300,
                    },
                },
            }
        )
    records.health = history
    records.health_revision += 1
    return history


def policy_with_criteria(policy):
    return replace(
        policy,
        capacity_criteria=CapacityPromotionCriteria("owned-pool-monitor", connections_percent=80),
        cost_quote=PromotionCostQuote(2500, "operator-quote-fixture", "2026-10-12T00:00:00+00:00"),
    )


def evaluate(records, criteria):
    return evaluate_capacity(
        criteria,
        records.health,
        database_binding=records.job["workload_database_binding"],
        deployment_binding=deployment_observation_binding(records.job),
        now=NOW,
        max_age_seconds=300,
    )


def test_sustained_pressure_creates_evidence_bound_capacity_proposal(setup):
    records, policy, service = setup
    policy = policy_with_criteria(policy)
    samples(records)
    proposal = service.review(principal(), ID, policy, now=NOW)
    assert proposal["plan"]["trigger"]["kind"] == "capacity_review"
    assert proposal["decision"]["capacity"]["eligible"] is True
    assert proposal["decision"]["automatic_approval"]["reason"] == "explicit_user_consent_required"
    assert proposal["status"] == "pending_approval"
    assert proposal["plan"]["cost_estimate"]["incremental_monthly_cents"] == 2500
    assert not proposal["plan"]["ready_to_cutover"]


@pytest.mark.parametrize(
    "change", ["spike", "gap", "foreign-db", "foreign-revision", "collector", "missing", "below"]
)
def test_spikes_gaps_unknown_and_foreign_metrics_do_not_trigger(setup, change):
    records, policy, service = setup
    policy = policy_with_criteria(policy)
    history = samples(records)
    if change == "spike":
        history[0]["database_metrics"]["values"]["connections"] = 1
    elif change == "gap":
        history[1]["checked_at"] = "2026-10-10T23:56:30+00:00"
    elif change == "foreign-db":
        for item in history:
            item["database_metrics"]["database_binding"] = {"instance_id": "foreign"}
    elif change == "foreign-revision":
        history[0]["deployment_binding"] = {}
    elif change == "collector":
        for item in history:
            item["database_metrics"]["collector_id"] = "browser"
    elif change == "missing":
        history[-1]["database_metrics"]["values"] = {}
    else:
        history[-1]["database_metrics"]["values"]["connections"] = 1
    assert service.review(principal(), ID, policy, now=NOW) is None
    assert records.save_calls == 0


def test_auto_consent_and_budget_record_approval_without_running_resources(setup):
    records, policy, service = setup
    policy = policy_with_criteria(policy)
    samples(records)
    consent = service.grant_automation(
        principal(),
        ID,
        policy,
        max_incremental_monthly_cents=3000,
        expires_at="2026-10-12T00:00:00+00:00",
        now=NOW,
    )
    approved = service.review(principal(), ID, policy, now=NOW)
    assert approved["status"] == "approved" and approved["approval"]["user_id"] == consent["user_id"]
    assert approved["execution_status"] == "unsupported_by_sky"
    assert service.review(principal(), ID, policy, now=NOW) == approved
    assert approved_snapshot(records, principal(), ID, approved["proposal_id"], policy, NOW)[1] == approved
    service.revoke_automation(principal(), ID)
    with pytest.raises(ValueError, match="changed"):
        approved_snapshot(records, principal(), ID, approved["proposal_id"], policy, NOW)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("budget", "incremental_cost_exceeds_consent"),
        ("expired", "consent_expired_or_scope_changed"),
        ("quote", "current_cost_evidence_required"),
        ("policy", "consent_expired_or_scope_changed"),
    ],
)
def test_ineligible_automation_keeps_manual_approval(setup, change, reason):
    records, policy, service = setup
    policy = policy_with_criteria(policy)
    samples(records)
    service.grant_automation(
        principal(),
        ID,
        policy,
        max_incremental_monthly_cents=3000,
        expires_at="2026-10-12T00:00:00+00:00",
        now=NOW,
    )
    if change == "budget":
        policy = replace(policy, cost_quote=replace(policy.cost_quote, incremental_monthly_cents=3001))
    elif change == "expired":
        records.job["promotion_automation_consent"]["expires_at"] = NOW
    elif change == "quote":
        policy = replace(policy, cost_quote=None)
    else:
        policy = replace(policy, target=replace(policy.target, instance_id="another"))
    proposal = service.review(principal(), ID, policy, now=NOW)
    assert proposal["status"] == "pending_approval"
    assert proposal["decision"]["automatic_approval"]["reason"] == reason


def test_time_only_cannot_enable_automation_and_missing_quote_cannot_be_manually_approved(setup):
    records, policy, service = setup
    with pytest.raises(ValueError, match="Elapsed time"):
        service.grant_automation(
            principal(),
            ID,
            policy,
            max_incremental_monthly_cents=3000,
            expires_at="2026-10-12T00:00:00+00:00",
            now=NOW,
        )
    samples(records)
    policy = replace(policy_with_criteria(policy), cost_quote=None)
    proposal = service.review(principal(), ID, policy, now=NOW)
    with pytest.raises(ValueError, match="cost estimate"):
        service.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)


def test_isolation_policy_is_independent_of_capacity(setup):
    _, policy, service = setup
    proposal = service.review(principal(), ID, replace(policy, isolation_required=True), now=NOW)
    assert proposal["plan"]["trigger"]["kind"] == "isolation_review"
    assert proposal["decision"]["reason"] == "isolation_required"
    assert proposal["status"] == "pending_approval"


def test_three_metric_types_and_non_finite_measurements(setup):
    records, _, _ = setup
    samples(records)
    for key in ("latency_p95_ms", "connections_percent", "storage_percent"):
        assert evaluate(
            records, CapacityPromotionCriteria("owned-pool-monitor", **{key: 40}, storage_quota_bytes=100)
        )["eligible"]
    records.health[-1]["database_metrics"]["values"]["latency_p95_ms"] = float("nan")
    assert not evaluate(records, CapacityPromotionCriteria("owned-pool-monitor", latency_p95_ms=40))[
        "eligible"
    ]


def test_future_or_duplicate_metric_timestamps_block(setup):
    records, _, _ = setup
    samples(records)
    records.health[1]["checked_at"] = records.health[0]["checked_at"]
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate(records, CapacityPromotionCriteria("owned-pool-monitor", connections_percent=80))


def test_all_window_values_are_bound_to_proposal_identity(setup):
    records, policy, service = setup
    policy = policy_with_criteria(policy)
    samples(records)
    first = service.review(principal(), ID, policy, now=NOW)
    records.health[1]["database_metrics"]["values"]["connections"] = 8
    second = service.review(principal(), ID, policy, now=NOW)
    assert first["proposal_id"] != second["proposal_id"]
    assert (
        first["decision"]["capacity"]["evidence_sha256"] != second["decision"]["capacity"]["evidence_sha256"]
    )
