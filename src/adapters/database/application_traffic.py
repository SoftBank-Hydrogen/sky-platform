"""Control protocol for explicitly registered, write-gated applications.

The application must atomically compare bindings/generations, drain *all*
writers and probe its real database. This is not inferred from HTTP health or
an ECS service. Unknown remote effects stop this controller without retries.
"""

import json
import secrets
import ssl
from dataclasses import asdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from domain.database import DatabaseBinding


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RegisteredApplicationTraffic:
    def __init__(
        self, endpoint, token, *, controller_id, source, target, guard, transport=None, ssl_context=None
    ):
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not isinstance(token, str)
            or not token
            or "\r" in token
            or "\n" in token
            or not isinstance(controller_id, str)
            or not controller_id
        ):
            raise ValueError("Pinned HTTPS control endpoint and credentials required")
        left, right = DatabaseBinding(**source), DatabaseBinding(**target)
        if (
            left.role != "shared_workload"
            or right.role != "dedicated_workload"
            or left.instance_id == right.instance_id
            or (left.organization_id, left.application_id, left.account_id, left.region)
            != (right.organization_id, right.application_id, right.account_id, right.region)
        ):
            raise ValueError("Same-application promotion bindings required")
        self.endpoint, self._token, self.controller_id = endpoint, token, controller_id
        self.ssl_context = ssl_context or ssl.create_default_context()
        if self.ssl_context.verify_mode != ssl.CERT_REQUIRED or not self.ssl_context.check_hostname:
            raise ValueError("Control TLS must verify certificate and hostname")
        self.bindings = [asdict(left), asdict(right)]
        self.guard, self.transport = guard, transport or self._post
        self.binding, self.generation, self.probed = None, None, False
        self.uncertain = False

    def _post(self, command):
        request = Request(
            self.endpoint,
            data=json.dumps(command).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        try:
            with build_opener(_NoRedirect(), HTTPSHandler(context=self.ssl_context)).open(
                request, timeout=30
            ) as response:
                if response.status != 200:
                    raise ValueError("Invalid controller response")
                body = response.read(16385)
                if len(body) > 16384:
                    raise ValueError("Controller response too large")
                return json.loads(body)
        except (HTTPError, URLError, OSError, ValueError):
            raise RuntimeError("Application control outcome requires reconciliation") from None

    def _call(self, action, expected, *, target=None):
        if self.uncertain:
            raise RuntimeError("Uncertain application control outcome; reconcile first")
        if expected not in self.bindings or (target is not None and target not in self.bindings):
            raise ValueError("Unregistered application binding")
        nonce = secrets.token_hex(16)
        command = {
            "schema_version": 1,
            "controller_id": self.controller_id,
            "request_id": nonce,
            "action": action,
            "expected_binding": expected,
            "expected_generation": self.generation,
            "target_binding": target,
        }
        self.guard()
        # Mark uncertain before the call: timeout, malformed receipt and lost
        # lease must never be interpreted as absence of an external effect.
        self.uncertain = True
        try:
            receipt = self.transport(command)
            self.guard()
            binding = target if target is not None else expected
            keys = {
                "schema_version",
                "controller_id",
                "request_id",
                "action",
                "binding",
                "generation",
                "writes_blocked",
                "active_writes",
                "database_read_verified",
            }
            if (
                not isinstance(receipt, dict)
                or set(receipt) != keys
                or type(receipt["schema_version"]) is not int
                or receipt["schema_version"] != 1
                or receipt["controller_id"] != self.controller_id
                or receipt["request_id"] != nonce
                or receipt["action"] != action
                or receipt["binding"] != binding
                or type(receipt["generation"]) is not int
                or receipt["generation"] < 1
                or (self.generation is not None and receipt["generation"] <= self.generation)
                or receipt["writes_blocked"] is not (action != "resume")
                or type(receipt["active_writes"]) is not int
                or receipt["active_writes"] != 0
                or type(receipt["database_read_verified"]) is not bool
                or (action == "probe" and receipt["database_read_verified"] is not True)
            ):
                raise ValueError("Application control evidence mismatch; reconcile first")
        except Exception:  # noqa: BLE001 -- do not expose control credentials/response bodies
            raise RuntimeError("Application control outcome requires reconciliation") from None
        self.binding, self.generation = dict(binding), receipt["generation"]
        self.uncertain = False
        return receipt

    def quiesce(self, expected_binding):
        receipt = self._call("quiesce", expected_binding)
        self.probed = False
        return receipt

    def activate(self, expected_binding, target_binding):
        if self.binding != expected_binding or self.generation is None or self.probed:
            raise ValueError("Quiesced matching application required")
        receipt = self._call("activate", expected_binding, target=target_binding)
        self.probed = False
        return receipt

    def probe(self, expected_binding):
        if self.binding != expected_binding or self.generation is None:
            raise ValueError("Activated matching application required")
        receipt = self._call("probe", expected_binding)
        self.probed = True
        return receipt

    def resume(self, expected_binding):
        if self.binding != expected_binding or not self.probed:
            raise ValueError("Verified application read required before enabling writes")
        receipt = self._call("resume", expected_binding)
        self.probed = False
        self.generation = None
        return receipt
