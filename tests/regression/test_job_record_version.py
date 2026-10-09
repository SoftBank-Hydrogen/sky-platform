"""Job recovery must not interpret an unknown record format as a known deployment."""

import json
import tempfile
import unittest
from pathlib import Path

from engine.deployment_policy import deployment_policy
from interfaces.http.server import App


JOB_ID = "a" * 16


class JobRecordVersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / JOB_ID / "source"
        self.source.mkdir(parents=True)
        self.record = self.source.parent / "job.json"
        self.legacy = {
            "id": JOB_ID,
            "mode": "agent",
            "status": "succeeded",
            "project": str(self.source),
            "plan": None,
            "events": [],
            "attempts": 1,
            "result": {"url": "http://127.0.0.1:12345"},
        }

    def test_new_record_is_versioned_and_survives_restart(self):
        app = App(self.root, monitor_interval=0, github_poll_interval=0)
        app.jobs[JOB_ID] = self.legacy.copy()
        app.save(JOB_ID)
        self.assertEqual(json.loads(self.record.read_text())["job_record_version"], 1)
        restored = App(self.root, monitor_interval=0, github_poll_interval=0)
        self.assertEqual(restored.jobs[JOB_ID]["job_record_version"], 1)
        self.assertEqual(restored.jobs[JOB_ID]["status"], "succeeded")

    def test_legacy_record_is_read_without_rewriting_until_next_save(self):
        self.record.write_text(json.dumps(self.legacy))
        restored = App(self.root, monitor_interval=0, github_poll_interval=0)
        self.assertIn(JOB_ID, restored.jobs)
        self.assertNotIn("job_record_version", json.loads(self.record.read_text()))
        restored.save(JOB_ID)
        self.assertEqual(json.loads(self.record.read_text())["job_record_version"], 1)

    def test_unknown_versions_are_not_loaded_or_overwritten(self):
        for version in (2, -1, True, "1"):
            with self.subTest(version=version):
                self.record.write_text(json.dumps({**self.legacy, "job_record_version": version}))
                original = self.record.read_bytes()
                restored = App(self.root, monitor_interval=0, github_poll_interval=0)
                self.assertNotIn(JOB_ID, restored.jobs)
                self.assertTrue(any(JOB_ID in item for item in restored.recovery_warnings))
                self.assertEqual(self.record.read_bytes(), original)

    def test_save_does_not_downgrade_an_unknown_record(self):
        app = App(self.root, monitor_interval=0, github_poll_interval=0)
        app.jobs[JOB_ID] = {**self.legacy, "job_record_version": 2}
        with self.assertRaisesRegex(ValueError, "Unsupported job record version"):
            app.save(JOB_ID)
        self.assertFalse(self.record.exists())

    def test_invalid_attempt_count_is_not_loaded_for_orphan_cleanup(self):
        for attempts in (-1, 4, True, "2"):
            with self.subTest(attempts=attempts):
                self.record.write_text(json.dumps({**self.legacy, "attempts": attempts}))
                restored = App(self.root, monitor_interval=0, github_poll_interval=0)
                self.assertNotIn(JOB_ID, restored.jobs)

    def test_invalid_stored_policy_is_not_loaded_or_rewritten(self):
        policy = deployment_policy('local-docker', False).as_dict()
        policy['allowed_targets'] = ['imaginary-cloud']
        for invalid in (policy, None):
            with self.subTest(policy=invalid):
                self.record.write_text(json.dumps({**self.legacy, 'deployment_policy': invalid}))
                original = self.record.read_bytes()
                restored = App(self.root, monitor_interval=0, github_poll_interval=0)
                self.assertNotIn(JOB_ID, restored.jobs)
                self.assertTrue(any(JOB_ID in item for item in restored.recovery_warnings))
                self.assertEqual(self.record.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
