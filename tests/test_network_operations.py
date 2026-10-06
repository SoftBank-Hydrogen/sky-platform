import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.aws import AwsConfigurationError, AwsSettings
from sky_platform.runtime.aws_network import ServiceNetworkRequest
from sky_platform.runtime.network_operations import NetworkOperations


class NetworkOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'network-operations'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012')
        self.request = ServiceNetworkRequest('demo-app', '123456789012',
                                             'ap-northeast-2', 'vpc-12345678')
        self.manager = NetworkOperations(self.root, self.settings)
        self.preview = {'application_id': 'demo-app', 'account': '123456789012',
                        'region': 'ap-northeast-2', 'vpc_id': 'vpc-12345678',
                        'stack_name': 'sky-network-demo-app', 'initial_ingress': []}

    def test_plan_is_read_only_and_creation_is_journaled_before_worker(self):
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.preflight',
                   return_value=self.preview) as preflight, \
                patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.create') as create, \
                patch('sky_platform.runtime.network_operations.threading.Thread.start') as start:
            plan = self.manager.plan(self.request)
            create.assert_not_called()
            operation = self.manager.start('demo-app', plan['plan_id'])
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(operation['status'], 'running')
        self.assertEqual(json.loads((self.root / 'demo-app.json').read_text())['status'], 'running')
        restored = NetworkOperations(self.root, self.settings)
        self.assertEqual(restored.get('demo-app')['status'], 'needs_attention')
        with self.assertRaisesRegex(ValueError, '이미'):
            restored.plan(self.request)

    def test_changed_plan_never_starts_creation(self):
        changed = {**self.preview, 'vpc_id': 'vpc-87654321'}
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.preflight',
                   side_effect=[self.preview, changed]), \
                patch('sky_platform.runtime.network_operations.threading.Thread.start') as start:
            plan = self.manager.plan(self.request)
            with self.assertRaisesRegex(ValueError, '변경'):
                self.manager.start('demo-app', plan['plan_id'])
        self.assertFalse((self.root / 'demo-app.json').exists())
        start.assert_not_called()

    def test_successful_worker_persists_stack_identity(self):
        stack_id = ('arn:aws:cloudformation:ap-northeast-2:123456789012:'
                    'stack/sky-network-demo-app/id')
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.preflight',
                   return_value=self.preview), \
                patch('sky_platform.runtime.network_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.create',
                   return_value={'stack_id': stack_id, 'service_security_group': 'sg-33333333'}):
            self.manager._run('demo-app')
        self.assertEqual(self.manager.get('demo-app')['stack_id'], stack_id)
        restored = NetworkOperations(self.root, self.settings)
        self.assertEqual(restored.get('demo-app')['stack_id'], stack_id)

    def test_fixed_group_mode_does_not_offer_app_network(self):
        manager = NetworkOperations(self.root, AwsSettings('ap-northeast-2',
            expected_account='123456789012', service_security_group='sg-33333333'))
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.preflight') as preflight:
            with self.assertRaisesRegex(AwsConfigurationError, '고정 서비스 보안 그룹'):
                manager.plan(self.request)
        preflight.assert_not_called()

    def test_reconcile_reads_verified_stack_without_creating_again(self):
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.preflight',
                   return_value=self.preview), \
                patch('sky_platform.runtime.network_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        with patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.inspect_current',
                   return_value={'service_security_group': 'sg-33333333',
                                 'stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/sky-network-demo-app/id'}) as inspect, \
                patch('sky_platform.runtime.network_operations.AwsServiceNetworkProvisioner.create') as create:
            result = self.manager.reconcile('demo-app')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['service_security_group'], 'sg-33333333')
        self.assertTrue(result['stack_id'].endswith('/id'))
        inspect.assert_called_once_with()
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
