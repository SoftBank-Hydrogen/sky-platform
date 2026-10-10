"""Actual PostgreSQL -> HTTP/auth -> observer -> HTTP/WebSocket game integration."""

import json
import os
import subprocess
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from domain.access import LoginSource, Role
from interfaces.http.alb_identity import AlbRequestAuthenticator, AlbTrust, Memberships
from interfaces.http.deployment_reads import DatabaseReadApp
from interfaces.http.server import App, handler_for
from tests.contract.test_deployment_reads import job
from tests.unit.test_alb_identity import CLIENT, ISSUER, SIGNER, _token

pytest_plugins = ["tests.contract.test_deployment_reads"]
ROOT = Path(__file__).resolve().parents[2]
GAME_ID = "0000000000000001"


@pytest.fixture
def integration(setup, tmp_path):
    store, _connect, service = setup
    driver_path = ROOT / "tests/support/monitoring_driver.cjs"
    if not (ROOT / "ops/monitoring/exporter/node_modules/ws").exists():
        pytest.fail("Run npm ci in ops/monitoring/exporter before integration tests")
    driver = subprocess.Popen(
        ["node", str(driver_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    servers = []
    threads = []
    try:
        games = json.loads(driver.stdout.readline())["games"]
        key = ec.generate_private_key(ec.SECP256R1())
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        auth = AlbRequestAuthenticator(
            (AlbTrust(SIGNER, ISSUER, CLIENT, LoginSource.EXTERNAL_IDP),),
            Memberships({(ISSUER, "identity-123"): ("user1", "org1", Role.VIEWER)}),
            key_loader=lambda region, key_id: public,
            clock=lambda: 1000,
        )
        app = DatabaseReadApp(service, workspace=store.workspace, authenticator=auth)
        backend = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
        state = {"jwt": _token(key), "page_failure": False, "detail_failure": False, "paths": []}

        class Edge(BaseHTTPRequestHandler):
            def do_GET(self):
                state["paths"].append(self.path)
                assert self.headers.get("X-Sky-Token") is None
                if self.path != "/health" and self.headers.get("Cookie") != "session=integration":
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return
                if (state["page_failure"] and "cursor=" in self.path) or (
                    state["detail_failure"] and self.path == "/api/jobs/" + GAME_ID
                ):
                    self.send_response(503)
                    self.end_headers()
                    return
                connection = HTTPConnection(*backend.server_address, timeout=5)
                try:
                    connection.request("GET", self.path, headers={"x-amzn-oidc-data": state["jwt"]})
                    response = connection.getresponse()
                    payload = response.read()
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "text/plain"))
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    connection.close()

            def log_message(self, *args):
                pass

        edge = ThreadingHTTPServer(("127.0.0.1", 0), Edge)
        for server in (backend, edge):
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        cookie = tmp_path / "cookie"
        cookie.write_text("session=integration")
        target = {
            "name": "sky",
            "kind": "sky",
            "authMode": "hosted",
            "cookieFile": str(cookie),
            "httpUrl": f"http://127.0.0.1:{edge.server_port}",
            "discovery": {"allowedHosts": ["127.0.0.1"]},
        }
        for index in range(50):
            identifier = f"{index + 100:016x}"
            store.save_job(identifier, job(identifier, status="running"))
        game_record = job(
            GAME_ID,
            application_id="test-game",
            target="aws-ecs-express",
            deployment_state="active",
            result={"url": games[0]},
            application_ir={"hypotheses": [{"kind": "sky-probe-protocol"}]},
        )
        store.save_job(GAME_ID, game_record)
        store.save_job("f" * 16, job("f" * 16, organization_id="other-org"))

        def poll(**options):
            driver.stdin.write(json.dumps({"target": target, **options}) + "\n")
            driver.stdin.flush()
            return json.loads(driver.stdout.readline())

        yield store, app, backend, target, games, game_record, key, state, cookie, poll
    finally:
        if driver.poll() is None:
            driver.stdin.write('{"action":"close"}\n')
            driver.stdin.flush()
            try:
                driver.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.communicate()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_database_signed_auth_pagination_discovery_and_failure_recovery(integration):
    store, _app, backend, _target, games, record, key, state, cookie, poll = integration
    first = poll()
    assert first["snapshot"]["sky_observed_collection_up"] == 1
    assert first["snapshot"]["sky_observed_jobs"] == {"succeeded": 1, "failed": 0, "other": 50}
    assert any("cursor=" in path for path in state["paths"])
    assert first["targets"][0]["httpUrl"] == games[0]
    name = first["targets"][0]["name"]
    assert "sky_observed_websocket_up" in first["metrics"]
    assert first["gameStates"][0]["probes"] == 1
    assert first["gameStates"][0]["joins"] == 0
    assert all(item["cookies"] == 0 for item in first["gameStates"])

    for failure in ("page_failure", "detail_failure"):
        state[failure] = True
        failed = poll()
        assert failed["snapshot"]["sky_discovery_up"] == 0
        assert failed["targets"] == first["targets"]
        if failure == "page_failure":
            assert "sky_observed_jobs" not in failed["snapshot"]
        state[failure] = False

    state["jwt"] = _token(key, header={"exp": 999})
    expired = poll()
    assert expired["snapshot"]["sky_observed_collection_error"] == "auth"
    assert expired["targets"] == first["targets"]
    state["jwt"] = _token(key)
    cookie.write_text("session=expired")
    redirected = poll()
    assert redirected["snapshot"]["sky_discovery_up"] == 0
    assert redirected["targets"] == first["targets"]
    cookie.write_text("session=integration")

    updated = {**record, "result": {"url": games[1]}}
    store.save_job(GAME_ID, updated, expected_revision=1)
    moved = poll()
    assert moved["targets"][0]["name"] == name
    assert moved["targets"][0]["httpUrl"] == games[1]
    assert moved["snapshot"]["sky_discovery_up"] == 1
    assert 'sky_observed_websocket_up{target="' + name + '",kind="game"} 1' in moved["metrics"]
    down = poll(gameDown=True)
    assert 'sky_observed_websocket_up{target="' + name + '",kind="game"} 0' in down["metrics"]
    recovered = poll()
    assert 'sky_observed_websocket_up{target="' + name + '",kind="game"} 1' in recovered["metrics"]
    store.save_job(GAME_ID, {**updated, "deployment_state": "deleted"}, expected_revision=2)
    removed = poll()
    assert removed["targets"] == []
    assert name not in removed["metrics"]

    # A browser token cannot bypass the real hosted handler.
    connection = HTTPConnection(*backend.server_address, timeout=5)
    try:
        connection.request(
            "GET", "/api/jobs", headers={"X-Sky-Token": "local", "x-amzn-oidc-data": state["jwt"]}
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()


def test_local_legacy_http_remains_compatible(integration, tmp_path):
    _store, _app, _backend, target, _games, _record, _key, _state, _cookie, poll = integration
    app = App(tmp_path / "legacy", monitor_interval=0, github_poll_interval=0)
    app.jobs = {GAME_ID: job(GAME_ID, status="running", organization_id="local_workspace")}
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target.update(authMode="local", httpUrl=f"http://127.0.0.1:{server.server_port}")
        target.pop("cookieFile")
        result = poll()
        assert result["snapshot"]["sky_observed_collection_up"] == 1
        assert result["snapshot"]["sky_observed_jobs"]["other"] == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_actual_k6_hosted_smoke_and_expired_session(integration):
    image = os.environ.get("SKY_TEST_K6_IMAGE")
    if not image:
        pytest.skip("Set SKY_TEST_K6_IMAGE to run Docker k6 against loopback HTTP")
    _store, _app, _backend, target, _games, _record, key, state, _cookie, _poll = integration
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--read-only",
        "--cap-drop",
        "ALL",
        "-v",
        f"{ROOT / 'scripts/load'}:/scripts:ro",
        "-e",
        "AUTH_MODE=hosted",
        "-e",
        "SKY_COOKIE=session=integration",
        "-e",
        "BASE_URL=" + target["httpUrl"],
        "-e",
        "JOB_ID=" + GAME_ID,
        image,
        "run",
        "--quiet",
        "/scripts/platform.js",
    ]
    success = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    assert success.returncode == 0, success.stderr
    report = json.loads(success.stdout)
    assert report["authMode"] == "hosted"
    assert report["summary"]["metrics"]["sky_read_failures"]["values"]["rate"] == 0
    assert report["summary"]["metrics"]["iterations"]["values"]["count"] == 5
    state["jwt"] = _token(key, header={"exp": 999})
    failure = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    assert failure.returncode != 0
    assert "preflight failed" in failure.stderr
