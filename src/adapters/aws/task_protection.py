"""ECS agent task protection; no control-plane credentials or arbitrary endpoints."""

import json
import os
import re
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EcsTaskProtection:
    def __init__(self, uri=None):
        self.uri = uri if uri is not None else os.environ.get("ECS_AGENT_URI", "")
        if not re.fullmatch(r"http://169\.254\.170\.2(?::[0-9]{1,5})?", self.uri):
            raise ValueError("ECS agent task protection endpoint required")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def set(self, enabled):
        request = urllib.request.Request(
            self.uri + "/task-protection/v1/state",
            method="PUT",
            data=json.dumps({"ProtectionEnabled": enabled, "ExpiresInMinutes": 5}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                body = response.read(8193)
                if len(body) > 8192:
                    raise OSError("Task protection update failed")
                result = json.loads(body)
                protection = result.get("protection") if isinstance(result, dict) else None
                if not isinstance(protection, dict) or protection.get("ProtectionEnabled") is not enabled:
                    raise OSError("Task protection update failed")
        except (OSError, ValueError):
            raise OSError("Task protection unavailable") from None
