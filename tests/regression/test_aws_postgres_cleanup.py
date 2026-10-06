"""Guardrails for the opt-in disposable database cleanup drill."""
import unittest
from unittest.mock import patch

from sky_platform.deployment.aws import AwsConfigurationError
from sky_platform.database.postgres import PostgresRequest
from tests.live.smoke_aws_postgres_cleanup import _db_users, plan


class PostgresCleanupTests(unittest.TestCase):
    def setUp(self):
        self.request = PostgresRequest('dbdrill-1234abcd', '123456789012',
                                       'ap-northeast-2', 'vpc-12345678',
                                       ('subnet-12345678', 'subnet-87654321'),
                                       'sg-12345678')
        self.database = {'database_id': self.request.database_id,
                         'database_arn': 'arn:aws:rds:ap-northeast-2:123456789012:db:'
                                         + self.request.database_id,
                         'secret_arn': 'arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbdrill',
                         'stack_id': 'arn:aws:cloudformation:ap-northeast-2:123456789012:'
                                     'stack/sky-db-dbdrill-1234abcd/abc'}

    def test_refuses_non_disposable_application_before_aws_call(self):
        with patch('tests.live.smoke_aws_postgres_cleanup.AwsPostgresProvisioner') as provisioner:
            with self.assertRaisesRegex(ValueError, 'dbdrill'):
                plan(PostgresRequest('demo-app', self.request.account, self.request.region,
                                     self.request.vpc_id, self.request.subnet_ids,
                                     self.request.service_security_group))
            provisioner.assert_not_called()

    def test_refuses_manual_snapshot(self):
        def response(_provisioner, args):
            if args[1] == 'list-tags-for-resource':
                return {'TagList': [{'Key': 'sky-managed', 'Value': 'true'},
                                    {'Key': 'sky-app', 'Value': self.request.application_id}]}
            if args[1] == 'describe-db-snapshots':
                return {'DBSnapshots': [{'DBSnapshotIdentifier': 'keep-me'}]}
            self.fail('Unexpected AWS call')
        with patch('tests.live.smoke_aws_postgres_cleanup.AwsPostgresProvisioner') as constructor, \
                patch('tests.live.smoke_aws_postgres_cleanup._response', side_effect=response):
            constructor.return_value.inspect_current.return_value = self.database
            with self.assertRaisesRegex(AwsConfigurationError, '수동 스냅샷'):
                plan(self.request)

    def test_detects_running_task_using_database_secret(self):
        secret = self.database['secret_arn']
        task_arn = 'arn:aws:ecs:ap-northeast-2:123456789012:task/default/abc'
        definition_arn = 'arn:aws:ecs:ap-northeast-2:123456789012:task-definition/probe:1'
        def response(_provisioner, args):
            if args[1] == 'list-services':
                return {'serviceArns': []}
            if args[1] == 'list-tasks':
                return {'taskArns': [task_arn]}
            if args[1] == 'describe-tasks':
                return {'failures': [], 'tasks': [{'taskArn': task_arn,
                                                  'taskDefinitionArn': definition_arn}]}
            if args[1] == 'describe-task-definition':
                return {'taskDefinition': {'taskDefinitionArn': definition_arn,
                    'containerDefinitions': [{'secrets': [{'valueFrom': secret + ':password::'}]}]}}
            self.fail('Unexpected AWS call')
        with patch('tests.live.smoke_aws_postgres_cleanup._response', side_effect=response):
            self.assertEqual(_db_users(object(), secret), [task_arn])


if __name__ == '__main__':
    unittest.main()
