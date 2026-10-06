import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from planning.analysis import AISettings
from deployment.aws import AwsSettings
from api.server import App, handler_for


class NetworkServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012')
        self.app = App(self.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.settings, monitor_interval=0)

    def request(self, suffix, payload=None, token=True):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/applications/demo-app/network/' + suffix
        body = json.dumps(payload).encode() if payload is not None else b''
        handler.headers = {'X-Sky-Token': self.app.token if token else 'wrong',
                           'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.json_response = Mock()
        handler.do_GET() if suffix == 'operation' else handler.do_POST()
        return handler.json_response.call_args.args

    def test_plan_and_create_require_session_and_reviewed_token(self):
        preview = {'application_id': 'demo-app', 'account': '123456789012',
                   'region': 'ap-northeast-2', 'vpc_id': 'vpc-12345678',
                   'stack_name': 'sky-network-demo-app', 'initial_ingress': []}
        with patch('deployment.network_operations.AwsServiceNetworkProvisioner.preflight',
                   return_value=preview), \
                patch('deployment.network_operations.threading.Thread.start') as start, \
                patch('deployment.network_operations.AwsServiceNetworkProvisioner.create') as create:
            status, _ = self.request('plan', {'vpc_id': 'vpc-12345678'}, token=False)
            self.assertEqual(status, 403)
            status, plan = self.request('plan', {'vpc_id': 'vpc-12345678'})
            self.assertEqual(status, 200)
            self.assertTrue(plan['plan_id'])
            create.assert_not_called()
            status, _ = self.request('create', {'plan_id': 'x' * 32})
            self.assertEqual(status, 400)
            status, operation = self.request('create', {'plan_id': plan['plan_id']})
            self.assertEqual(status, 202)
            self.assertEqual(operation['status'], 'running')
        start.assert_called_once_with()
        self.assertTrue((self.root / 'network-operations' / 'demo-app.json').exists())
        self.assertEqual(self.request('operation')[0], 200)

    def test_default_network_get_requires_session_and_only_reads(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/aws/default-network'
        handler.headers = {}
        handler.json_response = Mock()
        with patch('api.server.discover_default_network') as discover:
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 403)
        discover.assert_not_called()
        handler.headers = {'X-Sky-Token': self.app.token}
        expected = {'account': '123456789012', 'region': 'ap-northeast-2',
                    'vpc_id': 'vpc-12345678', 'subnet_ids': ['subnet-11111111', 'subnet-22222222']}
        with patch('api.server.discover_default_network', return_value=expected) as discover:
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args, (200, expected))
        discover.assert_called_once_with(self.settings)


if __name__ == '__main__':
    unittest.main()
