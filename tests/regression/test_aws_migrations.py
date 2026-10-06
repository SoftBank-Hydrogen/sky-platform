import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from deployment.aws_migrations import (AwsMigrationRunner, cleanup_interrupted_migration,
                                     inspect_migration_task, main)
from database.migrations import collect_sql_migrations
from database.postgres import PostgresRequest


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
ATTEMPT = 'a' * 16 + '-a1'
TASK_DEF = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-migrate-{ATTEMPT}:1'
TASK = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + '1234567890abcdef' * 2
IMAGE_DIGEST = 'sha256:' + 'a' * 64


class AwsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        project = Path(self.temp.name)
        (project / 'migrations').mkdir()
        (project / 'migrations' / '0001_init.sql').write_text('CREATE TABLE demo (id int);')
        self.request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
                                       ('subnet-11111111', 'subnet-22222222'), 'sg-33333333')
        self.database = {'service_security_group': self.request.service_security_group,
                         'migration_log_group': '/sky/migrations/demo-app',
                         'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/db-role',
                         'endpoint': f'db.{REGION}.rds.amazonaws.com', 'port': 5432,
                         'secret_arn': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:managed-id'}
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(REGION, expected_account=ACCOUNT))
        self.runner = AwsMigrationRunner(self.adapter, self.request, self.database,
            collect_sql_migrations(project),
            f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed', ATTEMPT)

    def test_preflight_selects_public_subnet_and_checks_log_retention(self):
        def aws(args, **_kwargs):
            if args[:2] == ['ec2', 'describe-route-tables']:
                return json.dumps({'RouteTables': [
                    {'Associations': [{'Main': True}], 'Routes': []},
                    {'Associations': [{'SubnetId': self.request.subnet_ids[1]}], 'Routes': [
                        {'DestinationCidrBlock': '0.0.0.0/0', 'GatewayId': 'igw-12345678',
                         'State': 'active'}]}]})
            return json.dumps({'logGroups': [{'logGroupName': self.database['migration_log_group'],
                                              'retentionInDays': 14}]})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.runner.preflight(), self.request.subnet_ids[1])
        with self.assertRaisesRegex(AwsConfigurationError, 'digest'):
            self.runner.task_definition()
        self.runner.image_digest = IMAGE_DIGEST
        self.assertEqual(self.runner.task_definition()['containerDefinitions'][0]['image'],
                         self.runner.repository + '@' + IMAGE_DIGEST)
        self.assertEqual(self.runner.task_definition()['containerDefinitions'][0]['secrets'][1],
                         {'name': 'PGPASSWORD', 'valueFrom': self.database['secret_arn'] + ':password::'})
        self.assertNotIn('password', json.dumps(self.runner.task_definition()['containerDefinitions'][0]['environment']))

    def test_run_task_verifies_owned_exit_zero(self):
        self.runner.subnet = self.request.subnet_ids[0]
        self.runner.image_digest = IMAGE_DIGEST
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        tags = [{'key': 'sky-managed', 'value': 'true'},
                {'key': 'sky-app', 'value': 'demo-app'},
                {'key': 'sky-attempt', 'value': ATTEMPT}]
        task = {'taskArn': TASK, 'taskDefinitionArn': TASK_DEF, 'lastStatus': 'STOPPED',
                'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                'launchType': 'FARGATE',
                'tags': tags, 'containers': [{'name': 'migration', 'exitCode': 0}]}
        def aws(args, **_kwargs):
            if '--cli-input-json' in args:
                file = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                payload = json.loads(file.read_text())
                if args[:2] == ['ecs', 'register-task-definition']:
                    self.assertEqual(payload['executionRoleArn'], self.database['execution_role_arn'])
                    self.assertEqual(payload['containerDefinitions'][0]['image'],
                                     self.runner.repository + '@' + IMAGE_DIGEST)
                    return json.dumps({'taskDefinition': {'taskDefinitionArn': TASK_DEF}})
                self.assertEqual(payload['networkConfiguration']['awsvpcConfiguration'], {
                    'subnets': [self.request.subnet_ids[0]],
                    'securityGroups': [self.request.service_security_group], 'assignPublicIp': 'ENABLED'})
                return json.dumps({'tasks': [{'taskArn': TASK}], 'failures': []})
            if args[:2] == ['ecs', 'wait']:
                return ''
            if args[:2] == ['ecs', 'describe-tasks']:
                return json.dumps({'tasks': [task]})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws):
            result = self.runner.run_task()
            self.assertEqual(result['task_arn'], TASK)
            self.assertEqual(result['image_digest'], IMAGE_DIGEST)
            self.assertTrue(self.runner.completed)
            self.assertEqual([item['aws_migration_status'] for item in checkpoints],
                             ['registered', 'running'])
            self.assertEqual(checkpoints[1]['aws_migration_task_arn'], TASK)
            task['containers'][0]['exitCode'] = 1
            with self.assertRaisesRegex(AwsConfigurationError, '마이그레이션이 실패'):
                self.runner.run_task()
            self.assertFalse(self.runner.completed)

    def test_read_only_inspection_distinguishes_running_failed_and_expired(self):
        task = {'taskArn': TASK, 'taskDefinitionArn': TASK_DEF,
                'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                'launchType': 'FARGATE', 'lastStatus': 'RUNNING',
                'tags': [{'key': 'sky-managed', 'value': 'true'},
                         {'key': 'sky-app', 'value': 'demo-app'},
                         {'key': 'sky-attempt', 'value': ATTEMPT}],
                'containers': [{'name': 'migration'}]}
        args = (self.adapter, 'demo-app', ACCOUNT, REGION, ATTEMPT, TASK, TASK_DEF)
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})) as aws:
            self.assertEqual(inspect_migration_task(*args)['status'], 'running')
            aws.assert_called_once()
            self.assertEqual(aws.call_args.args[0][:2], ['ecs', 'describe-tasks'])
        task['lastStatus'] = 'STOPPED'
        task['containers'][0]['exitCode'] = 1
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'failed')
        task['containers'][0].pop('exitCode')
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'unknown')
        task['containers'][0]['exitCode'] = False
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'unknown')
        task['containers'] = []
        task['stopCode'] = 'TaskFailedToStart'
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'failed')
        task['tags'][1]['value'] = 'another-app'
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [task]})):
            with self.assertRaisesRegex(AwsConfigurationError, '소유권'):
                inspect_migration_task(*args)
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'tasks': [], 'failures': [
                {'arn': TASK, 'reason': 'MISSING'}]})):
            self.assertEqual(inspect_migration_task(*args)['status'], 'unknown')
        with patch.object(self.adapter, 'aws') as aws:
            with self.assertRaisesRegex(ValueError, 'ARN'):
                inspect_migration_task(*args[:-2], TASK + '-wrong', TASK_DEF)
            aws.assert_not_called()

    def test_inspect_command_pins_account_before_describing_task(self):
        arguments = ['--application', 'demo-app', '--account', ACCOUNT, '--region', REGION,
                     '--attempt', ATTEMPT, '--task-arn', TASK,
                     '--task-definition-arn', TASK_DEF]
        with patch.object(AwsExpressAdapter, 'aws', return_value=json.dumps({'Account': '000000000000'})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '현재 AWS 계정'):
                main(arguments)
            self.assertEqual(aws.call_count, 1)
        def aws(args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'tasks': [], 'failures': [{'arn': TASK, 'reason': 'MISSING'}]})
        output = io.StringIO()
        with patch.object(AwsExpressAdapter, 'aws', side_effect=aws), redirect_stdout(output):
            main(arguments)
        self.assertEqual(json.loads(output.getvalue())['status'], 'unknown')

    def test_interrupted_cleanup_verifies_task_definition_and_image_before_deleting(self):
        image = self.runner.image
        task = {'taskArn': TASK, 'taskDefinitionArn': TASK_DEF,
                'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                'launchType': 'FARGATE', 'lastStatus': 'STOPPED',
                'tags': [{'key': 'sky-managed', 'value': 'true'},
                         {'key': 'sky-app', 'value': 'demo-app'},
                         {'key': 'sky-attempt', 'value': ATTEMPT}],
                'containers': [{'name': 'migration', 'exitCode': 0}]}
        definition = {'taskDefinition': {'taskDefinitionArn': TASK_DEF,
            'family': f'sky-migrate-{ATTEMPT}', 'status': 'ACTIVE',
            'containerDefinitions': [{'name': 'migration',
                'image': self.runner.repository + '@' + IMAGE_DIGEST}]},
            'tags': task['tags']}
        present = {'images': [{'registryId': ACCOUNT, 'repositoryName': 'sky-managed',
            'imageId': {'imageTag': ATTEMPT + '-db', 'imageDigest': IMAGE_DIGEST}}],
            'failures': []}
        absent = {'images': [], 'failures': [{'imageId': {'imageTag': ATTEMPT + '-db'},
            'failureCode': 'ImageNotFound', 'failureReason': 'Requested image not found'}]}
        calls = []
        checkpoints = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['ecs', 'describe-tasks']:
                return json.dumps({'tasks': [task]})
            if args[:2] == ['ecs', 'list-tasks']:
                return json.dumps({'taskArns': []})
            if args[:2] == ['ecs', 'describe-task-definition']:
                return json.dumps(definition)
            if args[:2] == ['ecr', 'batch-get-image']:
                return json.dumps(absent if ['ecr', 'batch-delete-image'] in calls else present)
            if args[:2] == ['ecs', 'deregister-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': TASK_DEF,
                                                      'status': 'INACTIVE'}})
            if args[:2] == ['ecr', 'batch-delete-image']:
                return json.dumps({'imageIds': [{'imageTag': ATTEMPT + '-db',
                                                 'imageDigest': IMAGE_DIGEST}], 'failures': []})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws):
            result = cleanup_interrupted_migration(self.adapter, self.request, ATTEMPT,
                TASK, TASK_DEF, image, IMAGE_DIGEST,
                checkpoint=lambda: checkpoints.append('definition_inactive'))
        self.assertEqual(result['state'], 'done')
        self.assertTrue(result['image_deleted'])
        self.assertEqual(checkpoints, ['definition_inactive'])
        self.assertLess(calls.index(['ecs', 'describe-tasks']),
                        calls.index(['ecs', 'deregister-task-definition']))
        self.assertLess(calls.index(['ecs', 'deregister-task-definition']),
                        calls.index(['ecr', 'batch-delete-image']))
        self.assertEqual(calls[-1], ['ecr', 'batch-get-image'])
        calls.clear()
        def swapped_image(args, **kwargs):
            if args[:2] == ['ecr', 'batch-get-image']:
                changed = json.loads(json.dumps(present))
                changed['images'][0]['imageId']['imageDigest'] = 'sha256:' + 'b' * 64
                return json.dumps(changed)
            return aws(args, **kwargs)
        with patch.object(self.adapter, 'aws', side_effect=swapped_image) as commands:
            with self.assertRaisesRegex(AwsConfigurationError, 'digest'):
                cleanup_interrupted_migration(self.adapter, self.request, ATTEMPT,
                    TASK, TASK_DEF, image, IMAGE_DIGEST)
        self.assertNotIn(['ecs', 'deregister-task-definition'],
                         [call.args[0][:2] for call in commands.call_args_list])
        self.assertNotIn(['ecr', 'batch-delete-image'],
                         [call.args[0][:2] for call in commands.call_args_list])

    def test_interrupted_cleanup_refuses_running_or_unowned_artifacts(self):
        def running(args, **_kwargs):
            if args[:2] == ['ecs', 'describe-tasks']:
                return json.dumps({'tasks': [{'taskArn': TASK, 'taskDefinitionArn': TASK_DEF,
                    'clusterArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/default',
                    'launchType': 'FARGATE', 'lastStatus': 'STOPPED',
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-app', 'value': 'demo-app'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}],
                    'containers': [{'name': 'migration', 'exitCode': 0}]}]})
            if args[:2] == ['ecs', 'list-tasks']:
                return json.dumps({'taskArns': [TASK]})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=running) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '실행 중'):
                cleanup_interrupted_migration(self.adapter, self.request, ATTEMPT,
                    TASK, TASK_DEF, self.runner.image, IMAGE_DIGEST)
        self.assertNotIn(['ecs', 'deregister-task-definition'],
                         [call.args[0][:2] for call in aws.call_args_list])
        with patch.object(self.adapter, 'aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '이미지 소유'):
                cleanup_interrupted_migration(self.adapter, self.request, ATTEMPT,
                    TASK, TASK_DEF, self.runner.image + '-wrong', IMAGE_DIGEST)
        aws.assert_not_called()

    def test_interrupted_cleanup_retry_accepts_journaled_inactive_definition(self):
        def aws(args, **_kwargs):
            if args[:2] == ['ecs', 'list-tasks']:
                return json.dumps({'taskArns': []})
            if args[:2] == ['ecr', 'batch-get-image']:
                return json.dumps({'images': [], 'failures': [
                    {'imageId': {'imageTag': ATTEMPT + '-db'},
                     'failureCode': 'ImageNotFound'}]})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws) as called:
            result = cleanup_interrupted_migration(self.adapter, self.request, ATTEMPT,
                TASK, TASK_DEF, self.runner.image, IMAGE_DIGEST, definition_inactive=True)
        self.assertEqual(result['state'], 'done')
        self.assertFalse(result['image_deleted'])
        self.assertEqual([call.args[0][:2] for call in called.call_args_list],
                         [['ecs', 'list-tasks'], ['ecr', 'batch-get-image']])

    def test_journaled_success_can_cleanup_after_task_lookup_expires(self):
        def aws(args, **_kwargs):
            if args[:2] == ['ecs', 'list-tasks']:
                return json.dumps({'taskArns': []})
            if args[:2] == ['ecs', 'describe-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': TASK_DEF,
                    'family': f'sky-migrate-{ATTEMPT}', 'status': 'ACTIVE',
                    'containerDefinitions': [{'name': 'migration',
                        'image': self.runner.repository + '@' + IMAGE_DIGEST}]},
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-app', 'value': 'demo-app'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}]})
            if args[:2] == ['ecr', 'batch-get-image']:
                return json.dumps({'images': [], 'failures': [
                    {'imageId': {'imageTag': ATTEMPT + '-db'},
                     'failureCode': 'ImageNotFound'}]})
            if args[:2] == ['ecs', 'deregister-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': TASK_DEF,
                                                       'status': 'INACTIVE'}})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws) as called:
            result = cleanup_interrupted_migration(self.adapter, self.request, ATTEMPT,
                TASK, TASK_DEF, self.runner.image, IMAGE_DIGEST, verified_outcome=True)
        self.assertEqual(result['state'], 'done')
        self.assertEqual([call.args[0][:2] for call in called.call_args_list], [
            ['ecs', 'list-tasks'], ['ecs', 'describe-task-definition'],
            ['ecr', 'batch-get-image'], ['ecs', 'deregister-task-definition']])

    def test_uncertain_task_result_is_recorded_and_stops_retry(self):
        self.runner.subnet = self.request.subnet_ids[0]
        self.runner.registered_arn = TASK_DEF
        self.runner.task_arn = TASK
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        def command(args, **_kwargs):
            return 'unix:///var/run/docker.sock' if args[:3] == ['docker', 'context', 'inspect'] else ''
        def aws(args, **_kwargs):
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            return self.image_details()
        with patch.object(self.adapter, 'command', side_effect=command), \
                patch.object(self.adapter, 'aws', side_effect=aws), \
                patch.object(self.runner, 'run_task', side_effect=RuntimeError('wait timed out')):
            with self.assertRaises(AwsConfigurationError) as error:
                self.runner.build_and_run()
        self.assertFalse(error.exception.retryable)
        self.assertIn(TASK, str(error.exception))
        self.assertEqual(checkpoints[-1]['aws_migration_status'], 'needs_attention')

    def test_success_is_checkpointed_before_cleanup(self):
        self.runner.subnet = self.request.subnet_ids[0]
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        def command(args, **_kwargs):
            return 'unix:///var/run/docker.sock' if args[:3] == ['docker', 'context', 'inspect'] else ''
        def aws(args, **_kwargs):
            return 'synthetic-password' if args[:2] == ['ecr', 'get-login-password'] else self.image_details()
        with patch.object(self.adapter, 'command', side_effect=command), \
                patch.object(self.adapter, 'aws', side_effect=aws), \
                patch.object(self.runner, 'run_task', return_value={'task_arn': TASK,
                    'bundle_digest': self.runner.bundle.digest}) as run, \
                patch.object(self.runner, 'cleanup_completed', return_value=True) as cleanup:
            result = self.runner.build_and_run()
        run.assert_called_once_with()
        cleanup.assert_called_once_with()
        self.assertEqual(result['cleanup_complete'], True)
        self.assertEqual([item['aws_migration_status'] for item in checkpoints],
                         ['image_pushed', 'image_verified', 'succeeded'])
        self.assertEqual(checkpoints[1]['aws_migration_image_digest'], IMAGE_DIGEST)
        self.assertEqual(checkpoints[2]['aws_migration_result']['task_arn'], TASK)

    def image_details(self):
        return json.dumps({'imageDetails': [{'registryId': ACCOUNT,
            'repositoryName': 'sky-managed', 'imageTags': [ATTEMPT + '-db'],
            'imageDigest': IMAGE_DIGEST}]})

    def test_image_digest_requires_owned_ecr_tag(self):
        with patch.object(self.adapter, 'aws', return_value=self.image_details()):
            self.assertEqual(self.runner.verify_pushed_image(), IMAGE_DIGEST)
        self.runner.image_digest = None
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'imageDetails': [
                {'registryId': ACCOUNT, 'repositoryName': 'sky-managed',
                 'imageTags': ['different'], 'imageDigest': IMAGE_DIGEST}]})):
            with self.assertRaisesRegex(AwsConfigurationError, 'digest'):
                self.runner.verify_pushed_image()
        self.assertIsNone(self.runner.image_digest)

    def test_cleanup_only_known_completed_task_artifacts(self):
        with patch.object(self.adapter, 'aws') as aws:
            self.assertFalse(self.runner.cleanup_completed())
            aws.assert_not_called()
        self.runner.registered_arn = TASK_DEF
        self.runner.task_arn = TASK
        with patch.object(self.adapter, 'aws') as aws:
            self.assertFalse(self.runner.cleanup_completed())
            aws.assert_not_called()
        self.runner.completed = True
        self.runner.image_digest = IMAGE_DIGEST
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        def cleaned(_adapter, _request, _attempt, _task, _definition, _image, _digest,
                    *, checkpoint):
            checkpoint()
            return {'image_deleted': True}
        with patch('deployment.aws_migrations.cleanup_interrupted_migration',
                   side_effect=cleaned) as cleanup:
            self.assertTrue(self.runner.cleanup_completed())
            self.assertFalse(self.runner.completed)
        self.assertEqual(cleanup.call_args.args[2:],
                         (ATTEMPT, TASK, TASK_DEF, self.runner.image, IMAGE_DIGEST))
        self.assertEqual(checkpoints, [
            {'aws_migration_cleanup_definition_inactive': True},
            {'aws_migration_cleanup_state': 'done',
             'aws_migration_cleanup_image_deleted': True}])

    def test_cleanup_records_inactive_definition_before_image_deletion_failure(self):
        self.runner.registered_arn = TASK_DEF
        self.runner.task_arn = TASK
        self.runner.completed = True
        self.runner.image_digest = IMAGE_DIGEST
        checkpoints = []
        self.adapter.checkpoint = lambda **fields: checkpoints.append(fields)
        def interrupted(_adapter, _request, _attempt, _task, _definition, _image, _digest,
                        *, checkpoint):
            checkpoint()
            raise RuntimeError('ECR unavailable')
        with patch('deployment.aws_migrations.cleanup_interrupted_migration',
                   side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'ECR unavailable'):
                self.runner.cleanup_completed()
        self.assertEqual(checkpoints, [{'aws_migration_cleanup_definition_inactive': True}])


if __name__ == '__main__':
    unittest.main()
