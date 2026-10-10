"""Explicitly composed approval submission HTTP boundary; not enabled by runtime.

Approval creation is internal and requires validated source/plan preparation.
Clients submit only a stored approval ID and request key, never approval claims.
"""

import json
import re
from dataclasses import asdict
from urllib.parse import urlsplit

from interfaces.http.deployment_reads import DatabaseReadApp, handler_for_reads
from ports.deployment_approvals import ApprovalUnavailable
from ports.operations import ApplicationBusy, IdempotencyConflict
from ports.state import RecordConflict


class DatabaseApprovalApp(DatabaseReadApp):
    def __init__(self, reads, approvals, *, authenticator, origin, readiness, workspace="team"):
        if authenticator is None or not callable(readiness):
            raise ValueError("Approval submission requires hosted authentication and readiness")
        try:
            url = urlsplit(origin)
            valid = (
                url.scheme in {"https", "http"}
                and url.netloc
                and not url.username
                and not url.password
                and not url.path
                and not url.query
                and not url.fragment
                and origin == f"{url.scheme}://{url.netloc}"
                and url.hostname
                and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
                and url.port != 0
                and not any(ord(char) <= 32 for char in origin)
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Invalid approval application origin")
        super().__init__(reads, workspace=workspace, authenticator=authenticator, readiness=readiness)
        self.approvals = approvals
        self.origin = origin


def handler_for_approvals(app: DatabaseApprovalApp):
    class Handler(handler_for_reads(app)):
        def do_GET(self):
            if self.path in {"/ready", "/api/config"}:
                if self.path == "/ready":
                    try:
                        app.readiness()
                    except (OSError, ValueError):
                        self.json_response(503, {"status": "not_ready"})
                    else:
                        self.json_response(200, {"status": "ready", "mode": "approval_submission"})
                elif self.authenticate_api():
                    self.json_response(
                        200,
                        {
                            "read_only": True,
                            "approval_submission": True,
                            "upload_enabled": False,
                            "targets": [],
                        },
                    )
                return
            super().do_GET()

        def do_POST(self):
            self.close_connection = True
            if not self.authenticate_api():
                return
            if self.headers.get_all("Origin") != [app.origin]:
                self.json_response(403, {"error": "Invalid request origin"})
                return
            route = re.fullmatch(
                r"/api/deployment-approvals/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/(submit|revoke)",
                self.path,
            )
            if route is None:
                self.json_response(404, {"error": "Not found"})
                return
            try:
                if (
                    self.headers.get_all("Content-Type") != ["application/json"]
                    or self.headers.get_all("Transfer-Encoding") is not None
                ):
                    raise ValueError()
                lengths = self.headers.get_all("Content-Length")
                if (
                    lengths is None
                    or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,4}", lengths[0])
                    or not 2 <= int(lengths[0]) <= 1024
                ):
                    raise ValueError()
                self.connection.settimeout(5)
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise ValueError()

                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError()
                        result[key] = value
                    return result

                body = json.loads(raw, object_pairs_hook=unique_object)
                expected = {"request_key"} if route[2] == "submit" else set()
                if not isinstance(body, dict) or set(body) != expected:
                    raise ValueError()
                if route[2] == "submit" and (
                    not isinstance(body["request_key"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", body["request_key"])
                ):
                    raise ValueError()
            except (ValueError, UnicodeError, TimeoutError, OSError):
                self.json_response(400, {"error": "Invalid approval request"})
                return
            try:
                app.readiness()
                if route[2] == "submit":
                    result = app.approvals.submit(self.principal, route[1], body["request_key"])
                    self.json_response(202, asdict(result))
                else:
                    changed = app.approvals.revoke(self.principal, route[1])
                    self.json_response(200, {"revoked": True, "changed": changed})
            except PermissionError:
                self.json_response(403, {"error": "Deployment approval access denied"})
            except FileNotFoundError:
                self.json_response(404, {"error": "Not found"})
            except (ApprovalUnavailable, ApplicationBusy, IdempotencyConflict, RecordConflict):
                self.json_response(409, {"error": "Approval cannot be admitted or changed"})
            except OSError:
                self.json_response(503, {"error": "Deployment admission temporarily unavailable"})
            except ValueError:
                self.json_response(500, {"error": "Invalid stored approval configuration"})

        def reject_method(self):
            self.close_connection = True
            if self.authenticate_api():
                self.json_response(405, {"error": "Method not allowed"})

        do_PUT = reject_method
        do_PATCH = reject_method
        do_DELETE = reject_method

    return Handler
