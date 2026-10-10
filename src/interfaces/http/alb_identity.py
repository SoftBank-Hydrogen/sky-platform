"""Verify ALB-signed OIDC claims before resolving an explicit organization membership.

Trust file: {"version": 1, "trusts": [{"signer_arn": "...", "issuer": "https://...",
"client_id": "...", "login_source": "corporate_sso|external_idp"}]}.
Membership file: {"version": 1, "members": [{"issuer": "https://...", "subject": "...",
"user_id": "...", "organization_id": "...", "role": "admin|deployer|viewer",
"enabled": true}]}. Both files are operator-provisioned; no browser claim grants membership.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from domain.access import LoginSource, Principal, Role

_ALB_ARN = re.compile(
    r"arn:aws:elasticloadbalancing:([a-z]{2}-[a-z]+-\d):\d{12}:loadbalancer/app/"
    r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16}\Z"
)
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z")


def _unique_json(payload: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise TypeError("Expected JSON object")
    return value


def _decode(segment: str) -> bytes:
    if not _BASE64URL.fullmatch(segment):
        raise ValueError("Invalid token encoding")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _read_configuration(path: Path) -> dict:
    if path.stat().st_mode & 0o022:
        raise ValueError("Identity configuration must not be group/world writable")
    with path.open("rb") as source:
        payload = source.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("Identity configuration is too large")
    return _unique_json(payload)


@dataclass(frozen=True, slots=True)
class AlbTrust:
    signer_arn: str
    issuer: str
    client_id: str
    login_source: LoginSource

    def __post_init__(self) -> None:
        if not isinstance(self.signer_arn, str) or not _ALB_ARN.fullmatch(self.signer_arn):
            raise ValueError("Invalid ALB signer ARN")
        if (
            not isinstance(self.issuer, str)
            or not self.issuer.startswith("https://")
            or len(self.issuer) > 512
        ):
            raise ValueError("Invalid OIDC issuer")
        if not isinstance(self.client_id, str) or not 1 <= len(self.client_id) <= 256:
            raise ValueError("Invalid OIDC client ID")
        if not isinstance(self.login_source, LoginSource) or self.login_source not in {
            LoginSource.CORPORATE_SSO,
            LoginSource.EXTERNAL_IDP,
        }:
            raise ValueError("Hosted login source must be corporate or external")

    @property
    def region(self) -> str:
        return _ALB_ARN.fullmatch(self.signer_arn).group(1)


class Memberships:
    """Operator-provisioned snapshot; no email or token claim creates a membership."""

    def __init__(self, records: Mapping[tuple[str, str], tuple[str, str, Role]]):
        self.records = dict(records)

    @classmethod
    def from_file(cls, path: Path, trusts: tuple[AlbTrust, ...]) -> Memberships:
        data = _read_configuration(path)
        if (
            set(data) != {"version", "members"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["members"], list)
        ):
            raise ValueError("Invalid memberships file")
        issuers = {trust.issuer for trust in trusts}
        records = {}
        for item in data["members"]:
            if not isinstance(item, dict) or set(item) != {
                "issuer",
                "subject",
                "user_id",
                "organization_id",
                "role",
                "enabled",
            }:
                raise ValueError("Invalid membership")
            issuer, subject = item["issuer"], item["subject"]
            if (
                not isinstance(issuer, str)
                or issuer not in issuers
                or not isinstance(subject, str)
                or not 1 <= len(subject) <= 256
                or type(item["enabled"]) is not bool
            ):
                raise ValueError("Invalid membership identity")
            role = Role(item["role"])
            # Reuse Principal's stable internal identifier validation.
            Principal(item["user_id"], item["organization_id"], role, LoginSource.EXTERNAL_IDP)
            key = (issuer, subject)
            if key in records:
                raise ValueError("Duplicate membership")
            records[key] = (item["user_id"], item["organization_id"], role) if item["enabled"] else None
        return cls({key: value for key, value in records.items() if value is not None})

    def resolve(self, issuer: str, subject: str, source: LoginSource) -> Principal | None:
        membership = self.records.get((issuer, subject))
        if membership is None:
            return None
        return Principal(*membership, source)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch_alb_public_key(region: str, key_id: str) -> bytes:
    """Fetch only from the AWS regional ALB key endpoint, never a token-supplied URL."""
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region) or not _KEY_ID.fullmatch(key_id):
        raise ValueError("Invalid ALB key location")
    url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{key_id}"
    with urllib.request.build_opener(_NoRedirect()).open(url, timeout=3) as response:
        pem = response.read(16_385)
    if len(pem) > 16_384:
        raise ValueError("ALB public key is too large")
    return pem


class AlbRequestAuthenticator:
    def __init__(
        self,
        trusts: tuple[AlbTrust, ...],
        memberships: Memberships,
        key_loader: Callable[[str, str], bytes] = fetch_alb_public_key,
        clock: Callable[[], float] = time.time,
    ):
        if not trusts or len({(item.signer_arn, item.issuer, item.client_id) for item in trusts}) != len(
            trusts
        ):
            raise ValueError("Distinct ALB trusts are required")
        self.trusts = trusts
        self.memberships = memberships
        self.key_loader = key_loader
        self.clock = clock
        self._keys: dict[tuple[str, str], tuple[ec.EllipticCurvePublicKey, float]] = {}

    @classmethod
    def from_files(cls, trusts_path: Path, memberships_path: Path) -> AlbRequestAuthenticator:
        data = _read_configuration(trusts_path)
        if (
            set(data) != {"version", "trusts"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["trusts"], list)
        ):
            raise ValueError("Invalid ALB trusts file")
        trusts = []
        for item in data["trusts"]:
            if not isinstance(item, dict) or set(item) != {
                "signer_arn",
                "issuer",
                "client_id",
                "login_source",
            }:
                raise ValueError("Invalid ALB trust")
            trusts.append(
                AlbTrust(
                    item["signer_arn"], item["issuer"], item["client_id"], LoginSource(item["login_source"])
                )
            )
        trust_tuple = tuple(trusts)
        memberships = Memberships.from_file(memberships_path, trust_tuple)
        return cls(trust_tuple, memberships)

    def _key(self, region: str, key_id: str) -> ec.EllipticCurvePublicKey:
        identity = (region, key_id)
        cached = self._keys.get(identity)
        now = self.clock()
        if cached and cached[1] > now:
            return cached[0]
        key = serialization.load_pem_public_key(self.key_loader(region, key_id))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise TypeError("ALB key must be P-256")
        self._keys[identity] = (key, now + 300)
        return key

    def authenticate_request(self, headers: Mapping[str, str]) -> Principal | None:
        get_all = getattr(headers, "get_all", None)
        values = get_all("x-amzn-oidc-data") if callable(get_all) else [headers.get("x-amzn-oidc-data")]
        if not isinstance(values, list) or len(values) != 1:
            return None
        token = values[0]
        if not isinstance(token, str) or len(token) > 16_384:
            return None
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            header = _unique_json(_decode(parts[0]))
            if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
                return None
            if not _KEY_ID.fullmatch(header["kid"]):
                return None
            trust = next(
                (
                    item
                    for item in self.trusts
                    if item.signer_arn == header.get("signer")
                    and item.issuer == header.get("iss")
                    and item.client_id == header.get("client")
                ),
                None,
            )
            if trust is None or type(header.get("exp")) is not int or header["exp"] <= self.clock():
                return None
            signature = _decode(parts[2])
            if len(signature) != 64:
                return None
            r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            self._key(trust.region, header["kid"]).verify(
                utils.encode_dss_signature(r, s), f"{parts[0]}.{parts[1]}".encode(), ec.ECDSA(hashes.SHA256())
            )
            claims = _unique_json(_decode(parts[1]))
            subject = claims.get("sub")
            if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
                return None
            return self.memberships.resolve(trust.issuer, subject, trust.login_source)
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            binascii.Error,
            InvalidSignature,
            UnsupportedAlgorithm,
            urllib.error.URLError,
            OSError,
        ):
            return None
