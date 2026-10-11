import json
import threading
from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from domain.access import LoginSource, Role
from interfaces.database_cutover import (
    DatabaseCutoverController,
    load_cutover_api_configuration,
    load_cutover_registrations,
)
from interfaces.http.database_cutover import handler_for_database_cutover
from interfaces.http.shared_database import SharedDatabaseApp
from interfaces.operating_review import OperatingReviewController
from tests.unit.test_database_cutover_workflow import flow as flow  # noqa: PLC0414
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


@pytest.fixture
def web(flow):
    _, admission, records, operations, _, _, _, _, operation, _ = flow
    operations.admit.return_value = operation
    operations.admit.reset_mock()
    actor = replace(principal(), login_source=LoginSource.EXTERNAL_IDP)
    identities = {
        "deployer": actor,
        "viewer": replace(actor, role=Role.VIEWER),
        "foreign": replace(actor, organization_id="foreign"),
    }
    app = SharedDatabaseApp(
        Mock(),
        Mock(check_ready=Mock()),
        authenticator=Mock(authenticate_request=lambda h: identities.get(h.get("Authorization"))),
        origin="http://127.0.0.1",
    )
    reviews = OperatingReviewController(records, admission.policies, clock=lambda: NOW)
    controller = DatabaseCutoverController(reviews, admission)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_database_cutover(app, reviews, controller))
    app.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = {
        "proposal_id": records.job["operating_proposal"]["proposal_id"],
        "request_key": "click-one",
        "confirm_cutover": True,
    }

    def request(*, data=None, method="POST", suffix="", headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        values = {"Authorization": "deployer", "Origin": app.origin, "Content-Type": "application/json"}
        values.update(headers or {})
        try:
            connection.request(
                method,
                f"/api/jobs/{ID}/database-cutover" + suffix,
                json.dumps(body if data is None else data),
                values,
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        yield request, body, flow, controller
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_http_accepts_explicit_consent_without_running_cutover_and_polls_owned_status(web):
    request, _, flow, _ = web
    _, _, _, operations, _, factory, _, _, operation, _ = flow
    status, receipt = request()
    assert status == 202 and receipt["operation_id"] == operation.id and receipt["status"] == "queued"
    assert set(receipt) == {"job_id", "operation_id", "status", "status_url", "requires_attention"}
    factory.assert_not_called()
    assert operations.admit.call_args.args[1] == "db_dedicated_cutover"
    assert request(method="GET", suffix="/" + operation.id, headers={"Authorization": "viewer"})[1] == receipt
    assert request(method="GET")[1]["execution_enabled"] is True


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "viewer"}, 403),
        ({"Authorization": "foreign"}, 404),
        ({"Authorization": "invalid"}, 403),
        ({"Origin": "https://foreign.example"}, 403),
    ],
)
def test_cutover_consent_checks_identity_owner_and_origin_before_admission(web, headers, status):
    request, _, flow, _ = web
    assert request(headers=headers)[0] == status
    flow[3].admit.assert_not_called()
    flow[5].assert_not_called()


@pytest.mark.parametrize("change", ["implicit", "target", "controller", "bad_key"])
def test_browser_cannot_supply_execution_contract_or_implicit_consent(web, change):
    request, body, flow, _ = web
    body = dict(body)
    if change == "implicit":
        body["confirm_cutover"] = 1
    elif change == "bad_key":
        body["request_key"] = "../foreign"
    else:
        body[change] = {"secret": "must-not-be-admitted"}
    assert request(data=body)[0] == 400
    flow[3].admit.assert_not_called()


def test_expired_or_disabled_registration_cannot_admit(web):
    request, _, flow, controller = web
    flow[4][ID]["verified_until"] = "2026-10-10T00:00:00+00:00"
    assert request(method="GET")[1]["execution_enabled"] is False
    assert request()[0] == 409
    flow[3].admit.assert_not_called()
    controller.admission.enabled = False
    assert request()[0] == 409


def test_poll_cannot_disclose_foreign_or_other_job_operation(web):
    request, _, flow, _ = web
    operation = replace(
        flow[8], command={**flow[8].command, "approval": {**flow[8].command["approval"], "job_id": "f" * 16}}
    )
    flow[3].get.return_value = operation
    assert request(method="GET", suffix="/" + operation.id)[0] == 404


def test_operator_registration_configuration_requires_explicit_activation_and_separate_queue(
    flow, monkeypatch
):
    _, admission, _, _, registrations, _, _, _, _, _ = flow
    raw = json.dumps({"schema_version": 1, "registrations": registrations})
    target = admission.policies[ID].target
    args = {
        "policies": admission.policies,
        "account_id": target.account_id,
        "region": target.region,
        "now": NOW,
    }
    monkeypatch.delenv("SKY_DATABASE_CUTOVER_ENABLED", raising=False)
    monkeypatch.delenv("SKY_DATABASE_CUTOVER_REGISTRATIONS_JSON", raising=False)
    assert load_cutover_api_configuration(**args) is None
    monkeypatch.setenv("SKY_DATABASE_CUTOVER_REGISTRATIONS_JSON", raw)
    with pytest.raises(ValueError):
        load_cutover_api_configuration(**args)
    monkeypatch.setenv("SKY_DATABASE_CUTOVER_ENABLED", "true")
    url = f"https://sqs.{target.region}.amazonaws.com/{target.account_id}/cutover.fifo"
    monkeypatch.setenv("SKY_DATABASE_CUTOVER_QUEUE_URL", url)
    monkeypatch.setenv("SKY_JOB_QUEUE_URL", url)
    with pytest.raises(ValueError):
        load_cutover_api_configuration(**args)
    monkeypatch.delenv("SKY_JOB_QUEUE_URL")
    assert load_cutover_api_configuration(**args) == registrations
    with pytest.raises(ValueError):
        load_cutover_registrations(
            raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
            policies=admission.policies,
            now=NOW,
        )
