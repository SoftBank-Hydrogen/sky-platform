"""Manual Docker smoke: real game ZIP, seeded score, write, restart, preserved data."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from adapters.local.docker import LocalDockerAdapter
from application.deployment_core import extract_project, make_plan
from application.infrastructure import inspect_infrastructure, validate_infrastructure
from application.local_sqlite import preflight_local_sqlite
from application.websocket_probe import probe_sky_game


def main() -> None:
    archive = Path(__file__).resolve().parents[3] / "demo-game" / "TUG-Sky-almostfinaltest.zip"
    job_id = uuid.uuid4().hex[:16]
    application_id = "smoke-" + job_id
    result = None
    with tempfile.TemporaryDirectory(prefix="sky-local-sqlite-") as temporary:
        project = extract_project(archive, Path(temporary) / "source")
        profile = inspect_infrastructure(project)
        binding = preflight_local_sqlite(project, profile, application_id, "/app/data")
        validate_infrastructure(profile, "local-docker", local_sqlite=True)
        adapter = LocalDockerAdapter(lambda *_: None, sqlite_binding=binding)
        plan = make_plan(project, "dockerfile", None, port=8080, health_path="/health")
        try:
            result = adapter.deploy(project, plan, job_id + "-a1")
            def scoreboard() -> dict:
                with urllib.request.urlopen(result["url"] + "/api/scoreboard", timeout=5) as response:
                    return json.load(response)
            before = scoreboard()
            initial = before["rounds"]
            assert initial == 13, before
            assert probe_sky_game(result["url"])["status"] == "passed"
            with __import__("unittest").TestCase().assertRaisesRegex(ValueError, "연결한 컨테이너"):
                adapter.prepare_sqlite_volume()
            script = (
                "const db=require('./db').openScores();"
                "db.saveRound({startedAt:1,endedAt:2,winner:'A',"
                "taps:{A:3,B:1},players:{A:1,B:1}});db.close()"
            )
            subprocess.run(["docker", "exec", result["container"], "node", "-e", script], check=True)
            assert scoreboard()["rounds"] == initial + 1
            subprocess.run(["docker", "restart", result["container"]], check=True)
            restarted_binding = adapter.command(
                ["docker", "port", result["container"], "8080/tcp"], quiet=True)
            restart_url = "http://" + restarted_binding.splitlines()[0]
            initial_url = result["url"]
            result["url"] = restart_url
            assert initial_url == restart_url, (initial_url, restart_url)
            last_error = None
            for attempt in range(20):
                try:
                    after_restart = scoreboard()["rounds"]
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.5)
            else:
                ports = subprocess.run(
                    ["docker", "port", result["container"], "8080/tcp"],
                    capture_output=True, text=True, check=False)
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "20", result["container"]],
                    capture_output=True, text=True, check=False)
                raise AssertionError(
                    f"Scoreboard did not recover after restart: {last_error}; "
                    f"initial={initial_url}; ports={ports.stdout.strip()}; logs={logs.stderr[-1000:]}")
            assert after_restart == initial + 1
            adapter.retire(result, job_id)
            result = None
            job_id = uuid.uuid4().hex[:16]
            result = adapter.deploy(project, plan, job_id + "-a1")
            assert scoreboard()["rounds"] == initial + 1
            assert probe_sky_game(result["url"])["status"] == "passed"
            print(json.dumps({"http": "verified", "websocket": "verified", "seed_rows": initial,
                              "after_write_and_restart": initial + 1,
                              "after_new_release": initial + 1,
                              "url_changed_on_restart": initial_url != restart_url,
                              "volume": result["sqlite_volume"]}, ensure_ascii=False))
        finally:
            if result is not None:
                adapter.retire(result, job_id)
            else:
                adapter.cleanup_failure(job_id + "-a1")
            # This volume belongs to this uniquely named smoke run only.
            volume = adapter.inspect_resource("volume", binding["volume_name"])
            if volume is not None and (volume.get("Labels") or {}).get("sky-application") == application_id:
                subprocess.run(["docker", "volume", "rm", binding["volume_name"]], check=True)


if __name__ == "__main__":
    main()
