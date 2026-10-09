"""Rehearse B -> A -> B using Sky's existing release API on a dedicated test app."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


class RehearsalError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SkyClient:
    def __init__(self, url, cookie_file=None):
        parsed = urllib.parse.urlsplit(url)
        if (
            (
                parsed.scheme != "https"
                and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            )
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RehearsalError("sky_url_invalid")
        if parsed.path not in {"", "/"}:
            raise RehearsalError("sky_url_invalid")
        self.url = url.rstrip("/")
        self.headers = {}
        if cookie_file:
            cookie = Path(cookie_file).read_text().strip()
            if not cookie or len(cookie) > 65536 or any(char in cookie for char in "\r\n\0"):
                raise RehearsalError("cookie_invalid")
            self.headers["Cookie"] = cookie
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        page = self.request("/", raw=True)
        token = re.search(r"const token='([A-Za-z0-9_-]{32,128})';", page)
        if not token:
            raise RehearsalError("sky_auth_or_token_unavailable")
        self.headers["X-Sky-Token"] = token[1]

    def request(self, path, body=None, *, raw=False):
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path, data=data, headers={**self.headers, "Content-Type": "application/json"}
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                content = response.read(4 * 1024 * 1024 + 1)
                if len(content) > 4 * 1024 * 1024:
                    raise RehearsalError("sky_response_too_large")
                text = content.decode()
                value = text if raw else json.loads(text)
                if not raw and not isinstance(value, dict):
                    raise RehearsalError("sky_response_schema_invalid")
                return value
        except (OSError, ValueError) as error:
            # Never report response bodies, Cookie, tokens or credential-bearing URLs.
            raise RehearsalError("sky_request_failed") from error


def validate_pair(current, previous, application_id, account, region):
    if not re.fullmatch(r"rollback-demo-[a-z0-9-]{1,17}", application_id):
        raise RehearsalError("dedicated_test_application_required")
    if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region):
        raise RehearsalError("account_or_region_invalid")
    for job, state in ((current, "active"), (previous, "superseded")):
        if (
            job.get("status") != "succeeded"
            or job.get("deployment_state") != state
            or job.get("application_id") != application_id
            or job.get("target") != "aws-ecs-express"
            or job.get("release_rollback_state") in {"running", "needs_attention"}
            or not re.fullmatch(r"[a-f0-9]{16}", job.get("id", ""))
        ):
            raise RehearsalError("release_state_or_application_mismatch")
        result = job.get("result")
        if not isinstance(result, dict):
            raise RehearsalError("release_identity_missing")
        if result.get("account") != account or result.get("region") != region:
            raise RehearsalError("release_account_or_region_mismatch")
        if not result.get("task_definition_arn") or not result.get("image"):
            raise RehearsalError("release_identity_missing")
    first, second = current["result"], previous["result"]
    if (
        current["id"] == previous["id"]
        or first["image"] == second["image"]
        or second["image"] not in first.get("images", [])
        or any(
            first.get(key) != second.get(key) or not first.get(key)
            for key in ("service", "service_arn", "url")
        )
    ):
        raise RehearsalError("release_service_or_image_mismatch")


def rehearse(
    client,
    *,
    current_id,
    previous_id,
    application_id,
    account,
    region,
    execute=False,
    confirm_application=None,
    require_websocket=False,
    timeout=1800,
    interval=5,
):
    if not all(re.fullmatch(r"[a-f0-9]{16}", value) for value in (current_id, previous_id)):
        raise RehearsalError("job_id_invalid")
    report = {
        "status": "failed",
        "application_id": application_id,
        "current_job": current_id,
        "previous_job": previous_id,
        "checks": [],
        "restoration": "not_needed",
        "scope": "existing_test_releases_only",
        "websocket_required": require_websocket,
        "started_at": datetime.now(UTC).isoformat(),
    }
    original_image = None
    original_task = None
    attempted = False
    rollback_verified = False

    def job(job_id):
        value = client.request(f"/api/jobs/{job_id}")
        if not isinstance(value, dict) or value.get("id") != job_id:
            raise RehearsalError("job_response_identity_mismatch")
        return value

    def check(job_id, stage):
        started = time.monotonic()
        health = client.request(f"/api/jobs/{job_id}/health")
        if health.get("healthy") is not True:
            raise RehearsalError("deployment_health_failed")
        if require_websocket:
            probe = client.request(f"/api/jobs/{job_id}/websocket-probe", b"")
            if probe.get("status") != "passed" or probe.get("protocol") != "sky.probe.v1":
                raise RehearsalError("websocket_exchange_failed")
        report["checks"].append(
            {
                "stage": stage,
                "http": "passed",
                "websocket": "passed" if require_websocket else "not_requested",
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        )

    def switch(source_id, target_id, stage):
        started = time.monotonic()
        source, target = job(source_id), job(target_id)
        validate_pair(source, target, application_id, account, region)
        expected_image = target["result"]["image"]
        expected_task = target["result"]["task_definition_arn"]
        client.request(f"/api/jobs/{source_id}/rollback-release", {"target_job_id": target_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            source, target = job(source_id), job(target_id)
            if source.get("release_rollback_state") in {"failed", "needs_attention"}:
                raise RehearsalError("rollback_failed_or_needs_attention")
            if (
                source.get("release_rollback_state") == "succeeded"
                and source.get("deployment_state") == "superseded"
                and target.get("deployment_state") == "active"
            ):
                evidence = source.get("release_rollback_verification") or {}
                if (
                    target["result"]["image"] != expected_image
                    or target["result"]["task_definition_arn"] != expected_task
                    or evidence.get("target_job_id") != target_id
                    or evidence.get("image") != target["result"]["image"]
                    or evidence.get("task_definition_arn") != target["result"]["task_definition_arn"]
                    or evidence.get("url") != target["result"]["url"]
                    or not evidence.get("service_deployment_arn")
                    or source.get("release_rollback_restore_pending") is not False
                ):
                    raise RehearsalError("rollback_evidence_missing")
                report["checks"].append(
                    {
                        "stage": stage,
                        "release_transition": "passed",
                        "duration_seconds": round(time.monotonic() - started, 3),
                    }
                )
                return
            time.sleep(interval)
        raise RehearsalError("rollback_timeout")

    try:
        current, previous = job(current_id), job(previous_id)
        validate_pair(current, previous, application_id, account, region)
        original_image = current["result"]["image"]
        original_task = current["result"]["task_definition_arn"]
        if not execute:
            report["status"] = "planned"
            return report
        if confirm_application != application_id:
            raise RehearsalError("explicit_application_confirmation_required")
        check(current_id, "initial_B")
        attempted = True  # Set before POST: a timeout can happen after the server accepted it.
        switch(current_id, previous_id, "rollback_B_to_A")
        check(previous_id, "previous_A")
        rollback_verified = True
    except RehearsalError as error:
        report["error"] = str(error)
    except KeyboardInterrupt:
        report["error"] = "interrupted"
    finally:
        if attempted:
            try:
                current, previous = job(current_id), job(previous_id)
                if (
                    previous.get("deployment_state") == "active"
                    and current.get("deployment_state") == "superseded"
                    and current.get("release_rollback_state") == "succeeded"
                ):
                    switch(previous_id, current_id, "restore_A_to_B")
                elif current.get("deployment_state") != "active" or current.get("release_rollback_state") in {
                    "running",
                    "needs_attention",
                }:
                    raise RehearsalError("restoration_state_unknown")
                restored = job(current_id)
                if (
                    restored["result"]["image"] != original_image
                    or restored["result"]["task_definition_arn"] != original_task
                ):
                    raise RehearsalError("restored_image_mismatch")
                check(current_id, "restored_B")
                report["restoration"] = "verified"
            except RehearsalError as error:
                report["restoration"] = "needs_attention"
                report["restoration_error"] = str(error)
        if rollback_verified and report["restoration"] == "verified" and "error" not in report:
            report["status"] = "passed"
        report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sky-url", "current-job", "previous-job", "application-id", "account", "region", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--cookie-file")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-application")
    parser.add_argument("--require-websocket", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 3600:
        parser.error("timeout must be 30..3600 seconds")
    # Reserve before any network or mutation; never overwrite existing evidence.
    with Path(args.output).open("x", encoding="utf-8") as output:
        try:
            client = SkyClient(args.sky_url, args.cookie_file)
            report = rehearse(
                client,
                current_id=args.current_job,
                previous_id=args.previous_job,
                application_id=args.application_id,
                account=args.account,
                region=args.region,
                execute=args.execute,
                confirm_application=args.confirm_application,
                require_websocket=args.require_websocket,
                timeout=args.timeout,
            )
        except (RehearsalError, OSError, ValueError):
            report = {"status": "failed", "error": "configuration_or_authentication_failed"}
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print("Rollback rehearsal:", report["status"], "— report saved")
    return 0 if report["status"] in {"planned", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
