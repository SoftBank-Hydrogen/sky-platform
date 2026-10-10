"""Verify signed ALB claims, then map only explicitly provisioned memberships."""

import base64
import json
import tempfile
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from domain.access import LoginSource, Principal, Role
from interfaces.http.alb_identity import AlbRequestAuthenticator, AlbTrust, Memberships
from interfaces.http.server import App, handler_for

SIGNER = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:loadbalancer/app/sky-auth/0123456789abcdef"
ISSUER = "https://id.example.test/"
CLIENT = "sky-client"
KEY_ID = "12345678-1234-1234-1234-123456789012"


def _part(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _token(private_key, *, header=None, claims=None):
    header = {
        "alg": "ES256",
        "kid": KEY_ID,
        "signer": SIGNER,
        "iss": ISSUER,
        "client": CLIENT,
        "exp": 2000,
        **(header or {}),
    }
    claims = {"sub": "identity-123", "email": "someone@company.example"} if claims is None else claims
    unsigned = f"{_part(header)}.{_part(claims)}"
    signature = private_key.sign(unsigned.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(signature)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return unsigned + "." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def verifier():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    calls = []

    def load(region, key_id):
        calls.append((region, key_id))
        return pem

    trust = AlbTrust(SIGNER, ISSUER, CLIENT, LoginSource.CORPORATE_SSO)
    memberships = Memberships({(ISSUER, "identity-123"): ("alice", "team_a", Role.DEPLOYER)})
    return AlbRequestAuthenticator((trust,), memberships, key_loader=load, clock=lambda: 1000), key, calls


def test_signed_claims_resolve_membership_and_ignore_unsigned_identity(verifier):
    auth, key, calls = verifier
    token = _token(key)
    assert auth.authenticate_request(
        {"x-amzn-oidc-data": token, "x-amzn-oidc-identity": "attacker"}
    ) == Principal("alice", "team_a", Role.DEPLOYER, LoginSource.CORPORATE_SSO)
    assert calls == [("ap-northeast-2", KEY_ID)]
    assert auth.authenticate_request({"x-amzn-oidc-data": token}) is not None
    assert len(calls) == 1


def test_duplicate_signed_headers_are_rejected(verifier):
    auth, key, calls = verifier
    headers = Message()
    headers.add_header("x-amzn-oidc-data", _token(key))
    headers.add_header("x-amzn-oidc-data", _token(key))
    assert auth.authenticate_request(headers) is None
    assert calls == []


@pytest.mark.parametrize(
    "changed",
    [
        {"signer": SIGNER.replace("sky-auth", "evil-auth")},
        {"iss": "https://other.example.test/"},
        {"client": "other-client"},
        {"alg": "none"},
        {"exp": 999},
        {"kid": "../other-key"},
    ],
)
def test_untrusted_header_fails_before_key_fetch(verifier, changed):
    auth, key, calls = verifier
    assert auth.authenticate_request({"x-amzn-oidc-data": _token(key, header=changed)}) is None
    assert calls == []


def test_tampered_claims_and_unregistered_subject_are_denied(verifier):
    auth, key, _ = verifier
    valid = _token(key)
    header, _, signature = valid.split(".")
    tampered = f"{header}.{_part({'sub': 'identity-123', 'email': 'changed@example.test'})}.{signature}"
    assert auth.authenticate_request({"x-amzn-oidc-data": tampered}) is None
    assert (
        auth.authenticate_request(
            {"x-amzn-oidc-data": _token(key, claims={"sub": "unknown", "email": "alice@company.example"})}
        )
        is None
    )
    assert auth.authenticate_request({"x-amzn-oidc-identity": "identity-123"}) is None


def test_wrong_signing_key_and_malformed_token_fail_closed(verifier):
    auth, _, _ = verifier
    other = ec.generate_private_key(ec.SECP256R1())
    assert auth.authenticate_request({"x-amzn-oidc-data": _token(other)}) is None
    for token in ("", "not-a-jwt", "a.b.c", "a." + "!" + ".c", "a" * 16_385):
        assert auth.authenticate_request({"x-amzn-oidc-data": token}) is None


def test_membership_file_rejects_duplicate_and_maps_source_from_trust(tmp_path: Path):
    trust_file = tmp_path / "trusts.json"
    member_file = tmp_path / "members.json"
    trust_file.write_text(
        json.dumps(
            {
                "version": 1,
                "trusts": [
                    {
                        "signer_arn": SIGNER,
                        "issuer": ISSUER,
                        "client_id": CLIENT,
                        "login_source": "external_idp",
                    }
                ],
            }
        )
    )
    member = {
        "issuer": ISSUER,
        "subject": "identity-123",
        "user_id": "alice",
        "organization_id": "team_a",
        "role": "viewer",
        "enabled": True,
    }
    member_file.write_text(json.dumps({"version": 1, "members": [member]}))
    auth = AlbRequestAuthenticator.from_files(trust_file, member_file)
    assert auth.memberships.resolve(ISSUER, "identity-123", auth.trusts[0].login_source) == Principal(
        "alice", "team_a", Role.VIEWER, LoginSource.EXTERNAL_IDP
    )
    member_file.write_text(json.dumps({"version": 1, "members": [member, member]}))
    with pytest.raises(ValueError, match="Duplicate membership"):
        AlbRequestAuthenticator.from_files(trust_file, member_file)
    member_file.write_text(json.dumps({"version": 1, "members": [{**member, "enabled": False}]}))
    auth = AlbRequestAuthenticator.from_files(trust_file, member_file)
    assert auth.memberships.resolve(ISSUER, "identity-123", auth.trusts[0].login_source) is None
    member_file.chmod(0o666)
    with pytest.raises(ValueError, match="group/world writable"):
        AlbRequestAuthenticator.from_files(trust_file, member_file)


def test_json_documents_share_file_validation(tmp_path: Path):
    trusts = {
        "version": 1,
        "trusts": [
            {"signer_arn": SIGNER, "issuer": ISSUER, "client_id": CLIENT, "login_source": "external_idp"}
        ],
    }
    member = {
        "issuer": ISSUER,
        "subject": "identity-123",
        "user_id": "alice",
        "organization_id": "team_a",
        "role": "deployer",
        "enabled": True,
    }
    trust_file, member_file = tmp_path / "trusts.json", tmp_path / "members.json"

    def both(members):
        trust_file.write_text(json.dumps(trusts))
        member_file.write_text(json.dumps(members))
        from_files = AlbRequestAuthenticator.from_files(trust_file, member_file)
        from_json = AlbRequestAuthenticator.from_json(json.dumps(trusts), json.dumps(members))
        assert from_json.trusts == from_files.trusts
        assert from_json.memberships.records == from_files.memberships.records
        return from_json

    auth = both({"version": 1, "members": [member]})
    assert auth.memberships.resolve(ISSUER, "identity-123", LoginSource.EXTERNAL_IDP) == Principal(
        "alice", "team_a", Role.DEPLOYER, LoginSource.EXTERNAL_IDP
    )
    for members, match in [
        ({"version": 1, "members": [member, member]}, "Duplicate membership"),
        ({"version": 2, "members": [member]}, "Invalid memberships file"),
        ({"version": 1, "members": [{**member, "organization_id": "-team"}]}, "organization_id"),
        ({"version": 1, "members": [{**member, "role": "owner"}]}, "Role"),
    ]:
        with pytest.raises(ValueError, match=match):
            both(members)
    with pytest.raises(ValueError, match="too large"):
        AlbRequestAuthenticator.from_json(json.dumps(trusts), " " * 1_048_577)
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        AlbRequestAuthenticator.from_json('{"version": 1, "version": 1, "trusts": []}', "{}")
    with pytest.raises(TypeError):
        AlbRequestAuthenticator.from_json(None, "{}")


def test_signed_identity_filters_http_jobs_without_local_token(verifier):
    auth, key, _ = verifier
    with tempfile.TemporaryDirectory() as folder:
        app = App(Path(folder), authenticator=auth, monitor_interval=0, github_poll_interval=0)
        owned, foreign = "a" * 16, "b" * 16
        app.jobs = {
            owned: {"id": owned, "status": "succeeded", "organization_id": "team_a", "created_by": "alice"},
            foreign: {"id": foreign, "status": "succeeded", "organization_id": "team_b", "created_by": "bob"},
        }
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = "/api/jobs"
        handler.headers = {"x-amzn-oidc-data": _token(key)}
        handler.json_response = Mock()
        handler.do_GET()
        status, jobs = handler.json_response.call_args.args
        assert status == 200
        assert [job["id"] for job in jobs] == [owned]
        handler.headers = {"x-amzn-oidc-data": _token(ec.generate_private_key(ec.SECP256R1()))}
        handler.do_GET()
        handler.json_response.assert_called_with(403, {"error": "Invalid session token"})
