"""Hosted review/consent routes; no provisioning or arbitrary policy upload."""

import re

from interfaces.http.mutation_requests import read_json_object
from interfaces.http.shared_database import handler_for_shared_database


def handler_for_operating_review(app, controller):
    class Handler(handler_for_shared_database(app)):
        def do_GET(self):
            route = re.fullmatch(r"/api/jobs/([a-f0-9]{16})/operating-review", self.path)
            if route:
                if self.authenticate_api():
                    self.respond(lambda: (200, controller.detail(self.principal, route[1])))
                return
            super().do_GET()

        def do_POST(self):
            route = re.fullmatch(r"/api/jobs/([a-f0-9]{16})/operating-review/(review|approve)", self.path)
            if not route:
                super().do_POST()
                return
            self.close_connection = True
            if not self.authenticate_api():
                return
            if self.headers.get_all("Origin") != [app.origin]:
                self.json_response(403, {"error": "Invalid request origin"})
                return
            try:
                body = read_json_object(self)
                expected = set() if route[2] == "review" else {"proposal_id", "confirm_review"}
                if set(body) != expected:
                    raise ValueError()
                if route[2] == "approve" and (
                    body["confirm_review"] is not True or not isinstance(body["proposal_id"], str)
                    or not re.fullmatch(r"OPR-[a-f0-9]{24}", body["proposal_id"])
                ):
                    raise ValueError()
            except (ValueError, UnicodeError, TimeoutError, OSError):
                self.json_response(400, {"error": "Invalid operating review request"})
                return

            def execute():
                proposal = (controller.review(self.principal, route[1]) if route[2] == "review"
                            else controller.approve(self.principal, route[1], body["proposal_id"]))
                return 200, {"proposal": proposal, "execution_enabled": False}

            self.respond(execute)

    return Handler
