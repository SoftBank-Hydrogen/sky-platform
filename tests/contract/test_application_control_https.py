"""Real HTTPS transport; application/DB rebind callbacks are controlled fixtures."""

import ipaddress
import secrets
import ssl
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
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

    from interfaces.http.application_control import application_control_handler

    original = gate.control

    def observed(command):
        calls.append(command)
        return original(command)

    gate.control = observed

    class Handler(application_control_handler(gate, token)):
        def do_POST(self):
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/control")
                self.end_headers()
                return
            return super().do_POST()

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


@pytest.mark.parametrize("failure", [None, "untrusted_ca", "redirect", "wrong_identity"])
def test_pinned_private_transport_preserves_tls_identity_and_never_redirects(https_app, failure):
    from adapters.database.pinned_control_https import PinnedHTTPSControlTransport

    _, plan, gate, traffic, calls, url, trust = https_app
    controller = traffic()
    endpoint = url + ("/redirect" if failure == "redirect" else "/control")
    if failure == "wrong_identity":
        endpoint = endpoint.replace("127.0.0.1", "wrong.control.internal")
    context = ssl.create_default_context() if failure == "untrusted_ca" else trust
    controller.transport = PinnedHTTPSControlTransport(
        endpoint, controller._token, address="127.0.0.1", ssl_context=context, guard=lambda: None
    )
    if failure:
        with pytest.raises(RuntimeError, match="reconciliation"):
            controller.quiesce(plan["source"])
        assert not calls and not gate.blocked
    else:
        controller.quiesce(plan["source"])
        assert gate.blocked and len(calls) == 1


@pytest.mark.parametrize(
    "headers,data",
    [
        ({"Content-Type": "text/plain"}, b"{}"),
        ({"Content-Type": "application/json", "Transfer-Encoding": "chunked"}, b"{}"),
        ({"Content-Type": "application/json"}, b"x" * 16385),
    ],
)
def test_authenticated_invalid_framing_never_reaches_gate(https_app, headers, data):
    _, _, gate, traffic, calls, url, trust = https_app
    headers = {**headers, "Authorization": "Bearer " + traffic()._token}
    with pytest.raises(HTTPError) as error:
        urlopen(Request(url + "/control", data=data, headers=headers), context=trust, timeout=5)
    assert error.value.code == 400 and not calls and not gate.blocked
