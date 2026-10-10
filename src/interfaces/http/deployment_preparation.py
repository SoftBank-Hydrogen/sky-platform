"""Explicit upload/preview/approval composition; default runtime stays read-only."""

import json
import re
import threading
from urllib.parse import urlsplit

from assets import ASSET_ROOT
from domain.access import Action, ResourceOwner, permitted
from interfaces.http.deployment_approvals import DatabaseApprovalApp, handler_for_approvals
from ports.deployment_previews import PreviewBusy, PreviewUnavailable
from ports.operations import IdempotencyConflict


class DatabasePreparationApp(DatabaseApprovalApp):
    def __init__(self, reads, approvals, preparation, *, authenticator, origin, workspace="team"):
        super().__init__(
            reads,
            approvals,
            authenticator=authenticator,
            origin=origin,
            readiness=preparation.previews.check_ready,
            workspace=workspace,
        )
        self.preparation = preparation
        self.upload_slot = threading.BoundedSemaphore(1)


def handler_for_preparation(app):
    class Handler(handler_for_approvals(app)):
        def do_GET(self):
            if self.path == "/api/config":
                if self.authenticate_api():
                    self.json_response(
                        200,
                        {
                            "read_only": False,
                            "approval_submission": True,
                            "upload_enabled": True,
                            "preparation_mode": "static",
                            "deployment_consumer_enabled": False,
                            "targets": ["aws"],
                        },
                    )
                return
            if self.path == "/ready":
                try:
                    app.readiness()
                except (OSError, ValueError):
                    self.json_response(503, {"status": "not_ready"})
                else:
                    self.json_response(200, {"status": "ready", "mode": "deployment_preparation"})
                return
            url = urlsplit(self.path)
            if url.path == "/prepare":
                if not self.authenticate_api():
                    return
                payload = (ASSET_ROOT / "static/deployment-preparation.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            match = re.fullmatch(r"/api/deployment-previews/([0-9a-f-]{36})", self.path)
            if match:
                if not self.authenticate_api():
                    return
                try:
                    self.json_response(200, app.preparation.detail(self.principal, match[1]))
                except PermissionError:
                    self.json_response(403, {"error": "Deployment preview access denied"})
                except FileNotFoundError:
                    self.json_response(404, {"error": "Not found"})
                except OSError:
                    self.json_response(503, {"error": "Deployment previews temporarily unavailable"})
                except ValueError:
                    self.json_response(400, {"error": "Invalid preview identity"})
                return
            super().do_GET()

        def _header(self, name):
            values = self.headers.get_all(name)
            if values is None or len(values) != 1:
                raise ValueError("Invalid request header")
            return values[0]

        def _body(self, limit, content_type):
            if (
                self._header("Content-Type") != content_type
                or self.headers.get_all("Transfer-Encoding") is not None
            ):
                raise ValueError("Invalid body encoding")
            length = self._header("Content-Length")
            if not re.fullmatch(r"[0-9]{1,8}", length) or not 0 < int(length) <= limit:
                raise ValueError("Invalid body size")
            self.connection.settimeout(30)
            data = self.rfile.read(int(length))
            if len(data) != int(length):
                raise ValueError("Incomplete body")
            return data

        def do_POST(self):
            approval = re.fullmatch(r"/api/deployment-previews/([0-9a-f-]{36})/approve", self.path)
            if self.path != "/api/deployment-previews" and not approval:
                super().do_POST()
                return
            self.close_connection = True
            if not self.authenticate_api():
                return
            if not permitted(
                self.principal,
                Action.DEPLOY,
                ResourceOwner(self.principal.organization_id, self.principal.user_id),
            ):
                self.json_response(403, {"error": "Deployment preparation access denied"})
                return
            if self.headers.get_all("Origin") != [app.origin]:
                self.json_response(403, {"error": "Invalid request origin"})
                return
            if not app.upload_slot.acquire(blocking=False):
                self.json_response(429, {"error": "Preparation is busy; retry the same request later"})
                return
            try:
                try:
                    if approval:

                        def unique(pairs):
                            output = {}
                            for key, value in pairs:
                                if key in output:
                                    raise ValueError("Duplicate field")
                                output[key] = value
                            return output

                        body = json.loads(self._body(1024, "application/json"), object_pairs_hook=unique)
                        if (
                            not isinstance(body, dict)
                            or set(body) != {"preview_digest"}
                            or not isinstance(body["preview_digest"], str)
                            or not re.fullmatch(r"[0-9a-f]{64}", body["preview_digest"])
                        ):
                            raise ValueError("Invalid preview approval")
                    else:
                        application_id = self._header("X-Application-Id")
                        request_key = self._header("Idempotency-Key")
                        if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", application_id) or not re.fullmatch(
                            r"[A-Za-z0-9_.:-]{1,128}", request_key
                        ):
                            raise ValueError("Invalid upload identity")
                        port = self._header("X-Container-Port")
                        health = self._header("X-Health-Path")
                        if not re.fullmatch(r"[0-9]{4,5}", port):
                            raise ValueError("Invalid port")
                        body = self._body(20 * 1024 * 1024, "application/zip")
                except (ValueError, UnicodeError, OSError):
                    self.json_response(400, {"error": "Invalid preparation request"})
                    return
                try:
                    app.readiness()
                    if approval:
                        result = app.preparation.approve(self.principal, approval[1], body["preview_digest"])
                        self.json_response(
                            200, {"id": result.id, "expires_at": result.expires_at.isoformat()}
                        )
                    else:
                        result = app.preparation.prepare(
                            self.principal,
                            application_id,
                            request_key,
                            body,
                            port=int(port),
                            health_path=health,
                        )
                        self.json_response(200, result)
                except PermissionError:
                    self.json_response(403, {"error": "Deployment preparation access denied"})
                except FileNotFoundError:
                    self.json_response(404, {"error": "Not found"})
                except (PreviewBusy, PreviewUnavailable, IdempotencyConflict):
                    self.json_response(
                        409, {"error": "Preview is busy, changed, expired or requires preparation"}
                    )
                except OSError:
                    self.json_response(
                        503, {"error": "Preparation temporarily unavailable; retry the same request"}
                    )
                except ValueError:
                    self.json_response(400, {"error": "ZIP or deployment settings cannot be prepared"})
            finally:
                app.upload_slot.release()

    return Handler
