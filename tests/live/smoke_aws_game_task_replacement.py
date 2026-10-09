"""Opt-in live game WebSocket and ECS task replacement check.

Run against an active disposable `dbdrill-*` Sky job. The default is read-only;
`--apply` stops exactly one verified service task and waits for its replacement.
This checks database-backed scoreboard persistence, not player-session continuity.
The caller must retire the disposable ECS/RDS/network resources afterward.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from adapters.aws.ecs import AwsExpressAdapter, AwsSettings


def scoreboard(url: str, expected_rows: int) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/api/scoreboard")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise AssertionError("Game scoreboard HTTP status differs from 200")
        body = json.load(response)
    if not isinstance(body, dict) or body.get("rounds") != expected_rows:
        raise AssertionError("Game scoreboard row count differs from the expected migration")


def game_websocket_scoreboard(url: str, expected_rows: int) -> None:
    # Joining changes room membership briefly, but sends no tap or score write.
    script = r"""
const endpoint = process.argv[1].replace(/^https:/, 'wss:').replace(/^http:/, 'ws:') + '/ws';
const expected = Number(process.argv[2]);
const ws = new WebSocket(endpoint);
const timer = setTimeout(() => { console.error('game WebSocket timeout'); process.exit(3); }, 15000);
ws.addEventListener('open', () => ws.send(JSON.stringify({type:'join'})));
ws.addEventListener('message', event => {
  let message;
  try { message = JSON.parse(event.data); } catch { return; }
  if (message.type !== 'scoreboard') return;
  if (message.rounds !== expected || !message.wins) {
    console.error('incorrect game scoreboard', message.rounds); process.exit(2);
  }
  clearTimeout(timer); ws.close(); console.log('GAME_WS_SCOREBOARD', message.rounds); process.exit(0);
});
ws.addEventListener('error', () => { console.error('game WebSocket error'); process.exit(4); });
"""
    subprocess.run(["node", "-e", script, url, str(expected_rows)], check=True, timeout=25)


def owned_task(adapter: AwsExpressAdapter, result: dict, job_id: str) -> str:
    attempt = job_id + "-a1"
    service = "sky-" + attempt
    expected_arn = (
        f"arn:aws:ecs:{adapter.settings.region}:{adapter.settings.expected_account}:service/default/{service}"
    )
    if (
        result.get("owner_attempt") != attempt
        or result.get("service") != service
        or result.get("service_arn") != expected_arn
        or result.get("account") != adapter.settings.expected_account
        or result.get("region") != adapter.settings.region
    ):
        raise AssertionError("Stored service owner differs from the requested disposable job")
    adapter.validate_url(result.get("url", ""), service, adapter.settings.region)

    def aws(*args: str) -> dict:
        return json.loads(adapter.aws(list(args), private=True, quiet=True))

    service_data = aws(
        "ecs", "describe-express-gateway-service", "--service-arn", expected_arn, "--include", "TAGS"
    )["service"]
    tags = {item["key"]: item["value"] for item in service_data.get("tags", [])}
    if (
        service_data.get("serviceArn") != expected_arn
        or service_data.get("status", {}).get("statusCode") != "ACTIVE"
        or tags.get("sky-managed") != "true"
        or tags.get("sky-attempt") != attempt
    ):
        raise AssertionError("Active service ownership was not confirmed")
    listing = aws(
        "ecs", "list-tasks", "--cluster", "default", "--service-name", service, "--desired-status", "RUNNING"
    )
    tasks = listing.get("taskArns")
    if listing.get("nextToken") or not isinstance(tasks, list) or len(tasks) != 1:
        raise AssertionError("Expected exactly one running service task")
    described = aws("ecs", "describe-tasks", "--cluster", "default", "--tasks", tasks[0])
    task = described.get("tasks", [{}])[0]
    if (
        described.get("failures")
        or task.get("taskArn") != tasks[0]
        or task.get("group") != "service:" + service
        or task.get("taskDefinitionArn") != result.get("task_definition_arn")
        or task.get("lastStatus") != "RUNNING"
    ):
        raise AssertionError("Running task does not match the verified release")
    return tasks[0]


def replace_owned_task(adapter: AwsExpressAdapter, result: dict, job_id: str, before: str) -> str:
    service = result["service"]

    def aws(*args: str) -> dict:
        return json.loads(adapter.aws(list(args), private=True, quiet=True))

    stopped = aws(
        "ecs",
        "stop-task",
        "--cluster",
        "default",
        "--task",
        before,
        "--reason",
        "Sky disposable game persistence verification",
    )["task"]
    if stopped.get("taskArn") != before:
        raise AssertionError("Task stop response identity differs")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        listing = aws(
            "ecs",
            "list-tasks",
            "--cluster",
            "default",
            "--service-name",
            service,
            "--desired-status",
            "RUNNING",
        )
        if listing.get("nextToken") or not isinstance(listing.get("taskArns"), list):
            raise AssertionError("Replacement task list was incomplete")
        replacements = [item for item in listing["taskArns"] if item != before]
        if len(replacements) == 1:
            described = aws("ecs", "describe-tasks", "--cluster", "default", "--tasks", replacements[0])
            task = described.get("tasks", [{}])[0]
            if (
                not described.get("failures")
                and task.get("lastStatus") == "RUNNING"
                and task.get("taskDefinitionArn") == result["task_definition_arn"]
                and task.get("group") == "service:" + service
            ):
                return replacements[0]
        time.sleep(10)
    raise TimeoutError("Owned service task was not replaced within 15 minutes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-file", required=True, type=Path)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-rounds", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.expected_rounds < 1:
        parser.error("Expected rounds must be positive")
    job = json.loads(args.job_file.read_text())
    job_id = job.get("id")
    if (
        not isinstance(job_id, str)
        or not re.fullmatch(r"[a-f0-9]{16}", job_id)
        or not re.fullmatch(r"dbdrill-[a-f0-9]{8}", job.get("application_id", ""))
        or job.get("status") != "succeeded"
        or job.get("deployment_state", "active") != "active"
        or job.get("target") != "aws-ecs-express"
    ):
        raise ValueError("An active disposable AWS game deployment is required")
    result = job["result"]
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    before = owned_task(adapter, result, job_id)
    url = result["url"]
    scoreboard(url, args.expected_rounds)
    game_websocket_scoreboard(url, args.expected_rounds)
    print(
        "PREFLIGHT",
        json.dumps({"job_id": job_id, "task": before, "rounds": args.expected_rounds}),
        flush=True,
    )
    if not args.apply:
        print("Read-only check passed. --apply is required for task replacement.", flush=True)
        return
    after = replace_owned_task(adapter, result, job_id, before)
    deadline = time.monotonic() + 300
    while True:
        try:
            scoreboard(url, args.expected_rounds)
            game_websocket_scoreboard(url, args.expected_rounds)
            break
        except (OSError, urllib.error.URLError, subprocess.CalledProcessError, AssertionError, TimeoutError):
            if time.monotonic() > deadline:
                raise
            time.sleep(5)
    print(
        "PASS",
        json.dumps(
            {
                "before_task": before,
                "after_task": after,
                "rounds_after_replacement": args.expected_rounds,
                "session_continuity": "unverified",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
