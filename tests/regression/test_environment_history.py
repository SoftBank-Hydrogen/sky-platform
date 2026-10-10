import json
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from application.analysis import AISettings
from application.deployment_core import analyze, source_digest, validate_environment
from adapters.local.docker import LocalDockerAdapter
from interfaces.http.server import App, StateDirectoryLock, handler_for


class EnvironmentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job_id = 'a' * 16
        self.project = self.root / self.job_id / 'source'
        self.project.mkdir(parents=True)
        (self.project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
        self.plan = analyze(self.project)
        self.plan.required_env = ['APP_SECRET']
        self.secret = 'synthetic-value-for-environment-test'

    def job(self, status='planned'):
        return {'id': self.job_id, 'status': status, 'project': str(self.project),
                'plan': asdict(self.plan), 'events': [], 'diff': 'diff'}

    def test_environment_validation(self):
        self.assertEqual(validate_environment({'APP_SECRET': self.secret}, ['APP_SECRET']), {'APP_SECRET': self.secret})
        for values in ({}, {'APP_SECRET': ''}, {'APP_SECRET': 'bad\nNEXT=bad'},
                       {'APP_SECRET': 'a\x00b'}, {'APP_SECRET': 42}, {'invalid-key': 'x'},
                       {'PORT': '8080'}, {'NODE_ENV': 'dev'}, []):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_environment(values, ['APP_SECRET'])

    def test_environment_file_private_deleted_and_logs_masked_on_failure(self):
        events = []
        adapter = LocalDockerAdapter(lambda stage, message: events.append(message))
        temp_paths = []
        def command(args, timeout=300):
            if args[1] == 'build':
                context = Path(args[-1])
                self.assertFalse(context.is_relative_to(self.project))
                self.assertNotIn(self.secret, (context / 'Dockerfile').read_text())
            if args[1] == 'run':
                path = Path(args[args.index('--env-file') + 1])
                temp_paths.append(path)
                self.assertFalse(path.is_relative_to(self.project))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.read_text(), 'APP_SECRET=' + self.secret + '\n')
                self.assertNotIn(self.secret, ' '.join(args))
                adapter.event('output', 'app output ' + self.secret)
                raise RuntimeError('run failed')
            return ''
        with patch.object(adapter, 'command', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'run failed'):
                adapter.deploy(self.project, self.plan, self.job_id, {'APP_SECRET': self.secret})
        self.assertTrue(temp_paths)
        self.assertFalse(temp_paths[0].exists())
        self.assertNotIn(self.secret, '\n'.join(events))
        self.assertIn('[REDACTED]', '\n'.join(events))
        self.assertFalse((self.project / 'Dockerfile').exists())
        self.assertEqual(source_digest(self.project), self.plan.source_digest)

    def test_worker_clears_values_and_masks_exceptions(self):
        app = App(self.root, AISettings())
        app.jobs[self.job_id] = self.job('running')
        values = {'APP_SECRET': self.secret}
        with patch.object(LocalDockerAdapter, 'deploy', side_effect=RuntimeError('failure ' + self.secret)):
            app.run(self.job_id, values)
        self.assertEqual(values, {})
        stored = (self.root / self.job_id / 'job.json').read_text()
        self.assertNotIn(self.secret, stored)
        self.assertIn('[REDACTED]', stored)

    def test_planned_and_successful_history_restored(self):
        for status in ('planned', 'succeeded'):
            with self.subTest(status=status):
                app = App(self.root, AISettings())
                job = self.job(status)
                if status == 'succeeded':
                    job['result'] = {'url': 'http://127.0.0.1:12345'}
                app.jobs[self.job_id] = job
                app.save(self.job_id)
                restored = App(self.root, AISettings())
                self.assertEqual(restored.jobs[self.job_id]['status'], status)
                self.assertEqual(restored.jobs[self.job_id]['plan'], asdict(self.plan))
                self.assertNotEqual(app.token, restored.token)
                self.assertEqual(restored.summaries()[0]['id'], self.job_id)

    def test_running_becomes_interrupted_without_redeployment(self):
        app = App(self.root, AISettings())
        app.jobs[self.job_id] = self.job('running')
        app.save(self.job_id)
        with patch.object(LocalDockerAdapter, 'deploy') as deploy:
            restored = App(self.root, AISettings())
            deploy.assert_not_called()
        self.assertEqual(restored.jobs[self.job_id]['status'], 'interrupted')
        self.assertEqual(json.loads((self.root / self.job_id / 'job.json').read_text())['status'], 'interrupted')

    def test_restart_removes_only_marked_uncommitted_uploads(self):
        orphan = self.root / ('b' * 16)
        orphan.mkdir()
        (orphan / '.uncommitted-upload').touch()
        (orphan / 'source.zip').write_bytes(b'partial upload')
        (orphan / '.job-abc123.tmp').write_text('partial record')
        (orphan / 'source').mkdir()
        (orphan / 'source' / 'server.js').write_text('partial extraction')

        unknown = self.root / ('c' * 16)
        unknown.mkdir()
        (unknown / '.uncommitted-upload').touch()
        (unknown / 'work').mkdir()

        committed = self.root / ('d' * 16)
        committed.mkdir()
        (committed / '.uncommitted-upload').touch()
        (committed / 'job.json').write_text('{}')

        restored = App(self.root, AISettings())
        self.assertFalse(orphan.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(committed.exists())
        self.assertTrue(self.project.exists())
        self.assertTrue(any(unknown.name in warning for warning in restored.recovery_warnings))

    def test_corrupt_state_does_not_stop_startup(self):
        for content in ('{broken', '[]', 'null'):
            with self.subTest(content=content):
                (self.root / self.job_id / 'job.json').write_text(content)
                app = App(self.root, AISettings())
                self.assertEqual(app.jobs, {})
                self.assertEqual(len(app.recovery_warnings), 1)

    def test_state_outside_project_is_rejected(self):
        job = self.job()
        job['project'] = str(self.root.parent)
        (self.root / self.job_id / 'job.json').write_text(json.dumps(job))
        app = App(self.root, AISettings())
        self.assertEqual(app.jobs, {})
        self.assertEqual(len(app.recovery_warnings), 1)

    def test_only_one_server_can_hold_state_directory_lock(self):
        with StateDirectoryLock(self.root):
            with self.assertRaisesRegex(RuntimeError, '이미 다른 Sky 서버'):
                with StateDirectoryLock(self.root):
                    pass
        with StateDirectoryLock(self.root):
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.root / '.server.lock').stat().st_mode & 0o777, 0o600)

    def test_failed_atomic_save_keeps_previous_record_and_marks_job_failed(self):
        app = App(self.root, AISettings())
        app.jobs[self.job_id] = self.job('planned')
        app.save(self.job_id)
        stored = (self.root / self.job_id / 'job.json').read_text()
        app.jobs[self.job_id].update(status='succeeded', result={'url': 'http://127.0.0.1:12345'})
        with patch('adapters.state.records.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(RuntimeError, '저장에 실패'):
                app.save(self.job_id)
        self.assertEqual((self.root / self.job_id / 'job.json').read_text(), stored)
        self.assertEqual((self.root / self.job_id / 'job.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(app.jobs[self.job_id]['status'], 'failed')
        self.assertNotIn('result', app.jobs[self.job_id])
        self.assertFalse(list((self.root / self.job_id).glob('.job-*.tmp')))

    def test_application_release_index_and_concurrent_deploy_guard(self):
        app = App(self.root, AISettings())
        first = self.job('succeeded')
        first.update(application_id='my-web-app', target='local-docker',
                     created_at='2026-09-27T00:00:00+00:00')
        second = dict(self.job('running'), id='b' * 16, application_id='my-web-app',
                      target='local-docker', created_at='2026-09-28T00:00:00+00:00')
        app.jobs[self.job_id] = first
        app.jobs[second['id']] = second
        self.assertEqual([release['id'] for release in app.releases('my-web-app')],
                         [second['id'], self.job_id])
        handler_class = handler_for(app)
        handler = handler_class.__new__(handler_class)
        handler.path = '/api/applications/my-web-app/releases'
        handler.headers = {'X-Sky-Token': app.token}
        handler.json_response = Mock()
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        self.assertEqual(len(handler.json_response.call_args.args[1]), 2)
        self.assertEqual(app.summaries()[0]['application_id'], 'my-web-app')
        with self.assertRaisesRegex(ValueError, '이미 진행 중'):
            app.ensure_application_available('my-web-app', 'local-docker')
        app.ensure_application_available('my-web-app', 'cloud-run')
        second['status'] = 'failed'
        app.ensure_application_available('my-web-app', 'local-docker')

    def test_waiting_application_can_be_cancelled_and_released(self):
        app = App(self.root, AISettings())
        job = self.job('waiting_input')
        job.update(mode='agent', application_id='my-web-app', target='local-docker',
                   missing_environment=['APP_SECRET'])
        app.jobs[self.job_id] = job
        handler_class = handler_for(app)
        handler = handler_class.__new__(handler_class)
        handler.path = f'/api/deployments/{self.job_id}/cancel'
        handler.headers = {'X-Sky-Token': app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        with self.assertRaisesRegex(ValueError, '이미 진행 중'):
            app.ensure_application_available('my-web-app', 'local-docker')
        handler.do_POST()
        handler.json_response.assert_called_once_with(200, {'id': self.job_id, 'status': 'cancelled'})
        self.assertEqual(job['missing_environment'], [])
        self.assertEqual(json.loads((self.root / self.job_id / 'job.json').read_text())['status'], 'cancelled')
        app.ensure_application_available('my-web-app', 'local-docker')

    def test_running_pre_deploy_cancellation_stops_after_ai_response(self):
        entered, release = threading.Event(), threading.Event()
        class SlowAgent:
            def next(self, _history):
                entered.set()
                self.assert_release()
                return [{'type': 'function_call', 'call_id': 'one',
                         'name': 'deploy_application', 'arguments': '{}'}]

            def assert_release(self):
                if not release.wait(5):
                    raise RuntimeError('Timed out waiting for test release')

        app = App(self.root, AISettings(), agent_factory=lambda _: SlowAgent())
        job = self.job('running')
        job.update(mode='agent', application_id='my-web-app', target='local-docker', plan=None)
        app.jobs[self.job_id] = job
        app.save(self.job_id)
        worker = threading.Thread(target=app.run_agent, args=(self.job_id,))
        with patch.object(LocalDockerAdapter, 'deploy') as deploy:
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                handler = handler_for(app).__new__(handler_for(app))
                handler.path = f'/api/deployments/{self.job_id}/cancel'
                handler.headers = {'X-Sky-Token': app.token, 'Content-Length': '0'}
                handler.json_response = Mock()
                handler.do_POST()
                handler.json_response.assert_called_once_with(
                    202, {'id': self.job_id, 'status': 'cancelling'})
                self.assertTrue(job['cancel_requested'])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            deploy.assert_not_called()
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job.get('attempts', 0), 0)
        self.assertEqual(json.loads((self.root / self.job_id / 'job.json').read_text())['status'], 'cancelled')
        app.ensure_application_available('my-web-app', 'local-docker')

    def test_cancellation_rejected_after_deploy_attempt_starts(self):
        app = App(self.root, AISettings())
        job = self.job('running')
        job.update(mode='agent', application_id='my-web-app', target='local-docker', attempts=1)
        app.jobs[self.job_id] = job
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = f'/api/deployments/{self.job_id}/cancel'
        handler.headers = {'X-Sky-Token': app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 409)
        self.assertEqual(job['status'], 'running')

    def test_pending_pre_deploy_cancel_survives_server_restart(self):
        app = App(self.root, AISettings())
        job = self.job('running')
        job.update(mode='agent', application_id='my-web-app', target='local-docker',
                   cancel_requested=True, attempts=0)
        app.jobs[self.job_id] = job
        app.save(self.job_id)
        restored = App(self.root, AISettings())
        self.assertEqual(restored.jobs[self.job_id]['status'], 'cancelled')
        self.assertNotIn('cancel_requested', restored.jobs[self.job_id])


if __name__ == '__main__':
    unittest.main()
