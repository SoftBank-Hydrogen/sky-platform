"""Queue routing, replay, acknowledgement and ownership fences."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.shared_database_consumer import (
    AllocationOwnershipLost,
    AllocationQueueRouter,
    SharedDatabaseConsumer,
)
from ports.operations import OutboxDelivery
from ports.queue import ReceivedOperation


@pytest.fixture
def consumer():
    operation = SimpleNamespace(
        id="op", attempt_id="attempt", application_id="app", kind="db_shared_allocate", status="queued"
    )
    store = Mock(workspace="team", get=Mock(return_value=operation))
    queue = Mock(
        receive=Mock(
            return_value=ReceivedOperation(
                "receipt",
                {"workspace": "team", "operation_id": "op", "attempt_id": "attempt", "application_id": "app"},
            )
        )
    )
    worker = Mock(execute=Mock(return_value={"status": "succeeded"}))
    return SharedDatabaseConsumer(store, queue, worker), store, queue, worker, operation


@pytest.mark.parametrize("status", ["succeeded", "failed", "needs_attention"])
def test_acknowledges_terminal_replay_without_execution(consumer, status):
    service, _store, queue, worker, operation = consumer
    operation.status = status
    assert service.consume_once() == "duplicate"
    queue.delete.assert_called_once()
    worker.execute.assert_not_called()


@pytest.mark.parametrize("change", ["scope", "application", "kind", "malformed", "running"])
def test_unowned_deliveries_never_execute_or_acknowledge(consumer, change):
    service, store, queue, worker, operation = consumer
    if change == "scope":
        store.workspace = "another"
    elif change == "application":
        operation.application_id = "another"
    elif change == "kind":
        operation.kind = "deploy"
    elif change == "malformed":
        queue.receive.return_value = ReceivedOperation("receipt", None)
    else:
        operation.status = "running"
    service.consume_once()
    worker.execute.assert_not_called()
    queue.delete.assert_not_called()
    queue.extend.assert_called_once()


def test_worker_return_alone_does_not_acknowledge(consumer):
    service, _store, queue, worker, _operation = consumer
    assert service.consume_once() == "succeeded"
    queue.delete.assert_not_called()
    assert worker.ownership is None


def test_acknowledge_only_after_durable_commit(consumer):
    service, _store, queue, worker, operation = consumer

    def execute(*_):
        operation.status = "succeeded"
        return {"status": "succeeded"}

    worker.execute.side_effect = execute
    assert service.consume_once() == "succeeded"
    queue.delete.assert_called_once()


def test_lost_ownership_leaves_message_for_recovery(consumer):
    service, _store, queue, worker, _operation = consumer
    worker.execute.side_effect = AllocationOwnershipLost()
    assert service.consume_once() == "unavailable"
    queue.delete.assert_not_called()


def test_guard_renews_both_leases_and_refuses_lost_db_lease(consumer):
    service, store, queue, _worker, _operation = consumer
    store.heartbeat.return_value = True
    with service.ownership("lease", "delivery") as guard:
        guard()
        queue.extend.assert_called_with("delivery", seconds=300)
        store.heartbeat.return_value = False
        with pytest.raises(AllocationOwnershipLost):
            guard()
        with pytest.raises(AllocationOwnershipLost):
            guard()


def test_rapid_guards_keep_db_fencing_without_throttling_sqs(consumer, monkeypatch):
    service, store, queue, _worker, _operation = consumer
    store.heartbeat.return_value = True
    service.protection = Mock()
    clock = Mock(return_value=100.0)
    monkeypatch.setattr("application.shared_database_consumer.time.monotonic", clock)
    with service.ownership("lease", "delivery") as guard:
        guard()
        guard()
        guard()
        assert store.heartbeat.call_count == 3
        queue.extend.assert_called_once_with("delivery", seconds=300)
        service.protection.set.assert_called_once_with(True)
        clock.return_value = 131.0
        guard()
        assert store.heartbeat.call_count == 4
        assert queue.extend.call_count == 2
        assert service.protection.set.call_count == 2


def test_router_reads_kind_from_database_and_preserves_delivery(consumer):
    _, store, default, allocation, operation = consumer
    router = AllocationQueueRouter(store, default, allocation)
    delivery = OutboxDelivery("event", "owner", 1, "op", "attempt", "app", "team")
    router.publish(delivery)
    allocation.publish.assert_called_once_with(delivery)
    default.publish.assert_not_called()
    operation.kind = "deploy"
    router.publish(delivery)
    default.publish.assert_called_once_with(delivery)
    with pytest.raises(OSError):
        router.publish(replace(delivery, workspace="foreign"))


def test_missing_allocation_queue_never_sends_allocation_to_builder(consumer):
    _, store, default, _, _ = consumer
    router = AllocationQueueRouter(store, default, None)
    delivery = OutboxDelivery("event", "owner", 1, "op", "attempt", "app", "team")
    with pytest.raises(OSError):
        router.publish(delivery)
    default.publish.assert_not_called()


def test_task_protection_failure_prevents_allocation(consumer):
    service, _store, queue, worker, _operation = consumer
    service.protection = Mock(set=Mock(side_effect=OSError()))
    assert service.consume_once() == "unavailable"
    worker.execute.assert_not_called()
    queue.delete.assert_not_called()


def test_task_protection_is_enabled_and_released(consumer):
    service, _store, _queue, _worker, _operation = consumer
    service.protection = Mock()
    service.consume_once()
    assert [call.args for call in service.protection.set.call_args_list] == [(True,), (False,)]
