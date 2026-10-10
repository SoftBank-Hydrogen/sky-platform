"""The local browser token must not cross into an injected hosted identity mode."""

import io
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from domain.access import LoginSource, Principal, Role
from interfaces.http.auth import LocalTokenAuthenticator
from interfaces.http.deployment_reads import DatabaseReadApp, handler_for_reads
from interfaces.http.server import App, handler_for


def _page(app, principal, headers=None):
    app.authenticator.authenticate_request = Mock(return_value=principal)
    handler = handler_for(app).__new__(handler_for(app))
    handler.path = "/"
    handler.headers = headers or {}
    handler.json_response = Mock()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = io.BytesIO()
    handler.do_GET()
    return handler


def test_hosted_page_requires_identity_and_never_embeds_a_shared_token():
    with tempfile.TemporaryDirectory() as folder:
        authenticator = Mock(authenticate_request=Mock(return_value=None))
        app = App(Path(folder), authenticator=authenticator, monitor_interval=0, github_poll_interval=0)
        assert app.hosted is True
        assert app.token is None
        denied = _page(app, None)
        denied.json_response.assert_called_once_with(403, {"error": "Invalid session token"})
        denied.send_response.assert_not_called()
        principal = Principal("alice", "team_a", Role.VIEWER, LoginSource.EXTERNAL_IDP)
        allowed = _page(app, principal)
        allowed.send_response.assert_called_once_with(200)
        assert b"const token='';" in allowed.wfile.getvalue()
        assert b"old-local-token" not in allowed.wfile.getvalue()
        stale_client = _page(app, principal, {"X-Sky-Token": "old-local-token"})
        stale_client.json_response.assert_called_once_with(
            403, {"error": "Local session token is unavailable in hosted mode"}
        )
        stale_client.send_response.assert_not_called()


def test_hosted_boundary_rejects_local_principal_and_local_authenticator():
    with tempfile.TemporaryDirectory() as folder:
        with pytest.raises(ValueError, match="request authenticator"):
            App(Path(folder), authenticator=LocalTokenAuthenticator("secret"),
                monitor_interval=0, github_poll_interval=0)
        authenticator = Mock(authenticate_request=Mock(return_value=Principal(
            "local_operator", "local_workspace", Role.ADMIN, LoginSource.LOCAL)))
        app = App(Path(folder), authenticator=authenticator, monitor_interval=0, github_poll_interval=0)
        denied = _page(app, authenticator.authenticate_request.return_value)
        denied.json_response.assert_called_once_with(403, {"error": "Invalid session token"})


def _read_request(app, path, principal, headers=None):
    app.authenticator.authenticate_request = Mock(return_value=principal)
    handler = handler_for_reads(app).__new__(handler_for_reads(app))
    handler.path = path
    handler.headers = headers or {}
    handler.json_response = Mock()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = io.BytesIO()
    handler.do_GET()
    return handler


def test_hosted_database_reads_require_verified_identity_and_never_embed_local_token():
    authenticator = Mock(authenticate_request=Mock(return_value=None))
    service = Mock()
    app = DatabaseReadApp(service, authenticator=authenticator)
    assert app.hosted is True and app.token is None
    viewer = Principal("alice", "team_a", Role.VIEWER, LoginSource.EXTERNAL_IDP)
    for path in ("/", "/api/config", "/api/jobs"):
        denied = _read_request(app, path, None)
        denied.json_response.assert_called_once_with(403, {"error": "Invalid session token"})
        denied.send_response.assert_not_called()
        stale = _read_request(app, path, viewer, {"X-Sky-Token": "old-local-token"})
        stale.json_response.assert_called_once_with(
            403, {"error": "Local session token is unavailable in hosted mode"}
        )
        stale.send_response.assert_not_called()
        local = Principal("local_operator", "local_workspace", Role.ADMIN, LoginSource.LOCAL)
        _read_request(app, path, local).json_response.assert_called_once_with(
            403, {"error": "Invalid session token"}
        )
    allowed = _read_request(app, "/", viewer)
    allowed.send_response.assert_called_once_with(200)
    assert b"const token='';" in allowed.wfile.getvalue()
    page = _read_request(app, "/api/config", viewer)
    assert page.json_response.call_args.args[0] == 200
    health = _read_request(app, "/health", None)
    health.json_response.assert_called_once_with(200, {"status": "ok"})


def test_hosted_database_reads_reject_local_token_authenticator():
    with pytest.raises(ValueError, match="request authenticator"):
        DatabaseReadApp(Mock(), authenticator=LocalTokenAuthenticator("secret"))
