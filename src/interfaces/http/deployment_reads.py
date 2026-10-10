"""Opt-in read-only HTTP boundary; never constructs the legacy mutable App."""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from application.certificate import deployment_certificate
from application.deployment_reads import DeploymentReadService
from assets import ASSET_ROOT
from interfaces.http.auth import LocalTokenAuthenticator
from ports.deployment_reads import ReadCursor


class DatabaseReadApp:
    def __init__(self, service: DeploymentReadService, *, workspace="team", authenticator=None):
        self.service = service
        self.workspace = workspace
        self.token = secrets.token_urlsafe(32)
        self.authenticator = authenticator or LocalTokenAuthenticator(self.token)

    def cursor_token(self, cursor):
        if cursor is None:
            return None
        payload = {**asdict(cursor), "workspace": self.workspace, "v": 1}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

    def cursor(self, token, principal, application_id):
        if token is None:
            return None
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 1024:
                raise ValueError()
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {"organization_id", "application_id", "created_at", "record_id", "workspace", "v"}
                or type(payload["v"]) is not int
                or payload["v"] != 1
                or payload["workspace"] != self.workspace
                or payload["organization_id"] != principal.organization_id
                or payload["application_id"] != application_id
                or not isinstance(payload["created_at"], str)
                or len(payload["created_at"]) > 128
                or not isinstance(payload["record_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", payload["record_id"])
            ):
                raise ValueError()
            return ReadCursor(
                payload["organization_id"],
                payload["application_id"],
                payload["created_at"],
                payload["record_id"],
            )
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError("Invalid page cursor") from None


def handler_for_reads(app: DatabaseReadApp):
    class Handler(BaseHTTPRequestHandler):
        def json_response(self, status, data):
            payload = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def authenticate_api(self):
            self.principal = app.authenticator.authenticate(self.headers.get("X-Sky-Token"))
            if self.principal is None:
                self.json_response(403, {"error": "Invalid session token"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                self.json_response(200, {"status": "ok"})
                return
            if self.path == "/":
                payload = (
                    (ASSET_ROOT / "static/index.html").read_text().replace("__TOKEN__", app.token).encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if not self.authenticate_api():
                return
            if self.path == "/api/config":
                self.json_response(
                    200,
                    {
                        "read_only": True,
                        "ai_available": False,
                        "ai_model": None,
                        "monitor_interval": 0,
                        "github_poll_interval": 0,
                        "targets": [],
                        "recovery_warnings": [],
                    },
                )
                return
            try:
                if len(self.path) > 2048:
                    raise ValueError("Invalid query")
                url = urlsplit(self.path)
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
                release = re.fullmatch(
                    r"/api/applications/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/releases", url.path
                )
                listing = url.path == "/api/jobs" or release is not None
                if listing:
                    if set(query) - {"limit", "cursor"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid query")
                    raw_limit = query.get("limit", ["50"])[0]
                    if not re.fullmatch(r"[0-9]{1,3}", raw_limit) or not 1 <= int(raw_limit) <= 100:
                        raise ValueError("Invalid page limit")
                    application_id = release[1] if release else None
                    options = {
                        "limit": int(raw_limit),
                        "cursor": app.cursor(query.get("cursor", [None])[0], self.principal, application_id),
                    }
                elif query:
                    raise ValueError("Query not supported")
            except ValueError:
                self.json_response(400, {"error": "Invalid deployment read query"})
                return
            try:
                if listing:
                    page = (
                        app.service.releases(self.principal, application_id, **options)
                        if release
                        else app.service.summaries(self.principal, **options)
                    )
                    self.json_response(
                        200, {"items": page.items, "next_cursor": app.cursor_token(page.next_cursor)}
                    )
                    return
                detail = re.fullmatch(r"/api/jobs/([a-f0-9]{16})(?:/(history|certificate))?", url.path)
                if detail:
                    job_id, projection = detail.groups()
                    if projection == "history":
                        result = app.service.history(self.principal, job_id)
                    else:
                        result = app.service.detail(self.principal, job_id)
                        if projection == "certificate":
                            result = deployment_certificate(result, result["health_history"])
                    self.json_response(200, result)
                    return
                self.json_response(404, {"error": "Not found"})
            except FileNotFoundError:
                self.json_response(404, {"error": "Not found"})
            except PermissionError:
                self.json_response(403, {"error": "Deployment read access denied"})
            except OSError:
                self.json_response(503, {"error": "Deployment records temporarily unavailable"})
            except ValueError:
                self.json_response(500, {"error": "Invalid persisted deployment record"})

        def do_POST(self):
            if self.authenticate_api():
                self.json_response(405, {"error": "This workspace is read-only"})
            self.close_connection = True

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    return Handler
