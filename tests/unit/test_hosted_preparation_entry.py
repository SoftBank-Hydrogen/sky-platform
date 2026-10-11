"""Cognito-hosted root serves the UI for its actual preparation API."""

import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from domain.access import LoginSource, Principal, Role
from interfaces.http.deployment_preparation import DatabasePreparationApp, handler_for_preparation


@pytest.mark.parametrize("path", ["/", "/prepare", "/?demo=1"])
def test_hosted_entry_uses_preparation_page_and_preserves_authentication(path):
    actor = Principal("user", "team", Role.DEPLOYER, LoginSource.EXTERNAL_IDP)
    authenticator = Mock(
        authenticate_request=lambda headers: actor if headers.get("Authorization") == "test" else None
    )
    app = DatabasePreparationApp(
        Mock(), Mock(), Mock(), authenticator=authenticator, origin="https://cloudas.store"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_preparation(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for headers, expected in [({}, 403), ({"Authorization": "test"}, 200)]:
            client = HTTPConnection(*server.server_address, timeout=5)
            client.request("GET", path, headers=headers)
            response = client.getresponse()
            body = response.read().decode()
            assert response.status == expected
            if expected == 200:
                assert "/api/deployments" in body
                assert "aws-ecs-express" not in body
                assert "__TOKEN__" not in body
                assert response.getheader("Cache-Control") == "no-store"
            client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
