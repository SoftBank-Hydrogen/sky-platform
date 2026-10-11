"""Private authenticated endpoint for an explicitly instrumented application.

Network placement and TLS termination remain operator requirements. This
handler is intentionally separate from the application's public HTTP handler.
It never logs control bodies/credentials and never invokes a control on redirects.
"""

import json
import secrets
from http.server import BaseHTTPRequestHandler


def application_control_handler(gate, token):
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise ValueError("Registered application control credential required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            body = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(405, {"code": "control_requires_post"})

        def do_POST(self):
            if self.path != "/control":
                return self.reply(404, {"code": "unknown_control_path"})
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return self.reply(401, {"code": "control_authentication_required"})
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                return self.reply(400, {"code": "invalid_control_framing"})
            try:
                length = int(lengths[0])
                if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
                    raise ValueError()
                command = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                return self.reply(400, {"code": "invalid_control_document"})
            try:
                receipt = gate.control(command)
            except Exception:  # noqa: BLE001 -- preserve closed gate, no body/secret/error text exposure
                return self.reply(503, {"code": "control_requires_reconciliation"})
            return self.reply(200, receipt)

    return Handler
