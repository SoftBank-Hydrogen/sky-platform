from dataclasses import asdict

import pytest

from application.database_cutover import DatabaseCutover
from domain.database import DatabaseBinding


class Fence:
    def __init__(self, binding):
        self.binding = binding
        self.enabled = True

    def set_login(self, enabled, *, guard):
        guard()
        self.enabled = enabled
        return {"login_enabled": enabled}


class Traffic:
    def __init__(self, binding):
        self.binding = binding
        self.closed = False
        self.fail = False

    def quiesce(self, expected):
        assert self.binding == expected
        self.closed = True

    def activate(self, expected, target):
        assert self.closed and self.binding == expected
        self.binding = target

    def probe(self, target):
        assert self.closed and self.binding == target
        if self.fail:
            raise ValueError("unhealthy")

    def resume(self, target):
        assert self.closed and self.binding == target
        self.closed = False


class Sync:
    def refresh(self, plan, *, expected_destination, guard, reverse=False):
        guard()
        return {**expected_destination, "reverse": reverse}


def fixture():
    source_binding = DatabaseBinding(
        "org", "app", "shared_workload", "111111111111", "ap-northeast-2", "pool", "app_db", "source-owner"
    )
    target_binding = DatabaseBinding(
        "org",
        "app",
        "dedicated_workload",
        "111111111111",
        "ap-northeast-2",
        "dedicated",
        "app_db",
        "target-owner",
    )
    source, target, traffic, events = (
        Fence(source_binding),
        Fence(target_binding),
        Traffic(asdict(source_binding)),
        [],
    )
    flow = DatabaseCutover(Sync(), source, target, traffic, guard=lambda: None, journal=events.append)
    return flow, source, target, traffic, events


def test_single_writer_and_explicit_reverse_recovery():
    flow, source, target, traffic, events = fixture()
    plan = {"plan_id": "test", "source": asdict(source.binding), "target": asdict(target.binding)}
    result = flow.promote(plan, plan)
    assert not source.enabled and target.enabled and not traffic.closed
    flow.rollback(plan, result["baseline"])
    assert source.enabled and not target.enabled and traffic.binding == plan["source"] and not traffic.closed
    assert any(e["stage"] == "reverse_sync" and e["status"] == "observed" for e in events)


def test_failed_probe_keeps_writes_closed_until_explicit_recovery():
    flow, source, target, traffic, events = fixture()
    traffic.fail = True
    plan = {"plan_id": "test", "source": asdict(source.binding), "target": asdict(target.binding)}
    with pytest.raises(ValueError, match="unhealthy"):
        flow.promote(plan, plan)
    assert traffic.closed and not source.enabled
    final = next(e["receipt"] for e in events if e["stage"] == "final_sync" and e["status"] == "observed")
    traffic.fail = False
    flow.rollback(plan, final)
    assert source.enabled and not target.enabled and not traffic.closed


def test_journal_failure_prevents_next_external_effect():
    flow, source, target, traffic, _events = fixture()

    def fail(event):
        raise RuntimeError("storage unavailable")

    flow.journal = fail
    plan = {"plan_id": "test", "source": asdict(source.binding), "target": asdict(target.binding)}
    with pytest.raises(RuntimeError):
        flow.promote(plan, plan)
    assert not traffic.closed and source.enabled and target.enabled


def test_foreign_baseline_rejected_before_fencing_or_quiesce():
    flow, source, target, traffic, _events = fixture()
    plan = {"plan_id": "test", "source": asdict(source.binding), "target": asdict(target.binding)}
    with pytest.raises(ValueError, match="baseline"):
        flow.promote(plan, {**plan, "plan_id": "foreign"})
    assert source.enabled and target.enabled and not traffic.closed
