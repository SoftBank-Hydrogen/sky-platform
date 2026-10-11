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


def test_http_consent_outbox_and_consumer_share_durable_operation(persisted, store):
    import json
    import threading
    from dataclasses import replace
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer

    from application.database_cutover_workflow import KIND
    from application.outbox import OutboxPublisher
    from application.shared_database_consumer import AllocationQueueRouter, SharedDatabaseConsumer
    from domain.access import LoginSource
    from interfaces.database_cutover import DatabaseCutoverController
    from interfaces.http.database_cutover import handler_for_database_cutover
    from interfaces.http.shared_database import SharedDatabaseApp
    from interfaces.operating_review import OperatingReviewController
    from ports.queue import ReceivedOperation

    admission, worker, records, factory, _, proposal_id = persisted
    actor = replace(principal(), login_source=LoginSource.EXTERNAL_IDP)
    app = SharedDatabaseApp(Mock(), Mock(check_ready=Mock()),
        authenticator=Mock(authenticate_request=lambda _: actor), origin='http://127.0.0.1')
    reviews = OperatingReviewController(records, admission.policies, clock=lambda: NOW)
    controller = DatabaseCutoverController(reviews, admission)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for_database_cutover(app, reviews, controller))
    app.origin = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()

    def request(method, suffix='', document=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            connection.request(method, f'/api/jobs/{ID}/database-cutover'+suffix,
                json.dumps(document), {'Content-Type': 'application/json', 'Origin': app.origin})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:connection.close()

    try:
        consent = {'proposal_id': proposal_id, 'request_key': 'real-postgres-http', 'confirm_cutover': True}
        status, accepted = request('POST', document=consent)
        assert status == 202 and accepted['status'] == 'queued'
        assert request('POST', document=consent)[1]['operation_id'] == accepted['operation_id']
        factory.assert_not_called()
        cutover_queue, default, allocations = Mock(), Mock(), Mock()
        router = AllocationQueueRouter(store, default, allocations, cutover_queue=cutover_queue)
        report = OutboxPublisher(store, router, 'http-cutover-contract').dispatch_once()
        assert report.confirmed == 1 and report.deferred == 0
        default.publish.assert_not_called();allocations.publish.assert_not_called()
        delivery = cutover_queue.publish.call_args.args[0]
        message = delivery.message()
        assert set(message) == {'version', 'workspace', 'operation_id', 'attempt_id', 'application_id'}
        cutover_queue.receive.return_value = ReceivedOperation('receipt', message)
        consumer = SharedDatabaseConsumer(store, cutover_queue, worker, kind=KIND)
        assert consumer.consume_once() == 'succeeded'
        cutover_queue.delete.assert_called_once()
        assert request('GET', '/'+accepted['operation_id'])[1]['status'] == 'succeeded'
        assert records.load_job(ID).record['workload_database_binding']['role'] == 'dedicated_workload'
        assert consumer.consume_once() == 'duplicate'
        factory.assert_called_once()
    finally:
        server.shutdown();server.server_close();thread.join(5)
