"""Opt-in DB review and allocation intake; no workload credentials or AWS client."""

import re
from dataclasses import asdict

from assets import ASSET_ROOT
from interfaces.http.deployment_reads import DatabaseReadApp, handler_for_reads
from interfaces.http.mutation_requests import read_json_object, validate_origin
from ports.operations import ApplicationBusy, IdempotencyConflict
from ports.shared_database_reviews import DatabaseReviewUnavailable
from ports.state import RecordConflict

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class SharedDatabaseApp(DatabaseReadApp):
    def __init__(self, reads, reviews, *, authenticator, origin, workspace="team"):
        if authenticator is None:
            raise ValueError("DB allocation intake requires hosted authentication")
        super().__init__(
            reads, workspace=workspace, authenticator=authenticator, readiness=reviews.check_ready
        )
        self.reviews, self.origin = reviews, validate_origin(origin)


def handler_for_shared_database(app: SharedDatabaseApp):
    class Handler(handler_for_reads(app)):
        def do_GET(self):
            if self.path == "/shared-database":
                if not self.authenticate_api():
                    return
                payload = (ASSET_ROOT / "static/shared-database.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/api/config":
                if self.authenticate_api():
                    self.json_response(
                        200,
                        {
                            "read_only": True,
                            "shared_database_review": True,
                            "shared_database_page": "/shared-database",
                            "upload_enabled": False,
                            "targets": [],
                        },
                    )
                return
            route = re.fullmatch(r"/api/shared-database-reviews/(" + _UUID + r")", self.path)
            if route:
                if self.authenticate_api():
                    self.respond(lambda: (200, app.reviews.detail(self.principal, route[1])))
                return
            super().do_GET()

        def respond(self, action):
            try:
                app.readiness()
                status, value = action()
                self.json_response(status, value)
            except PermissionError:
                self.json_response(403, {"error": "Shared database access denied"})
            except FileNotFoundError:
                self.json_response(404, {"error": "Not found"})
            except (
                DatabaseReviewUnavailable,
                ApplicationBusy,
                IdempotencyConflict,
                RecordConflict,
                ValueError,
            ):
                self.json_response(409, {"error": "Database choice unavailable or changed; review it again"})
            except OSError:
                self.json_response(503, {"error": "Shared database intake temporarily unavailable"})

        def do_POST(self):
            self.close_connection = True
            if not self.authenticate_api():
                return
            if self.headers.get_all("Origin") != [app.origin]:
                self.json_response(403, {"error": "Invalid request origin"})
                return
            review = re.fullmatch(r"/api/jobs/([0-9a-f]{16})/shared-database/review", self.path)
            command = re.fullmatch(
                r"/api/shared-database-reviews/(" + _UUID + r")/(submit|revoke)", self.path
            )
            if review is None and command is None:
                self.json_response(404, {"error": "Not found"})
                return
            try:
                body = read_json_object(self)
                expected = (
                    {"connection_limit"}
                    if review
                    else ({"request_key", "confirm_allocation"} if command[2] == "submit" else set())
                )
                if set(body) != expected:
                    raise ValueError()
                if review and (
                    type(body["connection_limit"]) is not int or not 1 <= body["connection_limit"] <= 50
                ):
                    raise ValueError()
                if (
                    command
                    and command[2] == "submit"
                    and (
                        body["confirm_allocation"] is not True
                        or not isinstance(body["request_key"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", body["request_key"])
                    )
                ):
                    raise ValueError()
            except (ValueError, UnicodeError, TimeoutError, OSError):
                self.json_response(400, {"error": "Invalid shared database request"})
                return

            def execute():
                if review:
                    value = asdict(
                        app.reviews.review(
                            self.principal, review[1], connection_limit=body["connection_limit"]
                        )
                    )
                    value["expires_at"] = value["expires_at"].isoformat()
                    return 201, value
                if command[2] == "submit":
                    receipt = app.reviews.submit(self.principal, command[1], body["request_key"])
                    return 202, {**asdict(receipt), "status": "accepted", "deployment_ready": False}
                changed = app.reviews.revoke(self.principal, command[1])
                return 200, {"revoked": True, "changed": changed}

            self.respond(execute)

        def reject_method(self):
            self.close_connection = True
            if self.authenticate_api():
                self.json_response(405, {"error": "Method not allowed"})

        do_PUT = reject_method
        do_PATCH = reject_method
        do_DELETE = reject_method

    return Handler
