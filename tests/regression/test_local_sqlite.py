import io
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.local.docker import LocalDockerAdapter
from application.analysis import AISettings
from application.infrastructure import (
    infrastructure_compatibility,
    inspect_infrastructure,
    validate_infrastructure,
)
from application.local_sqlite import preflight_local_sqlite
from interfaces.http.server import App, handler_for


def sqlite_project(root: Path) -> Path:
    project = root / "source"
    (project / "data").mkdir(parents=True)
    (project / "Dockerfile").write_text("FROM node:22-bookworm-slim\nWORKDIR /app\nCOPY . .\n")
    (project / "server.js").write_text('const sqlite = require("node:sqlite");\n')
    with sqlite3.connect(project / "data" / "scores.db") as connection:
        connection.execute("CREATE TABLE scores (id INTEGER PRIMARY KEY, score INTEGER)")
        connection.execute("INSERT INTO scores (score) VALUES (13)")
    return project


def archive(project: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for path in project.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(project))
    return output.getvalue()


class LocalSqliteTests(unittest.TestCase):
    def test_explicit_binding_allows_only_local_and_preserves_default_block(self):
        with tempfile.TemporaryDirectory() as folder:
            project = sqlite_project(Path(folder))
            profile = inspect_infrastructure(project)
            with self.assertRaises(ValueError):
                validate_infrastructure(profile, "local-docker")
            binding = preflight_local_sqlite(project, profile, "demo-app", "/app/data")
            self.assertEqual(binding["volume_name"], "sky-data-demo-app")
            self.assertEqual(binding["source_path"], "data/scores.db")
            validate_infrastructure(profile, "local-docker", local_sqlite=True)
            report = infrastructure_compatibility(profile, "local-docker", local_sqlite=True)
            self.assertTrue(report["compatible"])
            with self.assertRaises(ValueError):
                validate_infrastructure(profile, "cloud-run", local_sqlite=True)

    def test_preflight_rejects_wrong_mount_and_inconsistent_seed(self):
        with tempfile.TemporaryDirectory() as folder:
            project = sqlite_project(Path(folder))
            profile = inspect_infrastructure(project)
            with self.assertRaisesRegex(ValueError, "/app/data"):
                preflight_local_sqlite(project, profile, "demo-app", "/app/other")
            (project / "data" / "scores.db-wal").touch()
            with self.assertRaisesRegex(ValueError, "WAL"):
                preflight_local_sqlite(project, profile, "demo-app", "/app/data")

    def test_volume_owner_and_live_writer_are_checked(self):
        binding = {
            "application_id": "demo-app", "volume_name": "sky-data-demo-app",
            "mount_path": "/app/data",
        }
        adapter = LocalDockerAdapter(lambda *_: None, sqlite_binding=binding)
        owned = {"Name": binding["volume_name"], "Driver": "local", "Labels": {
            "sky-managed": "true", "sky-application": "demo-app", "sky-mount": "/app/data"}}
        with patch.object(adapter, "inspect_resource", return_value=owned), \
                patch.object(adapter, "command", return_value="busy-container"), \
                self.assertRaisesRegex(ValueError, "연결한 컨테이너"):
            adapter.prepare_sqlite_volume()
        with patch.object(adapter, "inspect_resource", return_value={
                **owned, "Labels": {"sky-managed": "true", "sky-application": "other"}}), \
                patch.object(adapter, "command") as command:
            with self.assertRaisesRegex(ValueError, "소유권"):
                adapter.prepare_sqlite_volume()
            command.assert_not_called()

    def test_upload_records_binding_and_blocks_active_second_deploy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = sqlite_project(root)
            payload = archive(source)
            app = App(root / "state", AISettings("fixture", "model"), monitor_interval=0)
            def upload():
                handler = handler_for(app).__new__(handler_for(app))
                handler.path = "/api/deployments"
                handler.headers = {
                    "X-Sky-Token": app.token, "X-Deploy-Target": "local-docker",
                    "X-Application-Id": "demo-app", "X-Local-Sqlite-Mount": "/app/data",
                    "Content-Length": str(len(payload)),
                }
                handler.rfile = io.BytesIO(payload)
                handler.json_response = Mock()
                with patch("interfaces.http.server.threading.Thread.start"):
                    handler.do_POST()
                return handler.json_response.call_args.args
            status, response = upload()
            self.assertEqual(status, 202, response)
            job = app.jobs[response["id"]]
            self.assertEqual(job["local_sqlite_binding"]["mount_path"], "/app/data")
            self.assertTrue(job["infrastructure_plan"]["compatibility"]["compatible"])
            job.update(status="succeeded", deployment_state="active", result={"url": "http://127.0.0.1:1"})
            app.save(job["id"])
            status, response = upload()
            self.assertEqual(status, 400)
            self.assertIn("종료 후 재배포", response["error"])

    def test_preview_applies_confirmed_binding_to_local_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = archive(sqlite_project(root))
            app = App(root / "state", AISettings(), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = "/api/compatibility"
            handler.headers = {
                "X-Sky-Token": app.token, "X-Application-Id": "demo-app",
                "X-Local-Sqlite-Mount": "/app/data", "Content-Length": str(len(payload)),
            }
            handler.rfile = io.BytesIO(payload)
            handler.json_response = Mock()
            handler.do_POST()
            status, response = handler.json_response.call_args.args
            self.assertEqual(status, 200, response)
            self.assertEqual(response["local_sqlite_binding"]["mount_path"], "/app/data")
            reports = {item["target"]: item for item in response["reports"]}
            self.assertTrue(reports["local-docker"]["compatible"])
            self.assertFalse(reports["cloud-run"]["compatible"])
            self.assertFalse(reports["aws-ecs-express"]["compatible"])


if __name__ == "__main__":
    unittest.main()
