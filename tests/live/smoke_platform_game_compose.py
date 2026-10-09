"""Sky intake -> Compose adapter -> HTTP/WebSocket -> retire, with offline agent calls."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
import unittest.mock
import urllib.request
import uuid
from pathlib import Path

from adapters.local.docker import LocalDockerAdapter
from application.analysis import AISettings
from application.certificate import deployment_certificate
from interfaces.http.server import App, handler_for
from tests.live.smoke_platform_game_local import OfflineAgent


def main() -> None:
    application_id = "compose-" + uuid.uuid4().hex[:16]
    archive = Path(__file__).resolve().parents[3] / "demo-game" / "TUG-Sky-almostfinaltest.zip"
    payload = archive.read_bytes()
    with tempfile.TemporaryDirectory(prefix="sky-platform-compose-") as temporary:
        app = App(
            Path(temporary),
            AISettings("offline-fixture", "offline-fixture"),
            agent_factory=lambda _: OfflineAgent(),
            monitor_interval=0,
            github_poll_interval=0,
        )
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = "/api/deployments"
        handler.headers = {
            "X-Sky-Token": app.token,
            "X-Deploy-Target": "onprem-compose",
            "X-Application-Id": application_id,
            "X-Local-Sqlite-Mount": "/app/data",
            "Content-Length": str(len(payload)),
        }
        handler.rfile = io.BytesIO(payload)
        handler.json_response = unittest.mock.Mock()
        with unittest.mock.patch("interfaces.http.server.threading.Thread.start"):
            handler.do_POST()
        status, response = handler.json_response.call_args.args
        if status != 202:
            raise AssertionError((status, response))
        job_id = response["id"]
        current = app
        try:
            app.run_agent(job_id)
            job = app.jobs[job_id]
            if job["status"] != "succeeded":
                raise AssertionError(
                    [
                        (event.get("stage"), event.get("message", "")[:300])
                        for event in job.get("events", [])[-8:]
                    ]
                )
            if not app.check_and_record_health(job_id)["healthy"]:
                raise AssertionError("Compose HTTP health failed")
            if app.check_and_record_websocket(job_id)["status"] != "passed":
                raise AssertionError("Compose WebSocket probe failed")
            with urllib.request.urlopen(job["result"]["url"] + "/api/scoreboard", timeout=5) as response:
                initial = json.load(response)["rounds"]
            if initial != 13:
                raise AssertionError(initial)
            original_url = job["result"]["url"]
            attempt_id = job["result"]["container"].removeprefix("sky-")
            compose_file = app.root / job_id / f"compose-{attempt_id}.json"
            compose = ["docker", "compose", "-p", job["result"]["compose_project"], "-f", str(compose_file)]
            write = (
                "const db=require('./db').openScores();"
                "db.saveRound({startedAt:1,endedAt:2,winner:'A',"
                "taps:{A:3,B:1},players:{A:1,B:1}});db.close()"
            )
            subprocess.run(
                [*compose, "exec", "-T", "app", "node", "-e", write],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([*compose, "restart", "app"], check=True, capture_output=True, text=True)
            for _ in range(30):
                try:
                    with urllib.request.urlopen(
                        job["result"]["url"] + "/api/scoreboard", timeout=2
                    ) as response:
                        after_restart = json.load(response)["rounds"]
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise AssertionError("Compose app was unavailable after restart")
            if after_restart != initial + 1:
                raise AssertionError(f"SQLite score did not survive restart: {after_restart}")
            current = App(
                app.root,
                AISettings("offline-fixture", "offline-fixture"),
                monitor_interval=0,
                github_poll_interval=0,
            )
            job = current.jobs[job_id]
            if job["status"] != "succeeded" or job["result"]["url"] != original_url:
                raise AssertionError("Compose deployment record was not restored")
            if not current.check_and_record_health(job_id)["healthy"]:
                raise AssertionError("Compose HTTP failed after server recovery")
            if current.check_and_record_websocket(job_id)["status"] != "passed":
                raise AssertionError("Compose WebSocket failed after server recovery")
            checks = {item["name"]: item["status"] for item in deployment_certificate(job)["verification"]}
            if checks["local_sqlite_mount"] != "passed" or checks["websocket_round_trip"] != "passed":
                raise AssertionError(checks)
            print(
                json.dumps(
                    {
                        "job_id": job_id,
                        "target": job["target"],
                        "status": job["status"],
                        "seed_rounds": initial,
                        "rounds_after_restart": after_restart,
                        "server_restart_recovered": True,
                        "http": checks["deployment_http"],
                        "websocket": checks["websocket_round_trip"],
                        "sqlite_mount": checks["local_sqlite_mount"],
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            job = current.jobs[job_id]
            if job.get("status") == "succeeded" and job.get("result"):
                current.retire_compose(job_id)
                if current.jobs[job_id]["deployment_state"] != "deleted":
                    raise AssertionError(current.jobs[job_id].get("retire_error"))
            binding = job["local_sqlite_binding"]
            adapter = LocalDockerAdapter(lambda *_: None)
            volume = adapter.inspect_resource("volume", binding["volume_name"])
            if volume and (volume.get("Labels") or {}).get("sky-application") == application_id:
                attached = adapter.command(
                    ["docker", "ps", "-a", "-q", "--filter", "volume=" + binding["volume_name"]], quiet=True
                )
                if not attached:
                    subprocess.run(["docker", "volume", "rm", binding["volume_name"]], check=True)


if __name__ == "__main__":
    main()
