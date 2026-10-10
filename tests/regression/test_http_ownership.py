"""HTTP ownership checks must run before revealing or changing deployment state."""

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from domain.access import LoginSource, Principal, Role
from interfaces.http.server import App, handler_for


OWN = "a" * 16
FOREIGN = "b" * 16
LEGACY = "c" * 16
GROUP = "d" * 16


def _job(job_id, organization=None, *, group_id=None):
    job = {"id": job_id, "status": "succeeded", "application_id": "demo-app", "created_at": "2026-10-10"}
    if organization:
        job.update(organization_id=organization, created_by="creator")
    if group_id:
        job.update(group_id=group_id, group_order=0)
    return job


def _handler(app, principal, path, *, method="GET", headers=None, body=b""):
    app.authenticator = Mock(authenticate=Mock(return_value=principal))
    handler = handler_for(app).__new__(handler_for(app))
    handler.path = path
    handler.headers = {"X-Sky-Token": "fixture", "Content-Length": str(len(body)), **(headers or {})}
    handler.rfile = io.BytesIO(body)
    handler.json_response = Mock()
    getattr(handler, f"do_{method}")()
    return handler.json_response.call_args.args


def test_job_list_and_item_hide_foreign_and_legacy_records():
    with tempfile.TemporaryDirectory() as folder:
        app = App(Path(folder), monitor_interval=0, github_poll_interval=0)
        app.jobs = {OWN: _job(OWN, "team_a"), FOREIGN: _job(FOREIGN, "team_b"), LEGACY: _job(LEGACY)}
        viewer = Principal("viewer", "team_a", Role.VIEWER, LoginSource.EXTERNAL_IDP)
        status, jobs = _handler(app, viewer, "/api/jobs")
        assert status == 200
        assert [job["id"] for job in jobs] == [OWN]
        assert _handler(app, viewer, f"/api/jobs/{FOREIGN}") == (404, {"error": "Not found"})
        assert _handler(app, viewer, f"/api/jobs/{LEGACY}") == (404, {"error": "Not found"})


def test_mutations_require_same_organization_and_role():
    with tempfile.TemporaryDirectory() as folder:
        app = App(Path(folder), monitor_interval=0, github_poll_interval=0)
        app.jobs = {OWN: _job(OWN, "team_a"), FOREIGN: _job(FOREIGN, "team_b")}
        viewer = Principal("viewer", "team_a", Role.VIEWER, LoginSource.EXTERNAL_IDP)
        deployer = Principal("deployer", "team_a", Role.DEPLOYER, LoginSource.EXTERNAL_IDP)
        assert _handler(app, deployer, f"/api/jobs/{FOREIGN}/retire", method="POST") == (
            404, {"error": "Not found"}
        )
        assert _handler(app, viewer, f"/api/jobs/{OWN}/retire", method="POST") == (
            403, {"error": "Insufficient permission"}
        )
        assert _handler(app, deployer, f"/api/jobs/{OWN}/retire", method="POST") == (
            403, {"error": "Insufficient permission"}
        )
        assert "deployment_state" not in app.jobs[OWN]


def test_application_and_group_routes_reject_mixed_ownership():
    with tempfile.TemporaryDirectory() as folder:
        app = App(Path(folder), monitor_interval=0, github_poll_interval=0)
        app.jobs = {OWN: _job(OWN, "team_a", group_id=GROUP),
                    FOREIGN: _job(FOREIGN, "team_b", group_id=GROUP)}
        admin = Principal("admin", "team_a", Role.ADMIN, LoginSource.CORPORATE_SSO)
        assert _handler(app, admin, f"/api/deployment-groups/{GROUP}") == (404, {"error": "Not found"})
        assert _handler(app, admin, "/api/applications/demo-app/releases") == (404, {"error": "Not found"})
        assert _handler(app, admin, "/api/applications/other-app/releases") == (404, {"error": "Not found"})


def test_github_source_list_and_mutation_hide_foreign_connections():
    with tempfile.TemporaryDirectory() as folder:
        app = App(Path(folder), monitor_interval=0, github_poll_interval=0)
        app.github_sources = {
            OWN: {"id": OWN, "organization_id": "team_a", "created_by": "creator", "last_job_ids": []},
            FOREIGN: {"id": FOREIGN, "organization_id": "team_b", "created_by": "creator", "last_job_ids": []},
        }
        viewer = Principal("viewer", "team_a", Role.VIEWER, LoginSource.EXTERNAL_IDP)
        status, sources = _handler(app, viewer, "/api/github/sources")
        assert status == 200
        assert [source["id"] for source in sources] == [OWN]
        assert _handler(app, viewer, f"/api/github/sources/{FOREIGN}/pause", method="POST") == (
            404, {"error": "Not found"}
        )
        assert _handler(app, viewer, f"/api/github/sources/{OWN}/pause", method="POST") == (
            403, {"error": "Insufficient permission"}
        )


def test_create_rejects_foreign_application_before_upload_or_github_call():
    with tempfile.TemporaryDirectory() as folder:
        app = App(Path(folder), monitor_interval=0, github_poll_interval=0)
        app.jobs = {FOREIGN: _job(FOREIGN, "team_b")}
        admin = Principal("admin", "team_a", Role.ADMIN, LoginSource.EXTERNAL_IDP)
        body = json.dumps({"repository_url": "https://github.com/example/demo", "branch": "main",
                           "application_id": "demo-app", "targets": ["local-docker"],
                           "public": False, "auto_deploy": False}).encode()
        app.create_github_deployment = Mock()
        assert _handler(app, admin, "/api/github/deployments", method="POST", body=body) == (
            404, {"error": "Not found"}
        )
        app.create_github_deployment.assert_not_called()
