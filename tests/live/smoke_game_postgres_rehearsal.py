"""Rehearse a converted team game against disposable local PostgreSQL.

This checks real migrated rows, HTTP scoreboard, WebSocket scoreboard and restart.
It does not create AWS resources or claim that AI produced the converted source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from adapters.database.migrations import collect_sql_migrations, stage_migrator_context
from adapters.database.sqlite_snapshot import compile_sqlite_snapshot
from application.consistency import check_async_database_callers, check_postgres_node_dependency
from application.deployment_core import extract_project
from tests.live.smoke_sqlite_migration import docker


PASSWORD = "sky-game-rehearsal-only"


def _json(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        if response.status != 200:
            raise AssertionError(f"HTTP {response.status}: {url}")
        result = json.load(response)
    if not isinstance(result, dict):
        raise AssertionError(f"Invalid JSON object: {url}")
    return result


def _wait_for_scoreboard(url: str, expected_rows: int) -> dict:
    last = None
    for _ in range(30):
        try:
            board = _json(url + "/api/scoreboard")
            if board.get("rounds") == expected_rows and len(board.get("recent", [])) >= min(5, expected_rows):
                return board
            last = f"unexpected scoreboard summary: {board.get('rounds')}"
        except (OSError, urllib.error.URLError, ValueError, AssertionError) as exc:
            last = type(exc).__name__
        time.sleep(1)
    raise AssertionError(f"PostgreSQL scoreboard HTTP probe failed: {last}")


def _websocket_scoreboard(container: str, expected_rows: int) -> None:
    script = r"""
const WebSocket = require('ws');
const expected = Number(process.argv[1]);
const ws = new WebSocket('ws://127.0.0.1:' + (process.env.PORT || 8080) + '/ws');
const timeout = setTimeout(() => { console.error('scoreboard timeout'); process.exit(3); }, 8000);
ws.on('open', () => ws.send(JSON.stringify({type: 'join'})));
ws.on('message', raw => {
  let message;
  try { message = JSON.parse(raw.toString()); } catch { return; }
  if (message.type !== 'scoreboard') return;
  if (message.rounds !== expected || !message.wins) {
    console.error('invalid WebSocket scoreboard'); process.exit(2);
  }
  clearTimeout(timeout); ws.close(); process.exit(0);
});
ws.on('error', error => { console.error(error.message); process.exit(4); });
"""
    docker("exec", container, "node", "-e", script, str(expected_rows), timeout=20)


def rehearse(archive: Path, work: Path, postgres_image: str) -> dict:
    if not work.is_dir() or work.is_symlink():
        raise ValueError("변환된 작업용 디렉터리가 필요합니다.")
    suffix = uuid.uuid4().hex[:12]
    network = "sky-game-rehearsal-" + suffix
    database = network + "-db"
    app = network + "-app"
    migrator_image = network + "-migrator"
    app_image = network + "-image"
    created = []
    with tempfile.TemporaryDirectory(prefix="sky-game-pg-rehearsal-") as folder:
        root = Path(folder)
        original = extract_project(archive, root / "source")
        source_database = (original / "data" / "scores.db").read_bytes()
        snapshot = compile_sqlite_snapshot(original / "data" / "scores.db")
        if (work / "data" / "scores.db").exists():
            raise ValueError("변환 작업본에 원본 SQLite 파일이 남아 있습니다.")
        migrations = collect_sql_migrations(work)
        if (len(migrations.migrations) != 1
                or migrations.migrations[0].name != "0000_sky_sqlite_import.sql"
                or migrations.migrations[0].sha256 != hashlib.sha256(snapshot.sql.encode()).hexdigest()):
            raise ValueError("작업본 마이그레이션이 게임 원본 SQLite 데이터와 다릅니다.")
        check_postgres_node_dependency(work)
        check_async_database_callers(original, work)
        stage_migrator_context(migrations, root / "migrator")
        try:
            docker("network", "create", network)
            created.append(("network", network))
            docker("run", "--rm", "-d", "--name", database, "--network", network,
                   "--network-alias", "sky-postgres", "-e", "POSTGRES_PASSWORD=" + PASSWORD,
                   postgres_image, timeout=90)
            created.append(("container", database))
            for _ in range(30):
                ready = subprocess.run(["docker", "exec", database, "pg_isready", "-U", "postgres"],
                                       capture_output=True, timeout=10, check=False)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("PostgreSQL 컨테이너가 준비되지 않았습니다.")
            docker("build", "-t", migrator_image, str(root / "migrator"), timeout=300)
            created.append(("image", migrator_image))
            migrated = docker("run", "--rm", "--network", network,
                              "-e", "PGHOST=sky-postgres", "-e", "PGDATABASE=postgres",
                              "-e", "PGUSER=postgres", "-e", "PGPASSWORD=" + PASSWORD,
                              migrator_image, timeout=90)
            if json.loads(migrated) != {"applied": 1}:
                raise AssertionError("SQLite 스냅샷 마이그레이션을 한 번 적용하지 못했습니다.")
            rows = int(docker("exec", database, "psql", "-X", "-A", "-t", "-U", "postgres",
                              "-c", "SELECT COUNT(*) FROM rounds"))
            expected_rows = snapshot.row_counts["rounds"]
            if rows != expected_rows:
                raise AssertionError("PostgreSQL로 이전된 게임 점수 행 수가 다릅니다.")
            docker("build", "-t", app_image, str(work), timeout=300)
            created.append(("image", app_image))
            docker("run", "-d", "--name", app, "--network", network,
                   "-e", "PGHOST=sky-postgres", "-e", "PGPORT=5432",
                   "-e", "PGDATABASE=postgres", "-e", "PGUSER=postgres",
                   "-e", "PGPASSWORD=" + PASSWORD, "-e", "PGSSLMODE=disable",
                   "-e", "PORT=8080", "-p", "127.0.0.1::8080", app_image)
            created.append(("container", app))
            binding = docker("port", app, "8080/tcp").splitlines()[0]
            url = "http://" + binding
            _wait_for_scoreboard(url, expected_rows)
            _websocket_scoreboard(app, expected_rows)
            docker("restart", app, timeout=90)
            binding = docker("port", app, "8080/tcp").splitlines()[0]
            url = "http://" + binding
            _wait_for_scoreboard(url, expected_rows)
            _websocket_scoreboard(app, expected_rows)
            if (original / "data" / "scores.db").read_bytes() != source_database:
                raise AssertionError("원본 SQLite 파일이 변경됐습니다.")
            return {"migrated_rows": expected_rows, "http": "passed", "websocket": "passed",
                    "restart": "passed", "aws_resources": 0}
        except Exception as exc:
            if ("container", app) in created:
                logs = subprocess.run(["docker", "logs", "--tail", "30", app],
                                      capture_output=True, text=True, timeout=15, check=False)
                detail = (logs.stdout + logs.stderr).replace(PASSWORD, "[REDACTED]")[-1400:]
                raise RuntimeError(f"게임 PostgreSQL 리허설 실패: {exc}; app logs: {detail}") from exc
            raise
        finally:
            for kind, name in reversed(created):
                args = {"container": ("rm", "-f"), "image": ("image", "rm"),
                        "network": ("network", "rm")}[kind]
                subprocess.run(["docker", *args, name], capture_output=True, text=True,
                               timeout=30, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--archive", type=Path, default=Path(__file__).resolve().parents[3]
                        / "demo-game" / "TUG-Sky-almostfinaltest.zip")
    parser.add_argument("--postgres-image", default="postgres:16-alpine")
    args = parser.parse_args()
    print(json.dumps(rehearse(args.archive, args.work, args.postgres_image), ensure_ascii=False))
