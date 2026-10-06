"""Safety checks for the opt-in Chrome RDS retirement drill cleanup."""
import json
import unittest
from unittest.mock import patch

from sky_platform.runtime.aws import AwsSettings
from sky_platform.runtime.postgres import PostgresRequest
from tests.smoke_aws_postgres_create_browser import (
    deployment_cleanup_blocker, main, retire_final_snapshot)


class BrowserRetirementCleanupTests(unittest.TestCase):
    def setUp(self):
        self.application = 'dbdrill-1234abcd'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                                    account_pin_required=True)
        self.request = PostgresRequest(self.application, '123456789012', 'ap-northeast-2',
                                       'vpc-12345678', ('subnet-11111111', 'subnet-22222222'),
                                       'sg-33333333')
        self.operation = {'application_id': self.application,
                          'database_id': 'sky-' + self.application,
                          'snapshot_id': 'sky-' + self.application + '-final-123456abcdef',
                          'stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/sky-db-dbdrill-1234abcd/id',
                          'status': 'succeeded', 'stage': 'stack_deleted'}

    def test_refuses_incomplete_retirement_before_any_aws_call(self):
        self.operation['status'] = 'needs_attention'
        with patch('tests.smoke_aws_postgres_create_browser.inspect_snapshot') as inspect:
            with self.assertRaises(AssertionError):
                retire_final_snapshot(self.application, self.operation,
                                      self.settings, self.request)
        inspect.assert_not_called()

    def test_refuses_unconfirmed_stack_before_deleting_snapshot(self):
        class FakeAdapter:
            def aws(self, args, **_kwargs):
                if args[:2] == ['cloudformation', 'describe-stacks']:
                    return json.dumps({'Stacks': [{'StackId': self_stack,
                                                   'StackStatus': 'DELETE_FAILED'}]})
                raise AssertionError('unexpected AWS call')

        class FakeProvisioner:
            def __init__(self, _request):
                self.adapter = FakeAdapter()

        self_stack = self.operation['stack_id']
        snapshot = {'snapshot_id': self.operation['snapshot_id'], 'status': 'available',
                    'encrypted': True, 'snapshot_arn': 'arn:aws:rds:example'}
        with patch('tests.smoke_aws_postgres_create_browser.inspect_snapshot',
                   return_value=snapshot), \
             patch('tests.smoke_aws_postgres_create_browser.AwsPostgresProvisioner',
                   FakeProvisioner):
            with self.assertRaisesRegex(AssertionError, 'DELETE_COMPLETE'):
                retire_final_snapshot(self.application, self.operation,
                                      self.settings, self.request)

    def test_one_action_drill_without_apply_stops_after_read_only_preflight(self):
        network = {'vpc_id': 'vpc-12345678'}
        with patch('tests.smoke_aws_postgres_create_browser.discover_default_network',
                   return_value=network), \
             patch('tests.smoke_aws_postgres_create_browser.AwsServiceNetworkProvisioner') as provisioner, \
             patch('tests.smoke_aws_postgres_create_browser.App') as app, \
             patch('tests.smoke_aws_postgres_create_browser.subprocess.run') as command:
            provisioner.return_value.preflight.return_value = {
                'stack_name': 'sky-network-dbdrill-1234abcd'}
            main(['--one-action-deploy', '--application', self.application,
                  '--account', '123456789012', '--region', 'ap-northeast-2'])
        provisioner.return_value.preflight.assert_called_once_with()
        app.assert_not_called()
        command.assert_not_called()

    def test_disposable_database_cleanup_waits_for_deployment_outcome(self):
        for job in ({'status': 'provisioning'}, {'status': 'waiting_input'},
                    {'status': 'running'}, {'status': 'failed', 'attempts': 1},
                    {'status': 'interrupted', 'aws_update_submitted': True},
                    {'status': 'succeeded', 'deployment_state': 'needs_attention'}):
            with self.subTest(job=job):
                self.assertIsNotNone(deployment_cleanup_blocker([job]))
        self.assertIsNone(deployment_cleanup_blocker([]))
        self.assertIsNone(deployment_cleanup_blocker([
            {'status': 'failed', 'attempts': 0},
            {'status': 'succeeded', 'deployment_state': 'deleted'}]))


if __name__ == '__main__':
    unittest.main()
