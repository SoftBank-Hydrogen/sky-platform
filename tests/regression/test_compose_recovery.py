"""Compose jobs preserve interruption and retirement state across server restarts."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.local.compose import LocalComposeAdapter
from interfaces.http.server import App, handler_for

JOB_ID = "a" * 16


class ComposeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = App(self.root, monitor_interval=0, github_poll_interval=0)
        source = self.root / JOB_ID / "source"
        source.mkdir(parents=True)
        self.app.jobs[JOB_ID] = {
            "id": JOB_ID,
            "mode": "agent",
            "target": "onprem-compose",
            "status": "running",
            "project": str(source),
            "plan": None,
            "attempts": 2,
            "events": [],
        }
        self.app.save(JOB_ID)

    def test_interrupted_attempts_are_not_redeployed_and_can_be_retired(self):
        recovered = App(self.root, monitor_interval=0, github_poll_interval=0)
        job = recovered.jobs[JOB_ID]
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["events"][-1]["stage"], "interrupted")

        handler = handler_for(recovered).__new__(handler_for(recovered))
        handler.path = f"/api/jobs/{JOB_ID}/retire"
        handler.headers = {"X-Sky-Token": recovered.token, "Content-Length": "0"}
        handler.json_response = Mock()
        with patch("interfaces.http.server.threading.Thread") as thread:
            handler.do_POST()
        handler.json_response.assert_called_once_with(202, {"id": JOB_ID, "deployment_state": "deleting"})
        thread.assert_called_once()

        restarted = App(self.root, monitor_interval=0, github_poll_interval=0)
        self.assertEqual(restarted.jobs[JOB_ID]["deployment_state"], "delete_failed")
        with patch.object(LocalComposeAdapter, "retire_orphan") as retire:
            restarted.retire_compose(JOB_ID)
        self.assertEqual([call.args[0] for call in retire.call_args_list], [JOB_ID + "-a1", JOB_ID + "-a2"])
        self.assertEqual(restarted.jobs[JOB_ID]["deployment_state"], "deleted")

    def test_completed_retirement_retries_after_server_restart(self):
        job = self.app.jobs[JOB_ID]
        job["status"] = "succeeded"
        job["deployment_state"] = "deleting"
        job["result"] = {"url": "http://127.0.0.1:12345", "container": "sky-" + JOB_ID + "-a1"}
        self.app.save(JOB_ID)
        recovered = App(self.root, monitor_interval=0, github_poll_interval=0)
        self.assertEqual(recovered.jobs[JOB_ID]["deployment_state"], "delete_failed")
        with patch.object(LocalComposeAdapter, "retire") as retire:
            recovered.retire_compose(JOB_ID)
        retire.assert_called_once()
        self.assertEqual(recovered.jobs[JOB_ID]["deployment_state"], "deleted")


if __name__ == "__main__":
    unittest.main()
