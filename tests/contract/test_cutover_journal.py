import pytest

from application.database_cutover import OperationCutoverJournal
from ports.operations import ApplicationBusy
from tests.contract.test_operation_store import database as database  # noqa: PLC0414
from tests.contract.test_operation_store import store as store  # noqa: PLC0414


def test_unknown_cutover_retains_intent_checkpoint_and_app_scope(store):
    operation = store.admit("app", "deploy", "cutover", {"plan_id": "approved"})
    lease = store.claim(operation.id, operation.attempt_id, "worker", seconds=900)
    journal = OperationCutoverJournal(store, lease)
    journal.begin({"plan_id": "approved"})
    journal({"stage": "final_sync", "status": "observed", "receipt": {"rows": 3}})
    journal({"stage": "activate_target", "status": "intent"})
    journal.hold()
    saved = store.get(operation.id)
    assert saved.status == "needs_attention" and saved.external_pending
    assert saved.checkpoint["cutover_steps"][-1]["status"] == "intent"
    with pytest.raises(ApplicationBusy):
        store.admit("app", "deploy", "another", {})


def test_verified_cutover_completes_and_releases_scope(store):
    operation = store.admit("app", "deploy", "cutover", {})
    lease = store.claim(operation.id, operation.attempt_id, "worker", seconds=900)
    journal = OperationCutoverJournal(store, lease)
    journal.begin({"plan_id": "approved"})
    with pytest.raises(ValueError, match="reconciliation"):
        OperationCutoverJournal(store, lease).begin({"plan_id": "approved"})
    journal({"stage": "probe_target", "status": "observed", "receipt": {"http_status": 200}})
    journal.finish({"binding": "target", "single_writer": True})
    assert store.get(operation.id).status == "succeeded"
    store.admit("app", "deploy", "next", {})
