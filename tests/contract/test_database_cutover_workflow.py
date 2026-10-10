from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from adapters.state.postgres import PostgresDeploymentRecordStore
from application.database_cutover_workflow import PromotionCutoverAdmission, PromotionCutoverWorker
from ports.operations import ApplicationBusy
from tests.contract.test_operation_store import database as database  # noqa: PLC0414
from tests.contract.test_operation_store import store as store  # noqa: PLC0414
from tests.unit.test_database_cutover_workflow import flow as flow  # noqa: PLC0414
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


@pytest.fixture
def persisted(flow, store, database):
    worker, _, memory, _, registrations, factory, traffic, _, operation, _ = flow
    records = PostgresDeploymentRecordStore(database, workspace=store.workspace)
    records.save_job(ID, memory.job, expected_revision=None)
    records.save_health(ID, memory.health, expected_revision=None)
    policies = worker.validator.policies
    admission = PromotionCutoverAdmission(
        records, store, policies, registrations, clock=lambda: NOW, enabled=True
    )
    executor = PromotionCutoverWorker(
        records,
        store,
        policies,
        registrations,
        factory,
        Mock(return_value=principal()),
        owner="contract-worker",
        clock=lambda: NOW,
    )
    return (
        admission,
        executor,
        records,
        factory,
        traffic,
        operation.command["approval"]["proposal"]["proposal_id"],
    )


def test_concurrent_admission_single_operation_and_verified_projection(persisted, store):
    admission, worker, records, factory, _, proposal_id = persisted
    with ThreadPoolExecutor(max_workers=4) as pool:
        operations = list(
            pool.map(lambda _: admission.admit(principal(), ID, proposal_id, "cutover"), range(8))
        )
    assert len({op.id for op in operations}) == 1
    operation = operations[0]
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "succeeded"
    assert store.get(operation.id).status == "succeeded"
    assert records.load_job(ID).record["workload_database_binding"]["role"] == "dedicated_workload"
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "succeeded"
    factory.assert_called_once()
    store.admit("game", "deploy", "next-update", {})


def test_failed_app_probe_retains_checkpoint_and_exclusive_scope(persisted, store):
    admission, worker, records, factory, traffic, proposal_id = persisted
    operation = admission.admit(principal(), ID, proposal_id, "cutover")
    traffic.fail = True
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "needs_attention"
    saved = store.get(operation.id)
    assert saved.external_pending and saved.external_intent["action"] == "database_cutover"
    assert saved.checkpoint["cutover_steps"][-1]["stage"] == "probe_target"
    assert records.load_job(ID).record["workload_database_binding"]["role"] == "shared_workload"
    with pytest.raises(ApplicationBusy):
        store.admit("game", "deploy", "unsafe-overlap", {})
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "needs_attention"
    factory.assert_called_once()
