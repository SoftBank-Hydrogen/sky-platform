"""ALB health checks must work without exposing authenticated API data."""

import unittest
from unittest.mock import Mock

from interfaces.http.server import handler_for


class ServiceHealthTests(unittest.TestCase):
    def test_health_without_token_does_not_read_jobs_or_configuration(self):
        app = Mock(token="private-session-token")
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = "/health"
        handler.headers = {}
        handler.json_response = Mock()
        handler.do_GET()
        handler.json_response.assert_called_once_with(200, {"status": "ok"})
        app.summaries.assert_not_called()

    def test_jobs_still_require_token(self):
        app = Mock(token="private-session-token")
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = "/api/jobs"
        handler.headers = {}
        handler.json_response = Mock()
        handler.do_GET()
        handler.json_response.assert_called_once_with(403, {"error": "Invalid session token"})
        app.summaries.assert_not_called()
