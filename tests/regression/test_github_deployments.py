import tempfile
import unittest
import zipfile
import json
from pathlib import Path
from unittest.mock import patch

from application.analysis import AISettings
from application.github_source import parse_repository_url, resolve_revision, validate_branch
from adapters.gcp.cloud_run import CloudRunSettings
from interfaces.http.server import App


FIRST = "a" * 40
SECOND = "b" * 40


def write_app_archive(_repository, commit, destination):
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(
            "demo-" + commit[:7] + "/package.json", '{"name":"demo","scripts":{"start":"node server.js"}}'
        )
        archive.writestr(
            "demo-" + commit[:7] + "/server.js",
            'require("http").createServer((req,res)=>res.end("ok")).listen(process.env.PORT||3000)',
        )


class GitHubSourceTests(unittest.TestCase):
    def test_restricts_urls_and_branch_names(self):
        self.assertEqual(
            parse_repository_url("https://github.com/team/demo.git/").url, "https://github.com/team/demo"
        )
        for url in (
            "http://github.com/team/demo",
            "https://github.com.evil.test/team/demo",
            "https://user:secret@github.com/team/demo",
            "https://github.com/team/demo/tree/main",
            "https://github.com/team/demo?x=1",
            "https://github.com/team/../demo",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_repository_url(url)
        for branch in ("../main", "main//x", "-bad", "bad.lock", ".hidden"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                validate_branch(branch)

    def test_resolves_default_branch_without_confusing_symref_and_commit(self):
        output = "ref: refs/heads/main\tHEAD\n" + FIRST + "\tHEAD\n"
        with patch("application.github_source.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = output
            self.assertEqual(
                resolve_revision(parse_repository_url("https://github.com/team/demo"), None), ("main", FIRST)
            )
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "HEAD")

    def test_initial_import_and_push_create_pinned_jobs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                monitor_interval=0,
                github_poll_interval=60,
            )
            with (
                patch(
                    "application.github_deployments.resolve_revision",
                    side_effect=[("main", FIRST), ("main", FIRST), ("main", SECOND), ("main", SECOND)],
                ),
                patch("application.github_deployments.download_revision", side_effect=write_app_archive),
                patch.object(app, "start_job_worker", return_value=True) as worker,
            ):
                first = app.create_github_deployment(
                    "https://github.com/team/demo", None, "demo-app", ["local-docker"], False, True
                )
                source_id = first["source_id"]
                first_job = first["deployment"]["id"]
                self.assertEqual(app.jobs[first_job]["github_source"]["commit"], FIRST)
                self.assertEqual(app.jobs[first_job]["deployment_policy"]["allowed_targets"],
                                 ("local-docker",))
                self.assertFalse(app.poll_github_source(source_id)["changed"])
                app.jobs[first_job]["status"] = "succeeded"
                app.jobs[first_job]["result"] = {"container": "sky-" + first_job + "-a1"}
                app.jobs[first_job]["deployment_state"] = "active"
                app.save(first_job)
                changed = app.poll_github_source(source_id)
                self.assertTrue(changed["changed"])
                second_job = changed["job_ids"][0]
                self.assertEqual(app.jobs[second_job]["git_replaces_local_job_id"], first_job)
                self.assertEqual(app.jobs[second_job]["github_source"]["commit"], SECOND)
                self.assertEqual(app.github_sources[source_id]["last_revision"], SECOND)
                self.assertFalse(app.poll_github_source(source_id)["changed"])
                self.assertEqual(worker.call_count, 2)
                app.set_github_source_enabled(source_id, False)
                with self.assertRaisesRegex(ValueError, "활성"):
                    app.poll_github_source(source_id)
            restarted = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                monitor_interval=0,
                github_poll_interval=60,
            )
            self.assertFalse(restarted.github_sources[source_id]["enabled"])
            self.assertEqual(restarted.github_sources[source_id]["last_revision"], SECOND)
            self.assertEqual(restarted.jobs[second_job]["status"], "interrupted")
            self.assertTrue(restarted.remove_github_source(source_id)["disconnected"])
            self.assertFalse(restarted.github_source_summaries())

    def test_failed_push_is_visible_and_same_commit_retry_is_manual(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                monitor_interval=0,
                github_poll_interval=60,
            )
            with (
                patch(
                    "application.github_deployments.resolve_revision",
                    side_effect=[
                        ("main", FIRST),
                        ("main", SECOND),
                        ("main", SECOND),
                        ("main", SECOND),
                        ("main", SECOND),
                    ],
                ),
                patch("application.github_deployments.download_revision", side_effect=write_app_archive),
                patch.object(app, "start_job_worker") as worker,
            ):
                first = app.create_github_deployment(
                    "https://github.com/team/demo", None, "demo-app", ["local-docker"], False, True
                )
                source_id = first["source_id"]
                first_job = first["deployment"]["id"]
                app.jobs[first_job]["status"] = "succeeded"
                app.jobs[first_job]["result"] = {"container": "sky-" + first_job + "-a1"}
                app.jobs[first_job]["deployment_state"] = "active"
                app.save(first_job)
                failed = app.poll_github_source(source_id)
                failed_job = failed["job_ids"][0]
                app.jobs[failed_job]["status"] = "failed"
                app.save(failed_job)
                summary = app.github_source_summaries()[0]
                self.assertEqual(summary["last_deployment_status"], "failed")
                self.assertTrue(summary["retryable"])
                self.assertEqual(app.jobs[first_job]["deployment_state"], "active")
                with patch(
                    "application.github_deployments.resolve_revision",
                    return_value=("main", "c" * 40),
                ):
                    with self.assertRaisesRegex(ValueError, "새 커밋"):
                        app.poll_github_source(source_id, retry_failed=True)
                self.assertEqual(worker.call_count, 2)
                self.assertFalse(app.poll_github_source(source_id)["changed"])
                self.assertEqual(worker.call_count, 2)
                retry = app.poll_github_source(source_id, retry_failed=True)
                self.assertTrue(retry["changed"])
                self.assertEqual(retry["commit"], SECOND)
                self.assertNotEqual(retry["job_ids"], [failed_job])
                self.assertEqual(app.github_source_summaries()[0]["last_deployment_status"], "running")
                self.assertEqual(worker.call_count, 3)
                with self.assertRaisesRegex(ValueError, "다시 시도할 실패한"):
                    app.poll_github_source(source_id, retry_failed=True)
                retried_job = retry["job_ids"][0]
                self.assertEqual(app.jobs[retried_job]["git_replaces_local_job_id"], first_job)
                app.jobs[retried_job]["status"] = "succeeded"
                app.jobs[retried_job]["result"] = {"container": "sky-" + retried_job + "-a1"}
                app.save(retried_job)
                with patch("application.github_deployments.LocalDockerAdapter.retire") as retire:
                    app.retire_replaced_github_local(retried_job)
                retire.assert_called_once()
                self.assertEqual(app.jobs[first_job]["deployment_state"], "deleted")
                summary = app.github_source_summaries()[0]
                self.assertEqual(summary["last_deployment_status"], "succeeded")
                self.assertFalse(summary["retryable"])

    def test_failed_import_does_not_persist_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                monitor_interval=0,
                github_poll_interval=60,
            )
            with (
                patch("application.github_deployments.resolve_revision", return_value=("main", FIRST)),
                patch(
                    "application.github_deployments.download_revision",
                    side_effect=ValueError("download failed"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "download failed"):
                    app.create_github_deployment(
                        "https://github.com/team/demo", None, "demo-app", ["local-docker"], False, True
                    )
            self.assertFalse(app.github_sources)
            self.assertFalse(app.jobs)

    def test_subscription_save_failure_discards_reserved_job(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                monitor_interval=0,
                github_poll_interval=60,
            )
            with (
                patch("application.github_deployments.resolve_revision", return_value=("main", FIRST)),
                patch("application.github_deployments.download_revision", side_effect=write_app_archive),
                patch.object(app, "save_github_sources", side_effect=OSError("disk full")),
                patch.object(app, "start_job_worker") as worker,
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    app.create_github_deployment(
                        "https://github.com/team/demo", None, "demo-app", ["local-docker"], False, True
                    )
            self.assertFalse(app.github_sources)
            self.assertFalse(app.jobs)
            self.assertFalse(list(Path(directory).glob("*/job.json")))
            worker.assert_not_called()

    def test_invalid_saved_subscription_disables_automatic_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "github-sources.json").write_text(json.dumps([{"id": "bad"}]))
            app = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                monitor_interval=0,
                github_poll_interval=60,
            )
            self.assertFalse(app.github_sources)
            self.assertTrue(any("GitHub" in warning for warning in app.recovery_warnings))

    def test_one_time_link_import_can_start_a_multi_target_group(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(
                Path(directory),
                AISettings("fixture-key", "fixture-model"),
                cloud_settings=CloudRunSettings("demo-project", "asia-northeast3"),
                monitor_interval=0,
                github_poll_interval=0,
            )
            with (
                patch("adapters.gcp.cloud_run.CloudRunSettings.unavailable_reason", return_value=None),
                patch("application.github_deployments.resolve_revision", return_value=("main", FIRST)),
                patch("application.github_deployments.download_revision", side_effect=write_app_archive),
                patch.object(app, "start_group_worker") as worker,
            ):
                response = app.create_github_deployment(
                    "https://github.com/team/demo",
                    "main",
                    "demo-app",
                    ["local-docker", "cloud-run"],
                    False,
                    False,
                )
            self.assertIsNone(response["source_id"])
            self.assertEqual(len(response["deployment"]["targets"]), 2)
            worker.assert_called_once_with(response["deployment"]["id"])
            self.assertEqual({job["github_source"]["commit"] for job in app.jobs.values()}, {FIRST})
            self.assertEqual(len({job["source_digest"] for job in app.jobs.values()}), 1)


if __name__ == "__main__":
    unittest.main()
