"""A paused agent must resume from its repaired working copy after a server restart."""
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.agent_fixture import call
from application.analysis import AISettings
from application.deployment_core import source_digest
from adapters.local.compose import LocalComposeAdapter
from adapters.local.docker import LocalDockerAdapter
from interfaces.http.server import App, handler_for


class AgentResumeTests(unittest.TestCase):
    def test_compose_environment_pause_survives_restart_and_uses_compose_adapter(self):
        actions = [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None,
                                      'port': 8080, 'health_path': '/',
                                      'required_env': ['APP_SECRET']}),
            ('deploy_application', {}),
        ]

        class ScriptedAgent:
            def __init__(self):
                self.index = 0

            def next(self, _history):
                name, arguments = actions[self.index]
                self.index += 1
                return call(name, arguments, self.index)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = 'e' * 16
            source = root / job_id / 'source'
            shutil.copytree('tests/fixtures/apps/hello-node', source)
            settings = AISettings('fixture-key', 'fixture-model')
            app = App(root, settings, agent_factory=lambda _: ScriptedAgent(), monitor_interval=0,
                      github_poll_interval=0)
            app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'status': 'running',
                                'target': 'onprem-compose', 'project': str(source),
                                'plan': None, 'events': [], 'attempts': 0,
                                'source_digest': source_digest(source)}
            app.save(job_id)
            with patch.object(LocalComposeAdapter, 'deploy') as deploy:
                app.run_agent(job_id)
                deploy.assert_not_called()
            self.assertEqual(app.jobs[job_id]['status'], 'waiting_input')
            work_digest = app.jobs[job_id]['work_digest']
            restored = App(root, settings, agent_factory=lambda _: ScriptedAgent(),
                           monitor_interval=0, github_poll_interval=0)
            self.assertEqual(restored.jobs[job_id]['status'], 'waiting_input')
            self.assertEqual(restored.jobs[job_id]['work_digest'], work_digest)
            supplied = {'APP_SECRET': 'synthetic-compose-secret'}
            received = []
            def deployed(_project, _plan, _attempt_id, environment):
                received.append(dict(environment))
                return {'url': 'http://127.0.0.1:12345',
                        'container': f'sky-{_attempt_id}',
                        'compose_project': f'sky-{_attempt_id}',
                        'image': f'sky/{_attempt_id}:latest',
                        'compose_sha256': 'a' * 64}
            with patch.object(LocalComposeAdapter, 'deploy', side_effect=deployed) as deploy:
                restored.run_agent(job_id, supplied)
            self.assertEqual(restored.jobs[job_id]['status'], 'succeeded')
            self.assertEqual(deploy.call_count, 1)
            self.assertEqual(received, [{'APP_SECRET': 'synthetic-compose-secret'}])
            self.assertEqual(supplied, {})
            self.assertNotIn('synthetic-compose-secret', (root / job_id / 'job.json').read_text())

    def test_environment_pause_restart_keeps_repairs_and_hides_secret(self):
        first_actions = [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', {'path': 'package.json', 'old_text': '"scripts": {}',
                                     'new_text': '"scripts": {"start": "node server.js"}'}),
            ('apply_project_patch', {'path': 'server.js',
                                     'old_text': ").listen(4321, '127.0.0.1',",
                                     'new_text': ").listen(Number(process.env.PORT || 4321), '0.0.0.0',"}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None,
                                      'port': 4321, 'health_path': '/',
                                      'required_env': ['APP_SECRET']}),
            ('deploy_application', {}),
        ]
        resumed_actions = [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None,
                                      'port': 4321, 'health_path': '/',
                                      'required_env': ['APP_SECRET']}),
            ('deploy_application', {}),
        ]
        secret = 'synthetic-secret-for-resume-test'

        class ScriptedAgent:
            def __init__(self, actions, resumed):
                self.actions, self.resumed, self.index = actions, resumed, 0

            def next(self, history):
                initial = json.loads(history[0]['content'])
                assert (secret not in json.dumps(history))
                assert ('APP_SECRET' in initial['available_environment_names']) is self.resumed
                name, arguments = self.actions[self.index]
                self.index += 1
                return call(name, arguments, self.index)

        instances = 0
        def factory(_settings):
            nonlocal instances
            instances += 1
            return ScriptedAgent(first_actions if instances == 1 else resumed_actions,
                                 resumed=instances > 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = 'a' * 16
            source = root / job_id / 'source'
            shutil.copytree('tests/fixtures/apps/unready-node', source)
            app = App(root, AISettings('fixture-key', 'fixture-model'),
                      agent_factory=factory, monitor_interval=0)
            app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'status': 'running',
                                'target': 'local-docker', 'project': str(source),
                                'plan': None, 'events': []}
            app.save(job_id)
            with patch.object(LocalDockerAdapter, 'deploy') as deploy:
                app.run_agent(job_id)
                deploy.assert_not_called()
                self.assertEqual(app.jobs[job_id]['status'], 'waiting_input')
                self.assertEqual(app.jobs[job_id]['missing_environment'], ['APP_SECRET'])
                self.assertIn('"scripts": {}', (source / 'package.json').read_text())
                work = root / job_id / 'work'
                self.assertIn('"start": "node server.js"', (work / 'package.json').read_text())
                self.assertIn('0.0.0.0', (work / 'server.js').read_text())
                self.assertEqual(app.jobs[job_id]['work_digest'], source_digest(work))

                restored = App(root, AISettings('fixture-key', 'fixture-model'),
                               agent_factory=factory, monitor_interval=0)
                self.assertEqual(restored.jobs[job_id]['status'], 'waiting_input')
                supplied = {'APP_SECRET': secret}
                seen_environment = []
                def deployed(_project, _plan, _attempt_id, environment):
                    seen_environment.append(dict(environment))
                    return {'url': 'http://127.0.0.1:12345',
                            'container': f'sky-{_attempt_id}', 'image': f'sky/{_attempt_id}:latest'}
                deploy.side_effect = deployed
                restored.run_agent(job_id, supplied)
                self.assertEqual(supplied, {})
                self.assertEqual(restored.jobs[job_id]['status'], 'succeeded')
                self.assertEqual(deploy.call_count, 1)
                self.assertEqual(seen_environment, [{'APP_SECRET': secret}])
                self.assertNotIn(secret, (root / job_id / 'job.json').read_text())

    def test_resume_rejects_missing_working_copy_before_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = 'b' * 16
            source = root / job_id / 'source'
            shutil.copytree('tests/fixtures/apps/unready-node', source)
            app = App(root, AISettings('fixture-key', 'fixture-model'),
                      agent_factory=lambda _: None, monitor_interval=0)
            app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'status': 'running',
                                'target': 'local-docker', 'project': str(source),
                                'plan': None, 'events': [], 'steps': 3,
                                'missing_environment': ['APP_SECRET']}
            app.save(job_id)
            with patch.object(LocalDockerAdapter, 'deploy') as deploy:
                app.run_agent(job_id, {'APP_SECRET': 'synthetic-secret'})
            self.assertEqual(app.jobs[job_id]['status'], 'failed')
            self.assertIn('작업용 소스를 찾지 못했습니다', app.jobs[job_id]['events'][-1]['message'])
            self.assertFalse((root / job_id / 'work').exists())
            deploy.assert_not_called()

    def test_resume_api_rejects_changed_work_before_accepting_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = 'c' * 16
            source = root / job_id / 'source'
            work = root / job_id / 'work'
            shutil.copytree('tests/fixtures/apps/unready-node', source)
            shutil.copytree(source, work)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'status': 'waiting_input',
                                'target': 'local-docker', 'project': str(source),
                                'plan': None, 'events': [], 'steps': 2,
                                'missing_environment': ['APP_SECRET'],
                                'work_digest': source_digest(work)}
            app.save(job_id)
            (work / 'server.js').write_text('changed after pause')
            payload = json.dumps({'environment': {'APP_SECRET': 'synthetic-secret'}}).encode()
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = f'/api/deployments/{job_id}/resume'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler.json_response = Mock()
            with patch('interfaces.http.server.threading.Thread.start') as start:
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 409)
            self.assertEqual(app.jobs[job_id]['status'], 'waiting_input')
            self.assertNotIn('synthetic-secret', (root / job_id / 'job.json').read_text())
            start.assert_not_called()
            # The worker checks again if files change after an API check but before it starts.
            app.jobs[job_id]['status'] = 'running'
            with patch.object(LocalDockerAdapter, 'deploy') as deploy:
                app.run_agent(job_id, {'APP_SECRET': 'synthetic-secret'})
            self.assertEqual(app.jobs[job_id]['status'], 'failed')
            self.assertEqual(app.jobs[job_id].get('attempts', 0), 0)
            deploy.assert_not_called()

    def test_resume_worker_start_failure_clears_secret_and_interrupts_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_id = 'd' * 16
            source = root / job_id / 'source'
            work = root / job_id / 'work'
            shutil.copytree('tests/fixtures/apps/unready-node', source)
            shutil.copytree(source, work)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'status': 'waiting_input',
                                'target': 'local-docker', 'project': str(source),
                                'plan': None, 'events': [], 'steps': 2, 'attempts': 0,
                                'missing_environment': ['APP_SECRET'],
                                'work_digest': source_digest(work)}
            app.save(job_id)
            secret = 'synthetic-secret-for-start-failure'
            payload = json.dumps({'environment': {'APP_SECRET': secret}}).encode()
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = f'/api/deployments/{job_id}/resume'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler.json_response = Mock()
            captured = []
            def fail_start(thread):
                captured.append(thread._args[1])
                raise RuntimeError('no thread')
            with patch('interfaces.http.server.threading.Thread.start', autospec=True,
                       side_effect=fail_start):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args,
                             (202, {'id': job_id, 'status': 'interrupted'}))
            self.assertEqual(captured, [{}])
            stored = (root / job_id / 'job.json').read_text()
            self.assertEqual(json.loads(stored)['status'], 'interrupted')
            self.assertNotIn(secret, stored)


if __name__ == '__main__':
    unittest.main()
