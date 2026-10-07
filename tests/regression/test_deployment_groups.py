import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.aws.ecs import AwsSettings
from application.analysis import AISettings
from application.deployment_core import analyze
from interfaces.http.server import App, handler_for


def archive(database=False):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as bundle:
        bundle.writestr('package.json', json.dumps({'scripts': {'start': 'node server.js'}}))
        bundle.writestr('server.js', "require('node:http').createServer((req,res)=>res.end('ok')).listen(3000)")
        if database:
            bundle.writestr('app.db', b'SQLite format 3\x00')
    return data.getvalue()


class DeploymentGroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = App(self.root / 'state', AISettings('fixture-key', 'fixture-model'),
                       aws_settings=AwsSettings('ap-northeast-2', expected_account='123456789012'),
                       monitor_interval=0)
        self.source = self.root / 'upload'
        self.source.mkdir()
        (self.source / 'package.json').write_text(json.dumps({'scripts': {'start': 'node server.js'}}))
        (self.source / 'server.js').write_text(
            "require('node:http').createServer((req,res)=>res.end('ok')).listen(3000)")

    def create(self):
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None):
            return self.app.create_deployment_group(
                self.source, 'demo-app', ['local-docker', 'aws-ecs-express'], True)

    def test_one_upload_reserves_ordered_independent_jobs_and_partial_success(self):
        group = self.create()
        first, second = group['targets']
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertEqual([item['target'] for item in group['targets']],
                         ['local-docker', 'aws-ecs-express'])
        self.assertEqual(self.app.jobs[first['job_id']]['source_digest'],
                         self.app.jobs[second['job_id']]['source_digest'])
        calls = []
        def run(job_id):
            calls.append(job_id)
            job = self.app.jobs[job_id]
            job['status'] = 'failed' if job_id == first['job_id'] else 'succeeded'
            if job['status'] == 'succeeded':
                job['result'] = {'url': 'https://example.test'}
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=run):
            self.app.run_group(group['id'])
        result = self.app.deployment_group(group['id'])
        self.assertEqual(calls, [first['job_id'], second['job_id']])
        self.assertEqual(result['status'], 'partial_success')
        self.assertEqual([item['status'] for item in result['targets']], ['failed', 'succeeded'])
        self.assertEqual(result['targets'][1]['url'], 'https://example.test')
        self.app.jobs[second['job_id']]['deployment_state'] = 'deleted'
        self.assertIsNone(self.app.deployment_group(group['id'])['targets'][1]['url'])

    def test_successful_local_image_is_promoted_without_a_second_agent_run(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        image_id = 'sha256:' + 'a' * 64
        calls = []
        def run_local(job_id):
            calls.append(job_id)
            self.assertEqual(job_id, local_id)
            job = self.app.jobs[job_id]
            work = self.app.root / job_id / 'work'
            shutil.copytree(Path(job['project']), work)
            plan = analyze(work)
            job.update(status='succeeded', plan=plan.__dict__, attempts=1,
                       result={'image': f'sky/{local_id}-a1:latest', 'image_id': image_id,
                               'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=run_local), \
                patch('interfaces.http.server.AwsExpressAdapter.deploy',
                      return_value={'url': 'https://example.test',
                                    'promotion': {'image_id': image_id}}) as deploy:
            self.app.run_group(group['id'])
        self.assertEqual(calls, [local_id])
        self.assertEqual(deploy.call_count, 1)
        self.assertEqual(deploy.call_args.args[1].target, 'aws-ecs-express')
        self.assertEqual(self.app.jobs[aws_id]['status'], 'succeeded')
        self.assertEqual(self.app.jobs[aws_id]['attempts'], 1)
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'succeeded')

    def test_waiting_for_environment_pauses_next_target(self):
        group = self.create()
        first, second = [item['job_id'] for item in group['targets']]
        def wait_for_input(job_id):
            self.app.jobs[job_id]['status'] = 'waiting_input'
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=wait_for_input) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(first)
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'waiting_input')
        self.app.jobs[first]['status'] = 'succeeded'
        self.app.save(first)
        with patch.object(self.app, 'run_agent', side_effect=lambda job_id:
                          self.app.jobs[job_id].update(status='failed')) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(second)

    def test_unexpected_runner_error_stops_before_next_target(self):
        group = self.create()
        first, second = [item['job_id'] for item in group['targets']]
        with patch.object(self.app, 'run_agent', side_effect=RuntimeError('runner stopped')) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(first)
        self.assertEqual(self.app.jobs[first]['status'], 'interrupted')
        self.assertEqual(self.app.jobs[second]['status'], 'planned')
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'interrupted')

    def test_restart_marks_running_child_interrupted_without_replaying_it(self):
        group = self.create()
        first, second = [item['job_id'] for item in group['targets']]
        self.app.jobs[first]['status'] = 'running'
        self.app.save(first)
        restored = App(self.root / 'state', AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        self.assertEqual(restored.jobs[first]['status'], 'interrupted')
        self.assertEqual(restored.jobs[second]['status'], 'planned')
        self.assertEqual(restored.deployment_group(group['id'])['status'], 'interrupted')
        with patch.object(restored, 'run_agent', side_effect=lambda job_id:
                          restored.jobs[job_id].update(status='succeeded')) as run:
            restored.run_group(group['id'])
        run.assert_called_once_with(second)

    def test_database_app_is_rejected_before_any_job_is_recorded(self):
        (self.source / 'app.db').write_bytes(b'SQLite format 3\x00')
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None):
            with self.assertRaisesRegex(ValueError, 'SQLite'):
                self.app.create_deployment_group(
                    self.source, 'demo-app', ['local-docker', 'aws-ecs-express'], True)
        self.assertFalse(self.app.jobs)

    def test_group_http_endpoint_accepts_one_zip_and_rejects_database_headers(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/deployment-groups'
        handler.rfile = io.BytesIO(archive())
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': str(len(archive())),
                           'X-Application-Id': 'demo-app',
                           'X-Deploy-Targets': 'local-docker,aws-ecs-express',
                           'X-Public-Access': 'true'}
        handler.json_response = Mock()
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None), \
             patch.object(self.app, 'start_group_worker', return_value=True):
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        self.assertEqual(len(self.app.jobs), 2)
        handler.headers['X-Postgres-Existing'] = 'true'
        handler.rfile = io.BytesIO(archive())
        handler.json_response.reset_mock()
        handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        self.assertEqual(len(self.app.jobs), 2)

    def test_read_and_continue_require_a_real_interrupted_group(self):
        group = self.create()
        first = group['targets'][0]['job_id']
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        handler.path = '/api/deployment-groups/' + group['id']
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        handler.path += '/continue'
        handler.json_response.reset_mock()
        with patch.object(self.app, 'start_group_worker') as start:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        start.assert_not_called()
        self.app.jobs[first]['status'] = 'interrupted'
        self.app.save(first)
        handler.json_response.reset_mock()
        with patch.object(self.app, 'start_group_worker', return_value=True) as start:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        start.assert_called_once_with(group['id'])


if __name__ == '__main__':
    unittest.main()
