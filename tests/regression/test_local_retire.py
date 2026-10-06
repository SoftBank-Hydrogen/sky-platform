import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deployment.core import LocalDockerAdapter
from api.server import App, handler_for


JOB_ID = 'd' * 16
ATTEMPT = JOB_ID + '-a1'
NAME = 'sky-' + ATTEMPT
IMAGE = 'sky/' + ATTEMPT + ':latest'


class LocalRetireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name))
        source = self.app.root / JOB_ID / 'source'
        source.mkdir(parents=True)
        self.app.jobs[JOB_ID] = {
            'id': JOB_ID, 'mode': 'agent', 'project': str(source), 'plan': None,
            'status': 'succeeded', 'target': 'local-docker', 'deployment_state': 'active',
            'events': [], 'result': {'container': NAME, 'image': IMAGE, 'url': 'http://127.0.0.1:12345'}}
        self.app.save(JOB_ID)
        handler_class = handler_for(self.app)
        self.handler = handler_class.__new__(handler_class)
        self.handler.path = f'/api/jobs/{JOB_ID}/retire'
        self.handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': '0'}
        self.handler.json_response = Mock()

    def test_retire_requires_owned_container_and_image(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        container = {'Name': '/' + NAME,
                     'Config': {'Image': IMAGE, 'Labels': {'app': 'sky',
                                                          'sky-attempt': ATTEMPT}}}
        image = {'RepoTags': [IMAGE], 'Config': {'Labels': {'app': 'sky'}}}
        with patch.object(adapter, 'inspect_resource', side_effect=[container, None, image, None]), \
                patch.object(adapter, 'command') as command:
            adapter.retire({'container': NAME, 'image': IMAGE}, JOB_ID)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[0].args[0], ['docker', 'rm', '-f', NAME])
        self.assertEqual(command.call_args_list[1].args[0], ['docker', 'image', 'rm', IMAGE])

    def test_retire_refuses_replaced_container(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        with patch.object(adapter, 'inspect_resource', return_value={
                'Name': '/' + NAME, 'Config': {'Image': IMAGE, 'Labels': {'app': 'other'}}}), \
                patch.object(adapter, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'ownership changed'):
                adapter.retire({'container': NAME, 'image': IMAGE}, JOB_ID)
            command.assert_not_called()

    def test_retire_refuses_retagged_image_when_container_missing(self):
        adapter = LocalDockerAdapter(lambda *_: None)
        with patch.object(adapter, 'inspect_resource', side_effect=[None, {
                'RepoTags': [IMAGE], 'Config': {'Labels': {'app': 'other'}}}]), \
                patch.object(adapter, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'image ownership changed'):
                adapter.retire({'container': NAME, 'image': IMAGE}, JOB_ID)
            command.assert_not_called()

    def test_retire_api_and_restart_retry(self):
        with patch('api.server.threading.Thread') as thread:
            self.handler.do_POST()
            self.handler.json_response.assert_called_once_with(
                202, {'id': JOB_ID, 'deployment_state': 'deleting'})
            thread.assert_called_once()
        recovered = App(self.app.root)
        self.assertEqual(recovered.jobs[JOB_ID]['deployment_state'], 'delete_failed')
        with patch('api.server.LocalDockerAdapter.retire') as retire:
            recovered.retire_local(JOB_ID)
            retire.assert_called_once()
        self.assertEqual(recovered.jobs[JOB_ID]['deployment_state'], 'deleted')
        self.assertIn('retired_at', recovered.jobs[JOB_ID])

    def test_interrupted_agent_cleans_only_recorded_owned_attempts(self):
        job = self.app.jobs[JOB_ID]
        job.update(status='running', attempts=2)
        job.pop('result')
        self.app.save(JOB_ID)
        recovered = App(self.app.root)
        self.assertEqual(recovered.jobs[JOB_ID]['status'], 'interrupted')
        self.app = recovered
        handler_class = handler_for(recovered)
        self.handler = handler_class.__new__(handler_class)
        self.handler.path = f'/api/jobs/{JOB_ID}/retire'
        self.handler.headers = {'X-Sky-Token': recovered.token, 'Content-Length': '0'}
        self.handler.json_response = Mock()
        with patch('api.server.threading.Thread') as thread:
            self.handler.do_POST()
        self.handler.json_response.assert_called_once_with(
            202, {'id': JOB_ID, 'deployment_state': 'deleting'})
        thread.assert_called_once()
        with patch('api.server.LocalDockerAdapter.retire') as retire:
            recovered.retire_local(JOB_ID)
        self.assertEqual([call.args[0]['container'] for call in retire.call_args_list],
                         [f'sky-{JOB_ID}-a1', f'sky-{JOB_ID}-a2'])
        self.assertTrue(all(call.args[1] == JOB_ID for call in retire.call_args_list))
        self.assertEqual(recovered.jobs[JOB_ID]['deployment_state'], 'deleted')
        self.handler.json_response.reset_mock()
        self.handler.do_POST()
        self.handler.json_response.assert_called_once_with(409, {'error': '종료할 수 있는 배포가 아닙니다.'})

    def test_orphan_cleanup_refuses_foreign_container_and_stays_retryable(self):
        job = self.app.jobs[JOB_ID]
        job.update(status='interrupted', attempts=1)
        job.pop('result')
        self.app.save(JOB_ID)
        with patch.object(LocalDockerAdapter, 'inspect_resource', return_value={
                'Name': '/' + NAME, 'Config': {'Image': IMAGE, 'Labels': {'app': 'other'}}}), \
                patch.object(LocalDockerAdapter, 'command') as command:
            self.app.retire_local(JOB_ID)
            command.assert_not_called()
        self.assertEqual(job['deployment_state'], 'delete_failed')
        self.assertIn('ownership changed', job['retire_error'])
        with patch('api.server.threading.Thread') as thread:
            self.handler.do_POST()
        self.assertEqual(job['deployment_state'], 'deleting')
        thread.assert_called_once()

    def test_interrupted_job_without_attempt_cannot_retire(self):
        job = self.app.jobs[JOB_ID]
        job.update(status='interrupted', attempts=0)
        job.pop('result')
        with patch('api.server.threading.Thread') as thread:
            self.handler.do_POST()
        self.handler.json_response.assert_called_once_with(409, {'error': '종료할 수 있는 배포가 아닙니다.'})
        thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
