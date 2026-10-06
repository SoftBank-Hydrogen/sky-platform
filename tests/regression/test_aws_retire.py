import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api.server import App, handler_for


JOB_ID = 'a' * 16
ATTEMPT = JOB_ID + '-a1'
SERVICE = 'sky-' + ATTEMPT
REGION = 'ap-northeast-2'
ACCOUNT = '123456789012'


class AwsRetireApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name))
        (self.app.root / JOB_ID).mkdir()
        source = self.app.root / JOB_ID / 'source'
        source.mkdir()
        self.app.jobs[JOB_ID] = {
            'id': JOB_ID, 'mode': 'agent', 'project': str(source), 'plan': None,
            'status': 'succeeded', 'target': 'aws-ecs-express',
            'deployment_state': 'active', 'aws': {'region': REGION}, 'events': [],
            'result': {'service': SERVICE, 'service_arn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}',
                       'image': f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:{ATTEMPT}',
                       'region': REGION, 'account': ACCOUNT, 'target': 'aws-ecs-express',
                       'url': f'https://{SERVICE}.ecs.{REGION}.on.aws'}}
        self.app.save(JOB_ID)
        handler_class = handler_for(self.app)
        self.handler = handler_class.__new__(handler_class)
        self.handler.path = f'/api/jobs/{JOB_ID}/retire'
        self.handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': '0'}
        self.handler.json_response = Mock()

    def test_retire_api_starts_once_and_marks_deleted_after_adapter_success(self):
        with patch('api.server.threading.Thread') as thread, \
                patch('api.server.AwsExpressAdapter.retire') as retire:
            self.handler.do_POST()
            self.handler.json_response.assert_called_once_with(202, {'id': JOB_ID, 'deployment_state': 'deleting'})
            thread.assert_called_once()
            self.assertEqual(self.app.jobs[JOB_ID]['deployment_state'], 'deleting')
            self.handler.json_response.reset_mock()
            self.handler.do_POST()
            self.handler.json_response.assert_called_once_with(409, {'error': '종료할 수 있는 AWS 배포가 아닙니다.'})
            self.app.retire_aws(JOB_ID)
            retire.assert_called_once()
        self.assertEqual(self.app.jobs[JOB_ID]['deployment_state'], 'deleted')
        self.handler.path = f'/api/jobs/{JOB_ID}/health'
        self.handler.json_response.reset_mock()
        self.handler.do_GET()
        self.handler.json_response.assert_called_once_with(409, {'error': '배포 종료 중이거나 이미 종료됐습니다.'})

    def test_retire_failure_keeps_job_retryable(self):
        self.app.jobs[JOB_ID]['deployment_state'] = 'deleting'
        with patch('api.server.AwsExpressAdapter.retire', side_effect=RuntimeError('AWS timeout')):
            self.app.retire_aws(JOB_ID)
        self.assertEqual(self.app.jobs[JOB_ID]['deployment_state'], 'delete_failed')
        self.assertIn('AWS timeout', self.app.jobs[JOB_ID]['retire_error'])
        with patch('api.server.threading.Thread') as thread:
            self.handler.do_POST()
        self.assertEqual(self.app.jobs[JOB_ID]['deployment_state'], 'deleting')
        thread.assert_called_once()

    def test_restart_marks_unfinished_retirement_retryable(self):
        self.app.jobs[JOB_ID]['deployment_state'] = 'deleting'
        self.app.save(JOB_ID)
        recovered = App(self.app.root)
        self.assertEqual(recovered.jobs[JOB_ID]['deployment_state'], 'delete_failed')
        self.assertEqual(recovered.jobs[JOB_ID]['events'][-1]['stage'], 'retire_interrupted')


if __name__ == '__main__':
    unittest.main()
