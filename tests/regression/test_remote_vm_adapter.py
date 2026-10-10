import io
import json
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from adapters.onprem.vm import RemoteVmComposeAdapter, VmSettings
from application.analysis import AISettings
from application.health import check_deployment
from interfaces.http.server import App, handler_for


class RemoteVmAdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = VmSettings("vm.example.test", "sky", "app.example.test")

    def test_vm_settings_reject_unsafe_ssh_targets(self):
        for host in ("", "127.0.0.1", "vm;touch /tmp/unsafe", "-bad.example", "vm..example"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                VmSettings(host, "sky", "app.example.test").validate()
        with self.assertRaises(ValueError):
            VmSettings("vm.example.test", "root;id", "app.example.test").validate()

    def test_remote_compose_uses_vm_port_and_keeps_values_out_of_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = RemoteVmComposeAdapter(lambda *_: None, Path(temporary), self.settings)
            attempt = "a" * 16 + "-a1"
            adapter._write_compose(attempt, f"sky/{attempt}:latest", SimpleNamespace(port=8080),
                                   adapter._host_port(), {"PRIVATE": "synthetic-secret"})
            service = json.loads(adapter.compose_file.read_text())["services"]["app"]
            self.assertEqual(service["ports"], ["0.0.0.0::8080"])
            self.assertEqual(service["environment"]["PRIVATE"],
                             "${" + adapter._compose_variable("PRIVATE") + ":?}")
            self.assertNotIn("synthetic-secret", adapter.compose_file.read_text())
            with patch.object(adapter, "command", return_value="0.0.0.0:43123\n[::]:43123"):
                self.assertEqual(adapter._published_url(attempt, 8080, None),
                                 "http://app.example.test:43123")

    def test_remote_commands_pin_docker_host_even_if_context_is_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = RemoteVmComposeAdapter(lambda *_: None, Path(temporary), self.settings)
            with patch("adapters.onprem.vm.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
                with patch.dict("os.environ", {"DOCKER_CONTEXT": "other-host"}):
                    self.assertEqual(adapter.command(["docker", "info"]), "ok")
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["DOCKER_HOST"], "ssh://sky@vm.example.test")
                self.assertNotIn("DOCKER_CONTEXT", env)

    def test_remote_compose_uses_same_pinned_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = RemoteVmComposeAdapter(lambda *_: None, Path(temporary), self.settings)
            attempt = "a" * 16 + "-a1"
            adapter._write_compose(attempt, f"sky/{attempt}:latest", SimpleNamespace(port=8080),
                                   None, {})
            with patch("adapters.local.compose.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                with patch.dict("os.environ", {"DOCKER_CONTEXT": "other-host"}):
                    adapter._compose(attempt, "config", "-q", environment={"DOCKER_HOST": "ssh://other@evil.test"})
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["DOCKER_HOST"], "ssh://sky@vm.example.test")
                self.assertNotIn("DOCKER_CONTEXT", env)
                self.assertEqual(env[adapter._compose_variable("DOCKER_HOST")],
                                 "ssh://other@evil.test")

    def test_deploy_returns_vm_url_only_after_http_200_and_owner_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = RemoteVmComposeAdapter(lambda *_: None, Path(temporary), self.settings)
            attempt = "a" * 16 + "-a1"
            image = f"sky/{attempt}:latest"
            name = f"sky-{attempt}"
            container = {"Config": {"Labels": {"app": "sky", "sky-attempt": attempt,
                                                "com.docker.compose.project": name}, "Image": image}}
            plan = SimpleNamespace(source_digest="abc", required_env=[], port=8080, health_path="/")
            response = SimpleNamespace(status=200)
            class Opener:
                def open(self, url, timeout):
                    self_url = f"http://app.example.test:43123/"
                    assert url == self_url
                    return nullcontext(response)
            with (patch("adapters.local.compose.source_digest", return_value="abc"),
                  patch("adapters.local.compose.ImageBuilder.build"),
                  patch.object(adapter, "inspect_resource", side_effect=[None, None, container]),
                  patch.object(adapter, "_compose"),
                  patch.object(adapter, "command", return_value="0.0.0.0:43123"),
                  patch("adapters.local.compose.urllib.request.build_opener", return_value=Opener())):
                result = adapter.deploy(Path(temporary), plan, attempt)
            self.assertEqual(result["url"], "http://app.example.test:43123")
            self.assertEqual(result["vm_ssh_host"], "vm.example.test")
            self.assertEqual(result["compose_project"], name)

    def test_retire_rejects_changed_vm_identity_before_docker_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = RemoteVmComposeAdapter(lambda *_: None, Path(temporary), self.settings)
            with patch.object(adapter, "inspect_resource") as inspect:
                with self.assertRaisesRegex(ValueError, "VM"):
                    adapter.retire({"vm_ssh_host": "another.example.test"}, "a" * 16)
                inspect.assert_not_called()

    def test_upload_records_explicit_vm_target_and_requires_network_permission(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("package.json", '{"scripts":{"start":"node server.js"}}')
            bundle.writestr("server.js", 'require("node:http").createServer((q,r)=>r.end("ok"))')
        content = archive.getvalue()
        with tempfile.TemporaryDirectory() as temporary:
            app = App(Path(temporary), AISettings("fixture-key", "fixture-model"), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = "/api/deployments"
            handler.json_response = Mock()
            handler.headers = {"X-Sky-Token": app.token, "X-Deploy-Target": "onprem-vm",
                               "X-Public-Access": "false", "Content-Length": str(len(content))}
            handler.rfile = io.BytesIO(content)
            with (patch.object(RemoteVmComposeAdapter, "unavailable_reason", return_value=None),
                  patch.object(VmSettings, "from_environment", return_value=self.settings)):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertFalse(app.jobs)
            handler.json_response.reset_mock()
            handler.headers["X-Public-Access"] = "true"
            handler.rfile = io.BytesIO(content)
            with (patch.object(RemoteVmComposeAdapter, "unavailable_reason", return_value=None),
                  patch.object(VmSettings, "from_environment", return_value=self.settings),
                  patch("interfaces.http.server.threading.Thread")):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 202)
            job = next(iter(app.jobs.values()))
            self.assertEqual(job["target"], "onprem-vm")
            self.assertEqual(job["vm"]["ssh_host"], "vm.example.test")
            self.assertEqual(job["infrastructure_plan"]["compatibility"]["access_mode"], "public")
            self.assertEqual(job["compilation"]["target_plan"]["execution_configuration"]["database_mode"], "none")

    def test_health_inspects_persisted_vm_and_pinned_docker_host(self):
        attempt = "a" * 16 + "-a1"
        name = f"sky-{attempt}"
        image = f"sky/{attempt}:latest"
        job = {"id": "a" * 16, "status": "succeeded", "target": "onprem-vm",
               "vm": {"ssh_host": "vm.example.test", "ssh_user": "sky",
                      "public_host": "app.example.test"},
               "result": {"container": name, "image": image, "compose_project": name,
                          "vm_ssh_host": "vm.example.test", "vm_ssh_user": "sky",
                          "vm_public_host": "app.example.test",
                          "url": "http://app.example.test:43123"},
               "plan": {"health_path": "/"}}
        inspect = [{"State": {"Running": True},
                    "Config": {"Labels": {"app": "sky", "com.docker.compose.project": name},
                               "Image": image}}]
        with (patch("application.health.subprocess.run") as run,
              patch("application.health.probe", return_value=True),
              patch.dict("os.environ", {"DOCKER_CONTEXT": "other-host"})):
            run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(inspect), stderr="")
            self.assertTrue(check_deployment(job)["healthy"])
            self.assertEqual(run.call_args.kwargs["env"]["DOCKER_HOST"],
                             "ssh://sky@vm.example.test")
            self.assertNotIn("DOCKER_CONTEXT", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
