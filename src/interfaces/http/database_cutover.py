"""Explicit consent for queued cutover. Never accept runtime controllers or secrets."""

import re

from interfaces.http.mutation_requests import read_json_object
from interfaces.http.operating_review import handler_for_operating_review
from interfaces.http.shared_database import _UUID


def handler_for_database_cutover(app, reviews, controller):
    class Handler(handler_for_operating_review(app, reviews)):
        def do_GET(self):
            route = re.fullmatch(r'/api/jobs/([a-f0-9]{16})/database-cutover(?:/(' + _UUID + r'))?', self.path)
            if route:
                if self.authenticate_api():
                    self.respond(lambda: (200, controller.status(self.principal, route[1], route[2])
                        if route[2] else controller.detail(self.principal, route[1])))
                return
            super().do_GET()

        def do_POST(self):
            route = re.fullmatch(r'/api/jobs/([a-f0-9]{16})/database-cutover', self.path)
            if not route:
                super().do_POST()
                return
            self.close_connection = True
            if not self.authenticate_api():
                return
            if self.headers.get_all('Origin') != [app.origin]:
                self.json_response(403, {'error': 'Invalid request origin'})
                return
            try:
                body = read_json_object(self)
                if (set(body) != {'proposal_id', 'request_key', 'confirm_cutover'}
                        or body['confirm_cutover'] is not True
                        or not isinstance(body['proposal_id'], str)
                        or not re.fullmatch(r'OPR-[a-f0-9]{24}', body['proposal_id'])
                        or not isinstance(body['request_key'], str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', body['request_key'])):
                    raise ValueError()
            except (ValueError, UnicodeError, TimeoutError, OSError):
                self.json_response(400, {'error': 'Invalid cutover consent request'})
                return
            self.respond(lambda: (202, controller.submit(self.principal, route[1],
                proposal_id=body['proposal_id'], request_key=body['request_key'])))

    return Handler
