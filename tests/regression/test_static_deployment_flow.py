"""Static releases stay distinct from container jobs across restart and cleanup."""

import io
import tempfile
import unittest
import zipfile
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

    def request(self, path, body=b"", public="true"):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = path
        handler.headers = {
            "X-Sky-Token": self.app.token,
            "X-Application-Id": "hello-site",
            "X-Public-Access": public,
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)
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
        restored = App(self.root, AISettings("", ""), aws_settings=self.settings, monitor_interval=0)
        self.assertEqual(restored.jobs[job_id]["status"], "interrupted")
        self.assertEqual(restored.jobs[job_id]["target"], "aws-s3-cloudfront")

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
