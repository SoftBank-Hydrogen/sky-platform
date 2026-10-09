"""The Compose execution boundary rejects unsupported requests and unowned results."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adapters.local.compose import LocalComposeAdapter
from adapters.local.docker import LocalDockerAdapter
from application.deployment_core import make_plan
from application.execution import ExecutionRequest, ExecutionState, execute
from engine.compatibility import TARGET_CAPABILITIES

ATTEMPT_ID = "a" * 16 + "-a1"


def owned_result() -> dict:
    return {
        "url": "http://127.0.0.1:12345",
        "container": f"sky-{ATTEMPT_ID}",
        "compose_project": f"sky-{ATTEMPT_ID}",
        "image": f"sky/{ATTEMPT_ID}:latest",
        "compose_sha256": "b" * 64,
    }


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        project = Path("tests/fixtures/apps/hello-node").resolve()
        plan = make_plan(project, "start", None, port=8080, target="onprem-compose")
        self.adapter = LocalComposeAdapter(lambda *_: None, Path(self.temporary.name))
        self.request = ExecutionRequest(
            target="onprem-compose",
            project=project,
            plan=plan,
            attempt_id=ATTEMPT_ID,
            environment={"APP_SECRET": "synthetic-private-value"},
        )

    def test_compose_declares_only_implemented_scope(self):
        capabilities = self.adapter.execution_capabilities()
        self.assertEqual(capabilities.target, "onprem-compose")
        self.assertEqual(capabilities.access_modes, frozenset({"loopback"}))
        self.assertEqual(TARGET_CAPABILITIES["onprem-compose"]["access_modes"], ["loopback"])
        self.assertTrue(capabilities.sqlite_volume)
        self.assertFalse(capabilities.postgresql_binding)
        self.assertFalse(capabilities.remote_host)
        self.assertFalse(capabilities.rollback)
        self.assertFalse(capabilities.restart_drill)

    def test_docker_declares_only_implemented_scope(self):
        capabilities = LocalDockerAdapter.execution_capabilities()
        self.assertEqual(capabilities.target, "local-docker")
        self.assertEqual(capabilities.access_modes, frozenset({"loopback"}))
        self.assertTrue(capabilities.sqlite_volume)
        self.assertFalse(capabilities.postgresql_binding)
        self.assertFalse(capabilities.remote_host)
        self.assertFalse(capabilities.rollback)
        self.assertFalse(capabilities.restart_drill)

    def test_docker_result_is_owned_and_unsupported_modes_are_rejected(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        request = replace(self.request, target="local-docker", plan=replace(self.request.plan, target="local-docker"))
        result = {"url": "http://127.0.0.1:12345", "container": f"sky-{ATTEMPT_ID}",
                  "image": f"sky/{ATTEMPT_ID}:latest"}
        with patch.object(adapter, "deploy", return_value=result) as deploy:
            self.assertEqual(execute(request, ExecutionState(adapter)), result)
            for invalid in (replace(request, access_mode="public"), replace(request, postgresql_binding=True),
                            replace(request, remote_host=True)):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    execute(invalid, ExecutionState(adapter))
            self.assertEqual(deploy.call_count, 1)
            for change in ({"url": "http://example.org:12345"}, {"container": "foreign"},
                           {"image": "foreign:latest"}):
                with self.subTest(change=change), patch.object(adapter, "deploy", return_value={**result, **change}):
                    with self.assertRaisesRegex(ValueError, "소유권 또는 루프백"):
                        execute(request, ExecutionState(adapter))

    def test_supported_request_calls_compose_and_preserves_private_environment(self):
        captured = []

        def deploy(_project, _plan, _attempt_id, environment):
            captured.append(dict(environment))
            return owned_result()

        with patch.object(self.adapter, "deploy", side_effect=deploy) as adapter_deploy:
            result = execute(self.request, ExecutionState(self.adapter))
        self.assertEqual(result, owned_result())
        adapter_deploy.assert_called_once()
        self.assertEqual(captured, [{"APP_SECRET": "synthetic-private-value"}])
        self.assertNotIn("synthetic-private-value", repr(self.request))

    def test_rejects_unsupported_features_before_adapter_call(self):
        requests = [
            replace(self.request, access_mode="public"),
            replace(self.request, postgresql_binding=True),
            replace(self.request, remote_host=True),
            replace(self.request, target="aws-ecs-express"),
            replace(self.request, plan=replace(self.request.plan, target="aws-ecs-express")),
        ]
        with patch.object(self.adapter, "deploy") as adapter_deploy:
            for request in requests:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    execute(request, ExecutionState(self.adapter))
            adapter_deploy.assert_not_called()

    def test_rejects_result_with_foreign_container_or_nonlocal_url(self):
        for change in (
            {"url": "https://example.org"},
            {"container": "someone-else"},
            {"compose_sha256": "bad"},
        ):
            with self.subTest(change=change):
                result = {**owned_result(), **change}
                with (
                    patch.object(self.adapter, "deploy", return_value=result),
                    self.assertRaisesRegex(ValueError, "소유권 또는 (루프백|설정 해시)"),
                ):
                    execute(self.request, ExecutionState(self.adapter))

    def test_rejects_wrong_sqlite_volume_binding(self):
        request = replace(
            self.request, sqlite_binding={"volume_name": "sky-data-demo", "mount_path": "/app/data"}
        )
        with (
            patch.object(self.adapter, "deploy", return_value=owned_result()),
            self.assertRaisesRegex(ValueError, "SQLite 볼륨"),
        ):
            execute(request, ExecutionState(self.adapter))


if __name__ == "__main__":
    unittest.main()
