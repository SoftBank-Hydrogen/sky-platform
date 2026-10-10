"""ECS agent task protection; no control-plane credentials or arbitrary endpoints."""
import json
import os
import re
import urllib.request
from adapters.github.remote_build import NoRedirect


class EcsTaskProtection:
    def __init__(self, uri=None):
        self.uri = uri if uri is not None else os.environ.get("ECS_AGENT_URI", "")
        # Fargate supplies a task-specific /api/<task-id>-<runtime-id> base URI.
        # Keep the agent host fixed and reject arbitrary paths, queries and fragments.
        pattern = r"http://169\.254\.170\.2(?::[0-9]{1,5})?(?:/api/[0-9a-f]{32}-[0-9]+)?"
        if not isinstance(self.uri, str) or not re.fullmatch(pattern, self.uri):
            raise ValueError("ECS agent task protection endpoint required")
        self.opener = urllib.request.build_opener(NoRedirect())

    def set(self, enabled):
        request = urllib.request.Request(self.uri+"/task-protection/v1/state", method="PUT",
            data=json.dumps({"ProtectionEnabled": enabled, "ExpiresInMinutes": 5}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=5) as response:
                body = response.read(8193)
                if len(body) > 8192 or json.loads(body).get("protection", {}).get("ProtectionEnabled") is not enabled:
                    raise OSError("Task protection update failed")
        except (OSError, ValueError):
            raise OSError("Task protection unavailable") from None
