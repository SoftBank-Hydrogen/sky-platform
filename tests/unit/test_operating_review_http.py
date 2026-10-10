"""Real HTTP framing/auth over a CAS fake; no AWS or PostgreSQL calls."""

import json
import threading
from dataclasses import asdict, replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from domain.access import LoginSource, Role
from interfaces.http.operating_review import handler_for_operating_review
from interfaces.http.shared_database import SharedDatabaseApp
from interfaces.operating_review import OperatingReviewController, load_operating_policies
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


@pytest.fixture
def web(setup, request):
    records, policy, _ = setup
    if getattr(request, "param", None) == "capacity":
        from tests.unit.test_promotion_policy import policy_with_criteria, samples

        policy = policy_with_criteria(policy)
        samples(records)
    actor = replace(principal(), login_source=LoginSource.EXTERNAL_IDP)
    identities = {
        "deployer": actor,
        "viewer": replace(actor, role=Role.VIEWER),
        "foreign": replace(actor, organization_id="foreign"),
    }
    auth = Mock(authenticate_request=lambda headers: identities.get(headers.get("Authorization")))
    app = SharedDatabaseApp(Mock(), Mock(check_ready=Mock()), authenticator=auth, origin="http://127.0.0.1")
    controller = OperatingReviewController(records, {ID: policy}, clock=lambda: NOW)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_operating_review(app, controller))
    app.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(action="review", body=None, *, headers=None, method="POST"):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        values = {"Authorization": "deployer", "Origin": app.origin, "Content-Type": "application/json"}
        values.update(headers or {})
        path = f"/api/jobs/{ID}/operating-review" + ("/" + action if method == "POST" else "")
        try:
            connection.request(method, path, json.dumps(body if body is not None else {}), values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        yield request, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_review_get_approve_and_viewer_read_are_bound_and_execution_disabled(web):
    request, records = web
    status, body = request()
    assert status == 200 and body["execution_enabled"] is False
    proposal = body["proposal"]
    assert request(method="GET", headers={"Authorization": "viewer"})[1]["proposal"] == proposal
    status, consent = request("approve", {"proposal_id": proposal["proposal_id"], "confirm_review": True})
    assert status == 200 and consent["proposal"]["status"] == "approved"
    assert consent["proposal"]["execution_status"] == "unsupported_by_sky"
    assert records.job["status"] == "succeeded" and records.save_calls == 2


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "viewer"}, 403),
        ({"Authorization": "foreign"}, 404),
        ({"Authorization": "invalid"}, 403),
        ({"Origin": "https://foreign.example"}, 403),
        ({"X-Sky-Token": "legacy"}, 403),
        ({"Content-Type": "text/plain"}, 400),
    ],
)
def test_invalid_auth_origin_or_framing_does_not_persist(web, headers, status):
    request, records = web
    assert request(headers=headers)[0] == status
    assert records.save_calls == 0


@pytest.mark.parametrize(
    "action,body",
    [
        ("review", {"target": {}}),
        ("review", {"now": NOW}),
        ("approve", {"proposal_id": "OPR-" + "a" * 24, "confirm_review": 1}),
        ("approve", {"proposal_id": "OPR-" + "a" * 24}),
    ],
)
def test_browser_cannot_choose_time_target_or_implicit_consent(web, action, body):
    request, records = web
    assert request(action, body)[0] == 400
    assert records.save_calls == 0


def test_changed_binding_and_unavailable_database_return_sanitized_errors(web):
    request, records = web
    proposal = request()[1]["proposal"]
    records.job["workload_database_binding"] = {"password": "must-not-leak"}
    status, body = request("approve", {"proposal_id": proposal["proposal_id"], "confirm_review": True})
    assert status == 409 and "must-not-leak" not in str(body)
    records.load_job = Mock(side_effect=OSError("private-secret"))
    status, body = request(method="GET")
    assert status == 503 and "private-secret" not in str(body)


def test_policy_file_pins_account_and_rejects_duplicates(setup, tmp_path):
    _, policy, _ = setup
    item = {"job_id": ID, **asdict(policy)}
    path = tmp_path / "policies.json"
    document = {"schema_version": 1, "policies": [item]}
    path.write_text(json.dumps(document))
    assert load_operating_policies(
        path, account_id=policy.target.account_id, region=policy.target.region
    ) == {ID: policy}
    with pytest.raises(ValueError, match="account"):
        load_operating_policies(path, account_id="222222222222", region=policy.target.region)
    document["policies"].append(item)
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="duplicate"):
        load_operating_policies(path, account_id=policy.target.account_id, region=policy.target.region)
    path.write_text('{"schema_version":1,"schema_version":1,"policies":[]}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_operating_policies(path, account_id=policy.target.account_id, region=policy.target.region)


@pytest.mark.parametrize("web", ["capacity"], indirect=True)
def test_explicit_automation_consent_http_and_revocation(web):
    request, records = web
    payload = {
        "confirm_automation": True,
        "max_incremental_monthly_cents": 3000,
        "expires_at": "2026-10-12T00:00:00+00:00",
    }
    assert request("grant-automation", payload, headers={"Authorization": "viewer"})[0] == 403
    assert request("grant-automation", {**payload, "confirm_automation": 1})[0] == 400
    assert request("grant-automation", {**payload, "target": {}})[0] == 400
    status, result = request("grant-automation", payload)
    assert status == 200 and result["execution_enabled"] is False
    assert result["automation_consent"]["user_id"] == "user1"
    proposal = request()[1]["proposal"]
    assert proposal["status"] == "approved" and proposal["decision"]["capacity"]["eligible"]
    assert request("revoke-automation", {"confirm_revoke": True})[0] == 200
    assert records.job["promotion_automation_consent"] is None
    assert request()[1]["proposal"]["status"] == "pending_approval"
