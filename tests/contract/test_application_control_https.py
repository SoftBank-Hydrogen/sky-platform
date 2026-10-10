"""Real HTTPS transport; application/DB rebind callbacks are controlled fixtures."""

import ipaddress
import json
import secrets
import ssl
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from adapters.database.application_traffic import RegisteredApplicationTraffic
from adapters.database.application_write_gate import ApplicationWriteGate
from tests.unit.test_application_traffic import setup as make_fixture


@pytest.fixture
def https_app(tmp_path):
    flow, plan, _, _, _ = make_fixture()
    bindings = [plan["source"], plan["target"]]
    gate = ApplicationWriteGate(
        "https-fixture", bindings[0], bindings, rebind=lambda binding: None, read_probe=lambda binding: True
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "control-fixture")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "control.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    key_path.chmod(0o600)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_path, key_path)
    trust = ssl.create_default_context(cafile=str(cert_path))
    token = secrets.token_urlsafe(32)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/control")
                self.end_headers()
                return
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_response(401)
                self.end_headers()
                return
            command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(command)
            body = json.dumps(gate.control(command)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = server_tls.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "https://127.0.0.1:" + str(server.server_port)

    def traffic(path="/control", context=trust):
        return RegisteredApplicationTraffic(
            url + path,
            token,
            controller_id="https-fixture",
            source=bindings[0],
            target=bindings[1],
            guard=lambda: None,
            ssl_context=context,
        )

    try:
        yield flow, plan, gate, traffic, calls, url, trust
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_tls_control_runs_cutover_and_reverse_without_leaking_auth(https_app):
    flow, plan, gate, traffic, calls, url, trust = https_app
    with pytest.raises(HTTPError) as error:
        urlopen(Request(url + "/control", data=b"{}"), context=trust, timeout=5)
    assert error.value.code == 401 and not calls
    flow.traffic = traffic()
    result = flow.promote(plan, plan)
    assert gate.binding == plan["target"] and not gate.blocked
    flow.rollback(plan, result["baseline"])
    assert gate.binding == plan["source"] and not gate.blocked
    assert [call["action"] for call in calls] == ["quiesce", "activate", "probe", "resume"] * 2
    assert all("Authorization" not in call for call in calls)


@pytest.mark.parametrize("failure", ["untrusted_ca", "redirect"])
def test_tls_or_redirect_failure_never_reaches_application_gate(https_app, failure):
    _, plan, gate, traffic, calls, _, _ = https_app
    controller = (
        traffic(context=ssl.create_default_context()) if failure == "untrusted_ca" else traffic("/redirect")
    )
    with pytest.raises(RuntimeError, match="reconciliation"):
        controller.quiesce(plan["source"])
    assert not calls and not gate.blocked and controller.uncertain
