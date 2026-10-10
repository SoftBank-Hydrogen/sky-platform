"""Pinned GitHub workflow dispatch with installation-token auth and bounded reads."""
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from ports.remote_builds import canonical, digest, request_key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubApi:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, path, body=None):
        if not path.startswith("/") or ".." in path or "\\" in path:
            raise ValueError("Invalid GitHub API path")
        request = urllib.request.Request("https://api.github.com" + path,
            data=None if body is None else canonical(body), method=method,
            headers={"Authorization": "Bearer " + self.token(), "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json", "User-Agent": "sky-worker"})
        try:
            with self.opener.open(request, timeout=20) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("GitHub response exceeds limit")
                return json.loads(data) if data else None
        except (urllib.error.URLError, OSError, UnicodeError, json.JSONDecodeError):
            raise OSError("GitHub request failed; outcome may be uncertain") from None


class InstallationToken:
    def __init__(self, app_id, installation_id, private_key, repository):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) for value in (app_id, installation_id)):
            raise ValueError("GitHub App/installation IDs are required")
        self.key = load_pem_private_key(private_key.encode(), password=None)
        if not isinstance(self.key, RSAPrivateKey) or self.key.key_size < 2048:
            raise ValueError("RSA GitHub App key required")
        self.app_id, self.installation_id, self.repository = app_id, installation_id, repository
        self.value, self.until = None, 0

    def jwt(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
        now = int(time.time())
        unsigned = encode(b'{"alg":"RS256","typ":"JWT"}') + "." + encode(canonical({"iat": now-30, "exp": now+540, "iss": self.app_id}))
        return unsigned + "." + encode(self.key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def __call__(self):
        if time.monotonic() >= self.until:
            result = GitHubApi(self.jwt).call("POST", f"/app/installations/{self.installation_id}/access_tokens",
                {"repositories": [self.repository.split('/')[1]], "permissions": {"actions": "write", "contents": "read"}})
            if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
                raise OSError("Installation token unavailable")
            self.value, self.until = result["token"], time.monotonic()+300
        return self.value


class GitHubRemoteBuild:
    def __init__(self, settings, api):
        self.settings, self.api = settings, api
        self.base = "/repos/" + settings.repository

    def dispatch(self, request):
        commit = self.api.call("GET", self.base + "/commits/" + urllib.parse.quote(self.settings.ref, safe=""))
        if commit.get("sha") != self.settings.workflow_sha:
            raise ValueError("Builder ref moved away from the pinned commit")
        self.api.call("POST", self.base + "/actions/workflows/" + self.settings.workflow + "/dispatches",
            {"ref": self.settings.ref, "inputs": {"build_id": request["build_id"],
                "request_key": request_key(request), "request_digest": digest(request)}})

    def observe(self, request):
        runs = self.api.call("GET", self.base + "/actions/workflows/" + self.settings.workflow + "/runs?event=workflow_dispatch&per_page=100")
        candidates = [run for run in runs.get("workflow_runs", []) if run.get("display_title") == "sky-build:"+request["build_id"]]
        if len(candidates) > 1:
            raise ValueError("Multiple workflow executions require explicit reconciliation")
        if not candidates:
            return None
        run = candidates[0]
        # Never download or accept output from a different ref, commit or repository.
        repository = run.get("repository", {}).get("full_name")
        if (run.get("head_sha"), run.get("head_branch"), run.get("event"), repository) != (
            self.settings.workflow_sha, self.settings.ref, "workflow_dispatch", self.settings.repository
        ) or type(run.get("id")) is not int:
            raise ValueError("Workflow execution provenance mismatch")
        if run.get("status") != "completed":
            return None
        if run.get("conclusion") != "success":
            return {"run_id": run["id"], "succeeded": False}
        return {"run_id": run["id"], "succeeded": True}
