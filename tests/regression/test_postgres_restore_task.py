import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from database.migrations import collect_sql_migrations
from database.postgres_restore_task import (plan_restore_verifier_task,
    RestoreVerifierRunner, verifier_run_request, verifier_task_definition)


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
APP = 'demo-app'
TARGET = 'sky-restore-demo-app-drill-20261002'
SNAPSHOT = 'sky-demo-app-backup-20261002'
VPC = 'vpc-12345678'
DB_GROUP = 'sg-12345678'
PROBE_GROUP = 'sg-87654321'
SERVICE_GROUP = 'sg-33333333'
SUBNET = 'subnet-11111111'
ROLE = f'arn:aws:iam::{ACCOUNT}:role/sky-db-demo-app-role'
SECRET = f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:rds!db-example-AbCdEf'


class RestoreTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'migrations').mkdir()
        (self.project / 'migrations' / '0001_init.sql').write_text('CREATE TABLE demo(id int);')
        self.settings = AwsSettings(REGION, expected_account=ACCOUNT,
                                    account_pin_required=True,
                                    service_security_group=SERVICE_GROUP)

    def _plan(self, routes=None):
        credentials = {'source_database_id': 'sky-demo-app',
                       'secret_arn': SECRET, 'secret_version_id': 'a' * 32,
                       'ecs_username_value_from': SECRET + ':username::' + 'a' * 32,
                       'ecs_password_value_from': SECRET + ':password::' + 'a' * 32}
        restored = {'target_database_id': TARGET, 'group_id': DB_GROUP,
                    'status': 'available', 'endpoint': 'restore.abcdef.' + REGION + '.rds.amazonaws.com',
                    'port': 5432}
        source = {'database_id': 'sky-demo-app', 'vpc_id': VPC,
                  'subnet_ids': [SUBNET, 'subnet-22222222']}
        database = {'database_id': 'sky-demo-app', 'secret_arn': SECRET,
                    'execution_role_arn': ROLE, 'migration_log_group': '/sky/migrations/demo-app'}
        route_response = {'RouteTables': routes if routes is not None else [
            {'Associations': [{'Main': True}], 'Routes': [
                {'DestinationCidrBlock': '0.0.0.0/0', 'GatewayId': 'igw-12345678',
                 'State': 'active'}]}]}
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['ec2', 'describe-route-tables']:
                return json.dumps(route_response)
            return json.dumps({'logGroups': [{'logGroupName': database['migration_log_group'],
                                              'retentionInDays': 14}]})
        with patch('database.postgres_restore_task.plan_restore_credentials',
                   return_value=credentials), \
                patch('database.postgres_restore_task.RestoreInstance.inspect_for_probe',
                      return_value=restored), \
                patch('database.postgres_restore_task.discover_existing_postgres',
                      return_value=source), \
                patch('database.postgres_restore_task.AwsPostgresProvisioner.inspect_current',
                      return_value=database), \
                patch('database.postgres_restore_task.AwsExpressAdapter.aws',
                      autospec=True, side_effect=aws):
            result = plan_restore_verifier_task(APP, SNAPSHOT, TARGET, self.settings,
                                                VPC, DB_GROUP, PROBE_GROUP, self.project)
        return result, calls

    def test_plans_owned_source_target_secret_and_public_probe_subnet(self):
        plan, calls = self._plan()
        self.assertEqual(plan['subnet_id'], SUBNET)
        self.assertEqual(plan['probe_group_id'], PROBE_GROUP)
        self.assertEqual(plan['execution_role_arn'], ROLE)
        self.assertEqual(plan['password_value_from'], SECRET + ':password::' + 'a' * 32)
        self.assertEqual(plan['migration_count'], 1)
        self.assertEqual(calls, [['ec2', 'describe-route-tables'],
                                 ['logs', 'describe-log-groups']])

    def test_rejects_subnet_without_internet_route(self):
        with self.assertRaisesRegex(AwsConfigurationError, '공개 ECS 서브넷'):
            self._plan(routes=[{'Associations': [{'Main': True}], 'Routes': []}])

    def test_task_definition_pins_image_secret_and_probe_group(self):
        plan, _ = self._plan()
        attempt = 'b' * 16
        digest = 'sha256:' + 'c' * 64
        definition = verifier_task_definition(plan, digest, attempt, 'd' * 32)
        container = definition['containerDefinitions'][0]
        self.assertEqual(container['image'], plan['repository'] + '@' + digest)
        self.assertEqual(container['secrets'][1]['valueFrom'], plan['password_value_from'])
        self.assertEqual(container['environment'][-1],
                         {'name': 'SKY_RESTORE_MARKER_ID', 'value': 'd' * 32})
        self.assertNotIn('password', json.dumps(container['environment']).lower())
        arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{definition["family"]}:1'
        run = verifier_run_request(plan, arn, attempt)
        self.assertEqual(run['clientToken'], 'sky-restore-verify-' + attempt)
        self.assertEqual(run['networkConfiguration']['awsvpcConfiguration']['securityGroups'],
                         [PROBE_GROUP])
        self.assertEqual(run['networkConfiguration']['awsvpcConfiguration']['subnets'], [SUBNET])
        with self.assertRaisesRegex(ValueError, 'ARN'):
            verifier_run_request(plan, arn.replace(attempt, 'e' * 16), attempt)

    def test_runner_verifies_image_task_tags_and_sql_log_before_cleanup(self):
        plan, _ = self._plan()
        attempt = 'b' * 16
        digest = 'sha256:' + 'c' * 64
        adapter = AwsExpressAdapter(lambda *_: None, self.settings)
        runner = RestoreVerifierRunner(adapter, plan, attempt, digest)
        definition_arn = (f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/'
                          f'sky-restore-verify-{attempt}:1')
        task_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'a' * 32
        task = {'taskArn': task_arn, 'taskDefinitionArn': definition_arn,
                'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                'launchType': 'FARGATE', 'lastStatus': 'STOPPED',
                'tags': [{'key': 'sky-managed', 'value': 'true'},
                         {'key': 'sky-app', 'value': APP},
                         {'key': 'sky-restore-target', 'value': TARGET},
                         {'key': 'sky-attempt', 'value': attempt}],
                'containers': [{'name': 'verify', 'exitCode': 0}]}
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['ecr', 'describe-images']:
                return json.dumps({'imageDetails': [{'registryId': ACCOUNT,
                    'repositoryName': 'sky-managed',
                    'imageTags': [runner.tag], 'imageDigest': digest}]})
            if args[:2] == ['ecs', 'register-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': definition_arn}})
            if args[:2] == ['ecs', 'run-task']:
                return json.dumps({'tasks': [{'taskArn': task_arn}], 'failures': []})
            if args[:2] == ['ecs', 'describe-tasks']:
                return json.dumps({'tasks': [task]})
            if args[:2] == ['logs', 'get-log-events']:
                if '--next-token' not in args:
                    return json.dumps({'events': [{'message': json.dumps({
                        'status': 'passed', 'migration_count': 1,
                        'marker_checked': False})}], 'nextForwardToken': 'token-1'})
                return json.dumps({'events': [], 'nextForwardToken': 'token-1'})
            if args[:2] == ['ecs', 'deregister-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': definition_arn,
                                                       'status': 'INACTIVE'}})
            if args[:2] == ['ecr', 'batch-delete-image']:
                return json.dumps({'imageIds': [{'imageTag': runner.tag}], 'failures': []})
            raise AssertionError(args)
        with patch.object(adapter, 'aws', side_effect=aws):
            self.assertEqual(runner.register(), definition_arn)
            self.assertEqual(runner.launch(), task_arn)
            result = runner.inspect_result(task_arn, definition_arn)
            self.assertEqual(result['migration_count'], 1)
            self.assertEqual(runner.cleanup_completed(result)['status'], 'cleaned')
        self.assertIn(['logs', 'get-log-events'], calls)
        task['tags'][1]['value'] = 'another-app'
        with patch.object(adapter, 'aws', side_effect=aws):
            with self.assertRaisesRegex(AwsConfigurationError, '소유권'):
                runner.inspect(task_arn, definition_arn)

    def test_runner_rejects_failed_sql_result_and_uncertain_cleanup(self):
        plan, _ = self._plan()
        runner = RestoreVerifierRunner(AwsExpressAdapter(lambda *_: None, self.settings),
                                       plan, 'b' * 16, 'sha256:' + 'c' * 64)
        with self.assertRaisesRegex(AwsConfigurationError, '성공'):
            runner.cleanup_completed({'status': 'failed'})

    def test_build_pushes_staged_verifier_and_pins_returned_digest(self):
        plan, _ = self._plan()
        attempt = 'b' * 16
        digest = 'sha256:' + 'c' * 64
        adapter = AwsExpressAdapter(lambda *_: None, self.settings)
        runner = RestoreVerifierRunner(adapter, plan, attempt)
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['ecr', 'list-images']:
                return json.dumps({'imageIds': []})
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            return json.dumps({'imageDetails': [{'registryId': ACCOUNT,
                'repositoryName': 'sky-managed', 'imageTags': [runner.tag],
                'imageDigest': digest}]})
        docker_calls = []
        def command(args, **_kwargs):
            docker_calls.append(args)
            return 'unix:///var/run/docker.sock' if args[:3] == [
                'docker', 'context', 'inspect'] else ''
        with patch.object(adapter, 'aws', side_effect=aws), \
                patch.object(adapter, 'command', side_effect=command):
            image = runner.build_and_push(collect_sql_migrations(self.project))
        self.assertEqual(image, plan['repository'] + ':' + runner.tag)
        self.assertEqual(runner.image_digest, digest)
        self.assertEqual(runner.definition['containerDefinitions'][0]['image'],
                         plan['repository'] + '@' + digest)
        self.assertEqual(calls, [['ecr', 'list-images'],
                                 ['ecr', 'get-login-password'],
                                 ['ecr', 'describe-images']])
        self.assertTrue(any(call[:2] == ['docker', 'build'] for call in docker_calls))
        self.assertTrue(any('push' in call for call in docker_calls))


if __name__ == '__main__':
    unittest.main()
