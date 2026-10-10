from dataclasses import asdict

import pytest

from adapters.database.application_traffic import RegisteredApplicationTraffic
from adapters.database.application_write_gate import ApplicationWriteGate
from tests.unit.test_database_cutover import fixture


class Control:
    def __init__(self, source):
        self.binding, self.generation, self.closed = source, 0, False
        self.requests = []
        self.corrupt = None

    def __call__(self, command):
        self.requests.append(command)
        assert command["expected_binding"] == self.binding
        assert command["expected_generation"] in {None, self.generation}
        action = command["action"]
        if action == "quiesce":
            self.closed = True
        else:
            assert self.closed
        if action == "activate":
            self.binding = command["target_binding"]
        if action == "resume":
            self.closed = False
        self.generation += 1
        receipt = {
            "schema_version": 1,
            "controller_id": command["controller_id"],
            "request_id": command["request_id"],
            "action": action,
            "binding": self.binding,
            "generation": self.generation,
            "writes_blocked": self.closed,
            "active_writes": 0,
            "database_read_verified": action == "probe",
        }
        if self.corrupt:
            receipt.update(self.corrupt)
        return receipt


def setup():
    flow, source, target, _, events = fixture()
    plan = {"plan_id": "test", "source": asdict(source.binding), "target": asdict(target.binding)}
    control = Control(plan["source"])
    traffic = RegisteredApplicationTraffic(
        "https://registered.internal/control",
        "secret-control-token",
        controller_id="app-control-v1",
        source=plan["source"],
        target=plan["target"],
        guard=lambda: None,
        transport=control,
    )
    flow.traffic = traffic
    return flow, plan, traffic, control, events


def test_control_adapter_runs_existing_cutover_and_reverse_recovery():
    flow, plan, _traffic, control, events = setup()
    result = flow.promote(plan, plan)
    assert control.binding == plan["target"] and not control.closed
    flow.rollback(plan, result["baseline"])
    assert control.binding == plan["source"] and not control.closed
    assert [r["action"] for r in control.requests] == ["quiesce", "activate", "probe", "resume"] * 2
    assert "secret-control-token" not in str(events)


@pytest.mark.parametrize(
    "corrupt",
    [
        {"active_writes": 1},
        {"active_writes": False},
        {"writes_blocked": False},
        {"generation": True},
        {"request_id": "replayed"},
        {"controller_id": "foreign"},
        {"password": "must-not-be-recorded"},
        {"schema_version": True},
    ],
)
def test_invalid_drain_evidence_stops_before_database_fence_and_refuses_replay(corrupt):
    flow, plan, traffic, control, events = setup()
    control.corrupt = corrupt
    with pytest.raises(RuntimeError, match="reconciliation"):
        flow.promote(plan, plan)
    assert flow.source.enabled and flow.target.enabled
    assert [event["stage"] for event in events] == ["quiesce"]
    with pytest.raises(RuntimeError, match="reconcile"):
        traffic.quiesce(plan["source"])
    assert len(control.requests) == 1


def test_probe_requires_actual_database_read_and_keeps_write_gate_closed():
    _flow, plan, traffic, control, _ = setup()
    traffic.quiesce(plan["source"])
    traffic.activate(plan["source"], plan["target"])
    with pytest.raises(ValueError, match="Verified"):
        traffic.resume(plan["target"])
    control.corrupt = {"database_read_verified": False}
    with pytest.raises(RuntimeError, match="reconciliation"):
        traffic.probe(plan["target"])
    assert control.closed


def test_transport_failure_not_retried_or_exposed():
    _, plan, traffic, _, _ = setup()

    def fail(command):
        raise OSError("secret-control-token")

    traffic.transport = fail
    with pytest.raises(RuntimeError) as error:
        traffic.quiesce(plan["source"])
    assert "secret-control-token" not in str(error.value)
    assert traffic.uncertain


def test_unregistered_binding_cannot_reach_transport():
    _, plan, traffic, control, _ = setup()
    with pytest.raises(ValueError, match="Unregistered"):
        traffic.quiesce({**plan["source"], "application_id": "another-app"})
    assert not control.requests


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://internal/control",
        "https://user@host/control",
        "https://host/control?token=secret",
        "https://host/control#part",
    ],
)
def test_only_operator_pinned_https_endpoint_allowed(endpoint):
    _, plan, _, _, _ = setup()
    with pytest.raises(ValueError, match="HTTPS"):
        RegisteredApplicationTraffic(
            endpoint,
            "token",
            controller_id="test",
            source=plan["source"],
            target=plan["target"],
            guard=lambda: None,
        )


def test_app_side_gate_drains_real_writer_before_cutover():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    flow, plan, traffic, _, _ = setup()
    active, finish, attempted = Event(), Event(), Event()
    reads, connections = [], []
    gate = ApplicationWriteGate(
        "app-control-v1",
        plan["source"],
        [plan["source"], plan["target"]],
        rebind=connections.append,
        read_probe=lambda binding: reads.append(binding) or True,
    )

    def transport(command):
        attempted.set()
        return gate.control(command)

    def writer():
        with gate.write(plan["source"]):
            active.set()
            assert finish.wait(5)

    traffic.transport = transport
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending_write = executor.submit(writer)
        assert active.wait(5)
        cutover = executor.submit(flow.promote, plan, plan)
        assert attempted.wait(5)
        assert not cutover.done() and not connections
        finish.set()
        pending_write.result(timeout=5)
        result = cutover.result(timeout=5)
    assert result["binding"] == plan["target"]
    assert reads == connections == [plan["target"]]
    with pytest.raises(RuntimeError, match="binding"), gate.write(plan["source"]):
        pass
    with gate.write(plan["target"]):
        assert gate.active == 1
    flow.rollback(plan, result["baseline"])
    with gate.write(plan["source"]):
        assert gate.active == 1


def test_app_partial_rebind_failure_keeps_all_writes_closed():
    _, plan, traffic, _, _ = setup()

    def rebind(binding):
        raise OSError("ambiguous secret or connection update")

    gate = ApplicationWriteGate(
        "app-control-v1",
        plan["source"],
        [plan["source"], plan["target"]],
        rebind=rebind,
        read_probe=lambda binding: True,
    )
    traffic.transport = gate.control
    traffic.quiesce(plan["source"])
    with pytest.raises(RuntimeError, match="reconciliation"):
        traffic.activate(plan["source"], plan["target"])
    assert gate.blocked and gate.uncertain
    for binding in (plan["source"], plan["target"]):
        with pytest.raises(RuntimeError), gate.write(binding):
            pass


def test_app_drain_timeout_keeps_gate_closed_without_switching_database():
    _, plan, traffic, _, _ = setup()
    changes = []
    gate = ApplicationWriteGate(
        "app-control-v1",
        plan["source"],
        [plan["source"], plan["target"]],
        rebind=changes.append,
        read_probe=lambda binding: True,
        drain_timeout=0.01,
    )
    traffic.transport = gate.control
    with gate.write(plan["source"]), pytest.raises(RuntimeError, match="reconciliation"):
        traffic.quiesce(plan["source"])
    assert gate.blocked and not changes
