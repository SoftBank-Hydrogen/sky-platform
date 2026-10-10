"""Static releases stay distinct from container jobs across restart and cleanup."""

import io
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.aws.ecs import AwsSettings
from application.analysis import AISettings
from application.certificate import deployment_certificate
from application.deployment_core import source_digest
from interfaces.http.server import App, handler_for


class StaticDeploymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = AwsSettings("ap-northeast-2", expected_account="123456789012")
        self.app = App(self.root, AISettings("", ""), aws_settings=self.settings, monitor_interval=0)

    def request(self, path, body=b"", public="true", target=None):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = path
        handler.headers = {
            "X-Sky-Token": self.app.token,
            "X-Application-Id": "hello-site",
            "X-Public-Access": public,
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)
        if target is not None:
            handler.headers["X-Deploy-Target"] = target
        handler.json_response = Mock()
        handler.do_POST()
        return handler.json_response.call_args.args

    @staticmethod
    def archive(files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, content in files.items():
                bundle.writestr(name, content)
        return buffer.getvalue()

    def create_job(self):
        archive = self.archive({"index.html": "<h1>Hello</h1>"})

        def preflight(_adapter, project, application_id, attempt_id):
            return {"source_digest": source_digest(project), "attempt_id": attempt_id}

        with patch("interfaces.http.server.AwsStaticSiteAdapter.unavailable_reason", return_value=None), \
                patch("interfaces.http.server.AwsStaticSiteAdapter.preflight", preflight), \
                patch.object(self.app, "start_job_worker", return_value=True):
            status, response = self.request("/api/static-deployments", archive)
        self.assertEqual(status, 202)
        return response["id"]

    def test_static_upload_is_persisted_and_survives_restart(self):
        job_id = self.create_job()
        job = self.app.jobs[job_id]
        self.assertEqual(job["target"], "aws-s3-cloudfront")
        self.assertEqual(job["mode"], "static_site")
        self.assertIsNone(job["plan"])
        self.assertEqual(job["architecture_decision"]["selected_candidate"], "aws-s3-cloudfront")
        self.assertEqual(job["compilation"]["deployment_ir"]["services"],
                         [{"id": "source-bundle", "kind": "static_site"}])
        certificate = deployment_certificate(job)
        self.assertEqual(certificate["decision_trace"]["status"], "recorded")
        self.assertEqual(certificate["compilation_status"], "recorded")
        restored = App(self.root, AISettings("", ""), aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.jobs[job_id]["status"], "interrupted")
        self.assertEqual(restored.jobs[job_id]["target"], "aws-s3-cloudfront")

    def test_public_auto_upload_selects_static_without_ai_planner(self):
        self.app.ai_settings = AISettings("fixture-key", "fixture-model")

        def preflight(_adapter, project, _application_id, _attempt_id):
            return {"source_digest": source_digest(project)}

        with patch("interfaces.http.server.AwsStaticSiteAdapter.unavailable_reason", return_value=None), \
                patch("application.static_deployments.AwsStaticSiteAdapter.preflight", preflight), \
                patch("interfaces.http.server.plan_infrastructure") as ai_planner, \
                patch.object(self.app, "start_job_worker", return_value=True):
            status, response = self.request(
                "/api/deployments", self.archive({"index.html": "<h1>Hello</h1>"}), target="auto"
            )
        self.assertEqual(status, 202)
        ai_planner.assert_not_called()
        job = self.app.jobs[response["id"]]
        self.assertEqual(job["target"], "aws-s3-cloudfront")
        self.assertEqual(job["requested_target"], "auto")
        self.assertEqual(job["architecture_decision"]["selection_mode"], "auto_target")
        self.assertEqual(job["infrastructure_plan"]["planner"], "static-source-rule")
        self.assertEqual(deployment_certificate(job)["compilation_status"], "recorded")

    def test_private_or_nonstatic_auto_upload_stays_in_existing_planner(self):
        self.app.ai_settings = AISettings("fixture-key", "fixture-model")
        cases = (
            ({"index.html": "<h1>Hello</h1>"}, "false"),
            ({"index.html": "<div id='root'></div>",
              "package.json": '{"scripts":{"build":"vite build"}}'}, "true"),
            ({"index.html": "<h1>Hello</h1>", "server.js": "require('node:http')"}, "true"),
        )
        for files, public in cases:
            with self.subTest(files=files, public=public), \
                    patch("interfaces.http.server.AwsStaticSiteAdapter.unavailable_reason", return_value=None), \
                    patch("interfaces.http.server.plan_infrastructure", side_effect=ValueError("planner reached")) \
                    as planner:
                status, response = self.request(
                    "/api/deployments", self.archive(files), public=public, target="auto"
                )
                self.assertEqual(status, 400)
                self.assertIn("planner reached", response["error"])
                planner.assert_called_once()
                self.assertFalse(self.app.jobs)

    def test_changed_compilation_is_rejected_before_cloud_adapter(self):
        job_id = self.create_job()
        self.app.jobs[job_id]["compilation"]["target_plan"]["execution_configuration"][
            "asset_source"] = "different-source"
        with patch.object(self.app, "static_adapter") as adapter:
            self.app.run_static_site(job_id)
        adapter.assert_not_called()
        self.assertEqual(self.app.jobs[job_id]["status"], "failed")
        self.assertEqual(self.app.jobs[job_id]["deployment_state"], "deleted")
        certificate = deployment_certificate(self.app.jobs[job_id])
        self.assertEqual(certificate["compilation_status"], "incomplete")

    def test_historical_static_record_can_still_run(self):
        job_id = self.create_job()
        for field in ("application_ir", "deployment_policy", "architecture_decision", "compilation"):
            self.app.jobs[job_id].pop(field)
        with patch.object(self.app, "static_adapter") as adapter:
            adapter.return_value.deploy.return_value = {
                "url": "https://example.invalid", "source_digest": self.app.jobs[job_id]["source_digest"]
            }
            self.app.run_static_site(job_id)
        adapter.return_value.deploy.assert_called_once()
        self.assertEqual(self.app.jobs[job_id]["status"], "succeeded")
        certificate = deployment_certificate(self.app.jobs[job_id])
        self.assertEqual(certificate["compilation_status"], "unrecorded")

    def test_failed_after_stack_creation_remains_recoverable(self):
        job_id = self.create_job()

        def adapter(_job, checkpoint=None):
            class Fake:
                def deploy(self, *_args):
                    checkpoint(static_stack_name="sky-static-owned")
                    checkpoint(static_stack_id="owned-stack")
                    raise RuntimeError("upload failed")

                def retire(self, *_args):
                    return {"status": "deleted"}

            return Fake()

        with patch.object(self.app, "static_adapter", side_effect=adapter):
            self.app.run_static_site(job_id)
            self.assertEqual(self.app.jobs[job_id]["status"], "failed")
            self.assertEqual(self.app.jobs[job_id]["deployment_state"], "needs_attention")
            with patch("interfaces.http.server.threading.Thread.start"):
                status, _response = self.request(f"/api/jobs/{job_id}/retire")
            self.assertEqual(status, 202)
            self.app.retire_static_site(job_id)
        self.assertEqual(self.app.jobs[job_id]["deployment_state"], "deleted")

    def test_manual_retirement_waits_for_same_app_static_deployment(self):
        old_id = self.create_job()
        old = self.app.jobs[old_id]
        old.update(status="succeeded", static_stack_id="old-stack", result={"url": "https://old.invalid"})
        self.app.save(old_id)
        new_id = "b" * 16
        self.app.jobs[new_id] = {
            "id": new_id, "application_id": "hello-site", "target": "aws-s3-cloudfront",
            "status": "running", "deployment_state": "active",
        }
        with patch("interfaces.http.server.threading.Thread.start") as worker:
            status, response = self.request(f"/api/jobs/{old_id}/retire")
        self.assertEqual(status, 409)
        self.assertIn("종료할 수 있는 배포", response["error"])
        self.assertEqual(old["deployment_state"], "active")
        worker.assert_not_called()

    def test_simultaneous_static_requests_reserve_only_one_job(self):
        project = self.root / "site"
        project.mkdir()
        (project / "index.html").write_text("<h1>Hello</h1>")
        barrier = threading.Barrier(2)

        def reserve(job_id):
            (self.app.root / job_id).mkdir()
            barrier.wait(timeout=5)
            try:
                self.app.create_static_job(job_id, project, "hello-site", requested_target="auto")
                return "reserved"
            except ValueError:
                return "rejected"

        with patch("adapters.aws.static_site.AwsStaticSiteAdapter._identity", return_value=None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(reserve, ("a" * 16, "b" * 16)))
        self.assertCountEqual(results, ("reserved", "rejected"))
        self.assertEqual(sum(job.get("mode") == "static_site" for job in self.app.jobs.values()), 1)

    def test_mixed_app_rejected_before_job_or_cloud_creation(self):
        archive = self.archive({"index.html": "Hello", "server.js": "require('http')"})
        with patch("interfaces.http.server.AwsStaticSiteAdapter.unavailable_reason", return_value=None), \
                patch("interfaces.http.server.AwsStaticSiteAdapter._identity", return_value=None):
            status, response = self.request("/api/static-deployments", archive)
        self.assertEqual(status, 400)
        self.assertIn("정적 파일만으로", response["error"])
        self.assertFalse(self.app.jobs)

    def test_uncertain_create_can_be_reconciled_without_redeploy(self):
        job_id = self.create_job()

        def adapter(_job, checkpoint=None):
            class Fake:
                def deploy(self, *_args):
                    checkpoint(static_stack_name="sky-static-" + job_id + "-a1")
                    raise RuntimeError("create request timed out")

                def reconcile(self, *_args):
                    return {"stack_id": "owned-stack", "stack_status": "CREATE_COMPLETE"}

            return Fake()

        with patch.object(self.app, "static_adapter", side_effect=adapter):
            self.app.run_static_site(job_id)
            self.assertEqual(self.app.jobs[job_id]["deployment_state"], "needs_attention")
            status, response = self.request(f"/api/jobs/{job_id}/reconcile")
        self.assertEqual(status, 200)
        self.assertEqual(response["stack_id"], "owned-stack")
        self.assertEqual(self.app.jobs[job_id]["static_stack_id"], "owned-stack")

    def test_certificate_does_not_infer_http_from_missing_container_plan(self):
        job_id = self.create_job()
        report = deployment_certificate(self.app.jobs[job_id])
        self.assertEqual(report["verification"][0]["status"], "unverified")
        self.assertIn("deployment_http", report["unverified"])


if __name__ == "__main__":
    unittest.main()
