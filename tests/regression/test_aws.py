import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adapters.aws.ecs import (AwsConfigurationError, AwsExpressAdapter, AwsSettings,
                           database_configuration_matches, postgres_ssl_environment,
                           service_group_ingress_is_restricted)
from application.deployment_core import analyze
from adapters.aws.postgres import PostgresRequest
from adapters.database.migrations import MigrationBundle, SqlMigration


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
SERVICE_GROUP = 'sg-12345678'
ATTEMPT = 'a' * 16 + '-a1'
SERVICE = 'sky-' + ATTEMPT
ARN = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}'
REPOSITORY = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed'


class AwsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
        self.plan = replace(analyze(self.project), target='aws-ecs-express')
        self.events = []
        self.adapter = AwsExpressAdapter(lambda stage, message: self.events.append((stage, message)),
                                         AwsSettings(REGION))

    def test_server_aws_target_requires_pinned_account(self):
        with patch.dict(os.environ, {'SKY_AWS_REGION': REGION,
                                      'SKY_AWS_ACCOUNT_ID': ''}):
            settings = AwsSettings.from_environment()
        self.assertIn('SKY_AWS_ACCOUNT_ID', settings.unavailable_reason())
        with patch.dict(os.environ, {'SKY_AWS_REGION': REGION,
                                      'SKY_AWS_ACCOUNT_ID': ACCOUNT,
                                      'SKY_AWS_SERVICE_SECURITY_GROUP': SERVICE_GROUP}), \
                patch('adapters.aws.ecs.shutil.which', return_value='/usr/bin/tool'):
            settings = AwsSettings.from_environment()
            self.assertIsNone(settings.unavailable_reason())
        self.assertEqual(settings.expected_account, ACCOUNT)
        self.assertEqual(settings.service_security_group, SERVICE_GROUP)
        with self.assertRaisesRegex(AwsConfigurationError, '보안 그룹 ID'):
            AwsSettings(REGION, service_security_group='sg-not-valid').validate()

    def test_wrong_account_stops_before_cloudformation(self):
        self.adapter.settings = AwsSettings(REGION, expected_account='999999999999')
        calls = []
        def aws(args, **kwargs):
            calls.append(args)
            return json.dumps({'Account': ACCOUNT})
        with patch('adapters.aws.ecs.shutil.which', return_value='/usr/bin/tool'), \
                patch.object(self.adapter, 'aws', side_effect=aws):
            with self.assertRaisesRegex(AwsConfigurationError, '리소스를 생성하지 않았습니다'):
                self.adapter.prepare_infrastructure()
        self.assertEqual(calls, [['sts', 'get-caller-identity']])

    def test_service_group_requires_default_vpc_and_no_ingress(self):
        self.adapter.settings = AwsSettings(REGION, service_security_group=SERVICE_GROUP)
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': 'vpc-12345678'}]})
            return json.dumps({'SecurityGroups': [{'GroupId': SERVICE_GROUP,
                'VpcId': 'vpc-12345678', 'IpPermissions': []}]})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.adapter.validate_service_security_group()
        self.assertEqual([args[:2] for args in calls],
                         [['ec2', 'describe-vpcs'], ['ec2', 'describe-security-groups']])
        with patch.object(self.adapter, 'aws', side_effect=lambda args, **kwargs:
                json.dumps({'Vpcs': [{'VpcId': 'vpc-12345678'}]}) if args[:2] == ['ec2', 'describe-vpcs']
                else json.dumps({'SecurityGroups': [{'GroupId': SERVICE_GROUP,
                    'VpcId': 'vpc-12345678', 'IpPermissions': [{'IpProtocol': '-1'}]}]})):
            with self.assertRaisesRegex(AwsConfigurationError, '인바운드'):
                self.adapter.validate_service_security_group()

    def test_service_group_accepts_only_gateway_security_group_on_one_port(self):
        group = {'GroupId': SERVICE_GROUP, 'VpcId': 'vpc-12345678',
                 'OwnerId': ACCOUNT, 'IpPermissions': [{
                     'IpProtocol': 'tcp', 'FromPort': 3000, 'ToPort': 3000,
                     'UserIdGroupPairs': [{'GroupId': 'sg-87654321',
                                           'UserId': ACCOUNT, 'VpcId': 'vpc-12345678'}]}]}
        self.assertTrue(service_group_ingress_is_restricted(group))
        self.adapter.settings = AwsSettings(REGION, service_security_group=SERVICE_GROUP)
        def aws(args, **_kwargs):
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': 'vpc-12345678'}]})
            return json.dumps({'SecurityGroups': [group]})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.adapter.validate_service_security_group()
        for change in ({'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                       {'FromPort': 1, 'ToPort': 65535},
                       {'UserIdGroupPairs': [{'GroupId': SERVICE_GROUP}]},
                       {'UserIdGroupPairs': [{'GroupId': 'sg-87654321',
                                              'UserId': '999999999999'}]}):
            altered = {**group, 'IpPermissions': [{**group['IpPermissions'][0], **change}]}
            self.assertFalse(service_group_ingress_is_restricted(altered))

    def test_custom_group_is_sent_and_verified_on_create(self):
        self.adapter.settings = AwsSettings(REGION, service_security_group=SERVICE_GROUP)
        image = REPOSITORY + ':' + ATTEMPT
        host = f'on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'
        submitted = []
        def aws(args, **_kwargs):
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            if args[:2] == ['ecs', 'create-express-gateway-service']:
                spec = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                submitted.append(json.loads(spec.read_text()))
                return json.dumps({'service': {'serviceArn': ARN, 'currentDeployment': 'deployment-1'}})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'status': {'statusCode': 'ACTIVE'},
                    'activeConfigurations': [{'primaryContainer': {'image': image},
                        'networkConfiguration': {'securityGroups': [SERVICE_GROUP]},
                        'taskDefinitionArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1',
                        'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': host}]}]}})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                return json.dumps({'serviceDeployments': [{'status': 'SUCCESSFUL'}]})
            raise AssertionError(args)
        def command(args, **_kwargs):
            if args[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock'
            return ''
        with patch.object(self.adapter, 'validate_service_security_group'), \
                patch.object(self.adapter, 'prepare_infrastructure',
                             return_value=(ACCOUNT, REPOSITORY, 'execution', 'infra')), \
                patch('adapters.aws.ecs.ImageBuilder.build'), \
                patch.object(self.adapter, 'command', side_effect=command), \
                patch.object(self.adapter, 'aws', side_effect=aws), \
                patch.object(self.adapter, 'verify'):
            result = self.adapter.deploy(self.project, self.plan, ATTEMPT)
        self.assertEqual(submitted[0]['networkConfiguration'], {'securityGroups': [SERVICE_GROUP]})
        self.assertEqual(result['service_security_group'], SERVICE_GROUP)

    def test_postgres_binding_uses_verified_secret_and_role(self):
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
                                  ('subnet-12345678', 'subnet-87654321'), SERVICE_GROUP)
        database = {'stack_id': f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/sky-db-demo-app/id',
                    'database_arn': f'arn:aws:rds:{REGION}:{ACCOUNT}:db:sky-demo-app',
                    'database_id': 'sky-demo-app',
                    'endpoint': f'sky-demo-app.example.{REGION}.rds.amazonaws.com',
                    'port': 5432, 'secret_arn': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:managed-id',
                    'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/db-execution'}
        adapter = AwsExpressAdapter(lambda *_: None,
                                    AwsSettings(REGION, expected_account=ACCOUNT,
                                                service_security_group=SERVICE_GROUP))
        image = REPOSITORY + ':' + ATTEMPT
        host = f'on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'
        submitted = []
        def aws(args, **_kwargs):
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            if args[:2] == ['ecs', 'create-express-gateway-service']:
                spec = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                submitted.append(json.loads(spec.read_text()))
                return json.dumps({'service': {'serviceArn': ARN, 'currentDeployment': 'deployment-1'}})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'status': {'statusCode': 'ACTIVE'},
                    'activeConfigurations': [{'executionRoleArn': database['execution_role_arn'],
                        'primaryContainer': {'image': image,
                            'environment': submitted[0]['primaryContainer']['environment'],
                            'secrets': submitted[0]['primaryContainer']['secrets']},
                        'networkConfiguration': {'securityGroups': [SERVICE_GROUP]},
                        'taskDefinitionArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1',
                        'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': host}]}]}})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                return json.dumps({'serviceDeployments': [{'status': 'SUCCESSFUL'}]})
            raise AssertionError(args)
        def command(args, **_kwargs):
            if args[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock'
            return ''
        self.plan.required_env = ['PGHOST', 'PGPASSWORD']
        bundle = MigrationBundle(self.project / 'migrations', (SqlMigration('0001_init.sql', 'a' * 64),), 'b' * 64)
        with patch('adapters.aws.postgres.AwsPostgresProvisioner.inspect_current', return_value=database), \
                patch('adapters.aws.migrations.AwsMigrationRunner') as migrator, \
                patch.object(adapter, 'validate_service_security_group'), \
                patch.object(adapter, 'prepare_infrastructure',
                             return_value=(ACCOUNT, REPOSITORY, 'base-execution', 'infra')), \
                patch('adapters.aws.ecs.ImageBuilder.build'), \
                patch.object(adapter, 'command', side_effect=command), \
                patch.object(adapter, 'aws', side_effect=aws), \
                patch.object(adapter, 'verify'):
            migrator.return_value.repository = REPOSITORY
            migrator.return_value.build_and_run.return_value = {'bundle_digest': bundle.digest}
            result = adapter.deploy(self.project, self.plan, ATTEMPT, postgres=request, migrations=bundle)
            migrator.return_value.preflight.assert_called_once_with()
            migrator.return_value.build_and_run.assert_called_once_with()
            migrator.return_value.build_and_run.side_effect = AwsConfigurationError('마이그레이션 실패')
            with self.assertRaisesRegex(AwsConfigurationError, '마이그레이션 실패'):
                adapter.deploy(self.project, self.plan, ATTEMPT, postgres=request, migrations=bundle)
        self.assertEqual(result['database'], database)
        self.assertEqual(result['migration'], {'bundle_digest': bundle.digest})
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]['executionRoleArn'], database['execution_role_arn'])
        self.assertEqual(submitted[0]['primaryContainer']['secrets'], [
            {'name': 'PGUSER', 'valueFrom': database['secret_arn'] + ':username::'},
            {'name': 'PGPASSWORD', 'valueFrom': database['secret_arn'] + ':password::'}])
        self.assertNotIn('password', str(submitted[0]['primaryContainer']['environment']).lower())

    def test_postgres_binding_rejects_wrong_target_before_build(self):
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
                                  ('subnet-12345678', 'subnet-87654321'), SERVICE_GROUP)
        with patch('adapters.aws.ecs.ImageBuilder.build') as build:
            with self.assertRaisesRegex(AwsConfigurationError, '계정·리전'):
                self.adapter.deploy(self.project, self.plan, ATTEMPT, postgres=request)
            build.assert_not_called()

    def test_postgres_ca_path_cannot_be_overridden_by_application_environment(self):
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
                                  ('subnet-12345678', 'subnet-87654321'), SERVICE_GROUP)
        adapter = AwsExpressAdapter(lambda *_: None,
                                    AwsSettings(REGION, expected_account=ACCOUNT,
                                                service_security_group=SERVICE_GROUP))
        with patch('adapters.aws.postgres.AwsPostgresProvisioner.inspect_current') as inspect:
            with self.assertRaisesRegex(ValueError, '배포 시스템이 설정'):
                adapter.deploy(self.project, self.plan, ATTEMPT,
                               environment={'PGSSLROOTCERT': '/tmp/untrusted.pem'}, postgres=request)
            inspect.assert_not_called()

    def test_postgres_release_cannot_drop_existing_binding(self):
        adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(REGION),
                                    existing={'database': {'database_id': 'sky-demo-app'}})
        with patch('adapters.aws.ecs.ImageBuilder.build') as build:
            with self.assertRaisesRegex(AwsConfigurationError, 'PostgreSQL 연결 구성'):
                adapter.deploy(self.project, self.plan, ATTEMPT)
            build.assert_not_called()

    def test_postgres_configuration_check_rejects_duplicate_host(self):
        database = {'endpoint': 'db.example', 'port': 5432, 'secret_arn':
                    f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:managed-id',
                    'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/db-execution'}
        configuration = {'executionRoleArn': database['execution_role_arn'],
                         'primaryContainer': {'environment': [
                             {'name': 'PGHOST', 'value': database['endpoint']},
                             {'name': 'PGPORT', 'value': '5432'},
                             {'name': 'PGDATABASE', 'value': 'appdb'},
                             {'name': 'PGSSLMODE', 'value': 'require'}],
                             'secrets': [
                                 {'name': 'PGUSER', 'valueFrom': database['secret_arn'] + ':username::'},
                                 {'name': 'PGPASSWORD', 'valueFrom': database['secret_arn'] + ':password::'}]}}
        self.assertTrue(database_configuration_matches(configuration, database))
        configuration['primaryContainer']['environment'].append({'name': 'PGHOST', 'value': 'wrong'})
        self.assertFalse(database_configuration_matches(configuration, database))

    def test_python_postgres_configuration_requires_verified_ca_and_hostname(self):
        database = {'endpoint': 'db.example', 'port': 5432, 'secret_arn':
                    f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:managed-id',
                    'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/db-execution'}
        ssl = postgres_ssl_environment('python-asgi')
        self.assertEqual(ssl, [
            {'name': 'PGSSLMODE', 'value': 'verify-full'},
            {'name': 'PGSSLROOTCERT', 'value': '/app/.sky-rds-ca.pem'}])
        self.assertEqual(postgres_ssl_environment('nodejs'),
                         [{'name': 'PGSSLMODE', 'value': 'require'}])
        configuration = {'executionRoleArn': database['execution_role_arn'],
                         'primaryContainer': {'environment': [
                             {'name': 'PGHOST', 'value': database['endpoint']},
                             {'name': 'PGPORT', 'value': '5432'},
                             {'name': 'PGDATABASE', 'value': 'appdb'}, *ssl],
                             'secrets': [
                                 {'name': 'PGUSER', 'valueFrom': database['secret_arn'] + ':username::'},
                                 {'name': 'PGPASSWORD', 'valueFrom': database['secret_arn'] + ':password::'}]}}
        self.assertTrue(database_configuration_matches(configuration, database))
        self.assertFalse(database_configuration_matches(
            configuration, database, postgres_ssl_environment('nodejs')))
        self.assertTrue(database_configuration_matches(configuration, database, ssl))
        configuration['primaryContainer']['environment'][-1]['value'] = '/tmp/untrusted.pem'
        self.assertFalse(database_configuration_matches(configuration, database))

    def test_python_postgres_deployment_requires_exact_ecs_tls_profile(self):
        request = PostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
                                  ('subnet-12345678', 'subnet-87654321'), SERVICE_GROUP)
        database = {'endpoint': 'db.example', 'port': 5432, 'secret_arn':
                    f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:managed-id',
                    'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/db-execution'}
        adapter = AwsExpressAdapter(lambda *_: None,
                                    AwsSettings(REGION, expected_account=ACCOUNT,
                                                service_security_group=SERVICE_GROUP))
        submitted = []
        downgrade = [False]
        image = REPOSITORY + ':' + ATTEMPT
        host = f'on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'
        def aws(args, **_kwargs):
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            if args[:2] == ['ecs', 'create-express-gateway-service']:
                spec = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                submitted.append(json.loads(spec.read_text()))
                return json.dumps({'service': {'serviceArn': ARN,
                                                'currentDeployment': 'deployment-1'}})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                environment = list(submitted[-1]['primaryContainer']['environment'])
                if downgrade[0]:
                    environment = [item for item in environment
                                   if item['name'] not in {'PGSSLMODE', 'PGSSLROOTCERT'}]
                    environment.append({'name': 'PGSSLMODE', 'value': 'require'})
                return json.dumps({'service': {'status': {'statusCode': 'ACTIVE'},
                    'activeConfigurations': [{'executionRoleArn': database['execution_role_arn'],
                        'primaryContainer': {'image': image, 'environment': environment,
                            'secrets': submitted[-1]['primaryContainer']['secrets']},
                        'networkConfiguration': {'securityGroups': [SERVICE_GROUP]},
                        'taskDefinitionArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1',
                        'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': host}]}]}})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                return json.dumps({'serviceDeployments': [{'status': 'SUCCESSFUL'}]})
            raise AssertionError(args)

        with patch('adapters.aws.postgres.AwsPostgresProvisioner.inspect_current', return_value=database), \
                patch.object(adapter, 'validate_service_security_group'), \
                patch.object(adapter, 'prepare_infrastructure',
                             return_value=(ACCOUNT, REPOSITORY, 'base-execution', 'infra')), \
                patch('adapters.aws.ecs.ImageBuilder.build'), \
                patch.object(adapter, 'command', return_value='unix:///var/run/docker.sock'), \
                patch.object(adapter, 'aws', side_effect=aws), \
                patch.object(adapter, 'verify'):
            adapter.deploy(self.project, replace(self.plan, runtime='python-asgi'), ATTEMPT,
                           postgres=request)
            downgrade[0] = True
            with self.assertRaisesRegex(AwsConfigurationError, 'PostgreSQL 역할'):
                adapter.deploy(self.project, replace(self.plan, runtime='python-asgi'), ATTEMPT,
                               postgres=request)
        self.assertEqual(len(submitted), 2)
        self.assertIn({'name': 'PGSSLMODE', 'value': 'verify-full'},
                      submitted[0]['primaryContainer']['environment'])
        self.assertIn({'name': 'PGSSLROOTCERT', 'value': '/app/.sky-rds-ca.pem'},
                      submitted[0]['primaryContainer']['environment'])

    def test_release_update_rejects_changed_service_group_before_build(self):
        prior = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                 'service': SERVICE, 'service_arn': ARN, 'image': REPOSITORY + ':' + ATTEMPT,
                 'url': f'https://{SERVICE}.ecs.{REGION}.on.aws',
                 'service_security_group': SERVICE_GROUP}
        adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(REGION), existing=prior)
        with patch.object(adapter, 'prepare_infrastructure',
                          return_value=(ACCOUNT, REPOSITORY, 'execution', 'infra')), \
                patch('adapters.aws.ecs.ImageBuilder.build') as build:
            with self.assertRaisesRegex(AwsConfigurationError, '리소스 정보'):
                adapter.deploy(self.project, self.plan, 'b' * 16 + '-a1')
            build.assert_not_called()

    def test_cloudformation_prepares_repository_and_roles(self):
        outputs = [{'OutputKey': name, 'OutputValue': value} for name, value in {
            'RepositoryUri': REPOSITORY,
            'ExecutionRoleArn': f'arn:aws:iam::{ACCOUNT}:role/execution',
            'InfrastructureRoleArn': f'arn:aws:iam::{ACCOUNT}:role/infrastructure'}.items()]
        calls = []
        def aws(args, **kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['cloudformation', 'describe-stacks']:
                return json.dumps({'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': outputs}]})
            return ''
        with patch('adapters.aws.ecs.shutil.which', return_value='/usr/bin/tool'), patch.object(self.adapter, 'aws', side_effect=aws):
            result = self.adapter.prepare_infrastructure()
        self.assertEqual(result[1], REPOSITORY)
        deploy = next(args for args in calls if args[:2] == ['cloudformation', 'deploy'])
        self.assertIn('--capabilities', deploy)
        self.assertIn('CAPABILITY_IAM', deploy)
        self.assertTrue(Path(deploy[deploy.index('--template-file') + 1]).is_file())

    def test_deploy_creates_bounded_service_then_verifies_url(self):
        image = REPOSITORY + ':' + ATTEMPT
        host = f'on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'
        url = 'https://' + host
        commands = []
        push_attempts = 0
        deployment_checks = 0
        def command(args, **kwargs):
            nonlocal push_attempts
            commands.append(args)
            if args[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock'
            if 'push' in args:
                push_attempts += 1
                if push_attempts == 1:
                    raise AwsConfigurationError('ECR 이미지 업로드 연결이 시간 초과됐습니다.')
            return ''
        def aws(args, **kwargs):
            nonlocal deployment_checks
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            if args[:2] == ['ecs', 'create-express-gateway-service']:
                path = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                payload = json.loads(path.read_text())
                self.assertEqual(payload['serviceName'], SERVICE)
                self.assertEqual(payload['scalingTarget'], {'minTaskCount': 1, 'maxTaskCount': 1})
                self.assertEqual(payload['primaryContainer']['environment'],
                                 [{'name': 'PORT', 'value': '3000'},
                                  {'name': 'APP_SECRET', 'value': 'synthetic-value'}])
                return json.dumps({'service': {'serviceArn': ARN}})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'status': {'statusCode': 'ACTIVE'},
                                                'currentDeployment': 'initial-deployment' if deployment_checks == 0 else None,
                                                'activeConfigurations': [{'primaryContainer': {'image': image},
                                                    'taskDefinitionArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1',
                                                    'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': host}]}]}})
            if args[:2] == ['ecs', 'list-service-deployments']:
                return json.dumps({'serviceDeployments': [{'serviceDeploymentArn': 'initial-deployment',
                    'status': 'SUCCESSFUL', 'createdAt': '2026-09-29T00:00:00Z'}]})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                deployment_checks += 1
                return json.dumps({'serviceDeployments': [{'status': 'IN_PROGRESS' if deployment_checks == 1
                                                                else 'SUCCESSFUL'}]})
            raise AssertionError(args)
        self.plan.required_env = ['APP_SECRET']
        with patch.object(self.adapter, 'prepare_infrastructure', return_value=(ACCOUNT, REPOSITORY, 'execution', 'infra')), \
                patch('adapters.aws.ecs.ImageBuilder.build'), patch.object(self.adapter, 'command', side_effect=command), \
                patch.object(self.adapter, 'aws', side_effect=aws), patch.object(self.adapter, 'verify') as verify, \
                patch('adapters.aws.ecs.time.sleep'):
            result = self.adapter.deploy(self.project, self.plan, ATTEMPT, {'APP_SECRET': 'synthetic-value'})
        self.assertEqual(result['url'], url)
        self.assertEqual(result['service_arn'], ARN)
        self.assertIn('task-definition/', result['task_definition_arn'])
        verify.assert_called_once_with(url + '/')
        self.assertTrue(any(args[-2:] == ['push', image] for args in commands))
        self.assertEqual(push_attempts, 2)
        self.assertGreaterEqual(deployment_checks, 2)
        self.assertNotIn('synthetic-value', str(self.events))
        self.assertNotIn('synthetic-password', str(self.events))

    def test_cleanup_only_deletes_owned_service(self):
        self.adapter.service_created = True
        self.adapter.service_arn = ARN
        self.adapter.image_pushed = False
        self.adapter.image_built = False
        calls = []
        def aws(args, **kwargs):
            calls.append(args)
            return json.dumps({'service': {'tags': [{'key': 'sky-managed', 'value': 'false'},
                                                    {'key': 'sky-attempt', 'value': ATTEMPT}]}})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.adapter.cleanup_failure(ATTEMPT)
        self.assertEqual(len(calls), 1)

    def test_failed_create_waits_for_inactive_before_deleting_image(self):
        self.adapter.service_created = True
        self.adapter.service_arn = ARN
        self.adapter.image = REPOSITORY + ':' + ATTEMPT
        self.adapter.image_pushed = True
        states = iter(['ACTIVE', 'DRAINING', 'INACTIVE'])
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'serviceArn': ARN,
                    'status': {'statusCode': next(states)},
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}]}})
            return '{}'
        with patch.object(self.adapter, 'aws', side_effect=aws), patch('adapters.aws.ecs.time.sleep'):
            self.adapter.cleanup_failure(ATTEMPT)
        self.assertLess(next(i for i, call in enumerate(calls)
                             if call[:2] == ['ecs', 'delete-express-gateway-service']),
                        next(i for i, call in enumerate(calls) if call[:2] == ['ecr', 'batch-delete-image']))
        self.assertEqual(sum(call[:2] == ['ecs', 'describe-express-gateway-service'] for call in calls), 3)

    def test_failed_create_preserves_image_when_service_owner_is_uncertain(self):
        self.adapter.service_created = True
        self.adapter.service_arn = ARN
        self.adapter.image = REPOSITORY + ':' + ATTEMPT
        self.adapter.image_pushed = True
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'},
                'tags': [{'key': 'sky-managed', 'value': 'false'},
                         {'key': 'sky-attempt', 'value': ATTEMPT}]}})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.adapter.cleanup_failure(ATTEMPT)
        self.assertFalse(any(call[:2] == ['ecs', 'delete-express-gateway-service'] for call in calls))
        self.assertFalse(any(call[:2] == ['ecr', 'batch-delete-image'] for call in calls))

    def test_failed_create_preserves_image_while_service_drains(self):
        self.adapter.service_created = True
        self.adapter.service_arn = ARN
        self.adapter.image = REPOSITORY + ':' + ATTEMPT
        self.adapter.image_pushed = True
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'serviceArn': ARN,
                    'status': {'statusCode': 'DRAINING'},
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}]}})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws), patch('adapters.aws.ecs.time.sleep'):
            self.adapter.cleanup_failure(ATTEMPT)
        self.assertFalse(any(call[:2] == ['ecr', 'batch-delete-image'] for call in calls))
        self.assertTrue(any(stage == 'cleanup' and '보존' in message for stage, message in self.events))

    def test_failed_create_reports_ecr_delete_failure(self):
        self.adapter.image = REPOSITORY + ':' + ATTEMPT
        self.adapter.image_pushed = True
        with patch.object(self.adapter, 'aws', return_value=json.dumps({'failures': [
                {'failureCode': 'AccessDenied'}]})):
            self.adapter.cleanup_failure(ATTEMPT)
        self.assertTrue(any(stage == 'cleanup' and '정리 확인 필요' in message
                            for stage, message in self.events))

    def test_rejects_wrong_service_url(self):
        with self.assertRaises(AwsConfigurationError):
            self.adapter.validate_url('https://wrong.example.com', SERVICE, REGION)

    def test_retire_waits_for_inactive_before_deleting_image(self):
        image = REPOSITORY + ':' + ATTEMPT
        result = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                  'service': SERVICE, 'service_arn': ARN, 'image': image,
                  'url': f'https://on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'}
        calls = []
        states = iter(['ACTIVE', 'DRAINING', 'INACTIVE'])
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                state = next(states)
                return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': state},
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}],
                    'activeConfigurations': [{'primaryContainer': {'image': image}}]}})
            if args[:2] == ['ecr', 'batch-delete-image']:
                return json.dumps({'failures': []})
            return '{}'
        with patch.object(self.adapter, 'aws', side_effect=aws), patch('adapters.aws.ecs.time.sleep'):
            self.assertEqual(self.adapter.retire(result, ATTEMPT)['state'], 'deleted')
        self.assertLess(next(i for i, args in enumerate(calls) if args[:2] == ['ecs', 'delete-express-gateway-service']),
                        next(i for i, args in enumerate(calls) if args[:2] == ['ecr', 'batch-delete-image']))
        self.assertEqual(sum(args[:2] == ['ecs', 'describe-express-gateway-service'] for args in calls), 3)

    def test_retire_rejects_unowned_service(self):
        image = REPOSITORY + ':' + ATTEMPT
        result = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                  'service': SERVICE, 'service_arn': ARN, 'image': image,
                  'url': f'https://{SERVICE}.ecs.{REGION}.on.aws'}
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'},
                'tags': [{'key': 'sky-managed', 'value': 'false'},
                         {'key': 'sky-attempt', 'value': ATTEMPT}]}})
        with patch.object(self.adapter, 'aws', side_effect=aws):
            with self.assertRaises(AwsConfigurationError):
                self.adapter.retire(result, ATTEMPT)
        self.assertFalse(any(args[:2] in (['ecs', 'delete-express-gateway-service'],
                                          ['ecr', 'batch-delete-image']) for args in calls))

    def test_second_release_updates_owned_service_and_keeps_url(self):
        second_attempt = 'b' * 16 + '-a1'
        old_image = REPOSITORY + ':' + ATTEMPT
        new_image = REPOSITORY + ':' + second_attempt
        url = f'https://on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'
        prior = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                 'service': SERVICE, 'service_arn': ARN, 'image': old_image, 'url': url}
        checkpoints = []
        adapter = AwsExpressAdapter(lambda stage, message: self.events.append((stage, message)),
                                    AwsSettings(REGION), existing=prior,
                                    checkpoint=lambda **updates: checkpoints.append(updates))
        calls = []
        updated_describes = [0]
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                updated = any(call[:2] == ['ecs', 'update-express-gateway-service'] for call in calls)
                if updated:
                    updated_describes[0] += 1
                current_deployment = 'new-deployment' if updated and updated_describes[0] == 1 else None
                active = [{'primaryContainer': {'image': new_image if updated else old_image},
                    'taskDefinitionArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:{2 if updated else 1}',
                    'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]}]
                if current_deployment:
                    active.insert(0, {'primaryContainer': {'image': old_image},
                        'taskDefinitionArn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1'})
                return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'},
                    'currentDeployment': current_deployment,
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}],
                    'activeConfigurations': active}})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                return json.dumps({'serviceDeployments': [{'status': 'SUCCESSFUL'}]})
            if args[:2] == ['ecs', 'list-service-deployments']:
                return json.dumps({'serviceDeployments': [{'serviceDeploymentArn': 'old-deployment',
                    'createdAt': '2026-09-28T00:00:00+00:00', 'status': 'SUCCESSFUL'}]})
            if args[:2] == ['ecs', 'update-express-gateway-service']:
                self.assertEqual(checkpoints[0]['aws_previous_deployment_arn'], 'old-deployment')
                self.assertEqual(checkpoints[0]['aws_candidate_image'], new_image)
                self.assertTrue(checkpoints[0]['aws_update_submitted'])
                payload = json.loads(Path(args[args.index('--cli-input-json') + 1].removeprefix('file://')).read_text())
                self.assertEqual(payload['serviceArn'], ARN)
                self.assertEqual(payload['primaryContainer']['image'], new_image)
                self.assertNotIn('serviceName', payload)
                return json.dumps({'service': {'serviceArn': ARN}})
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            raise AssertionError(args)
        def command(args, **_kwargs):
            if args[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock'
            return ''
        with patch.object(adapter, 'prepare_infrastructure', return_value=(ACCOUNT, REPOSITORY, 'execution', 'infra')), \
                patch('adapters.aws.ecs.ImageBuilder.build'), patch.object(adapter, 'command', side_effect=command), \
                patch.object(adapter, 'aws', side_effect=aws), patch.object(adapter, 'verify') as verify, \
                patch('adapters.aws.ecs.time.sleep'):
            result = adapter.deploy(self.project, self.plan, second_attempt)
        self.assertEqual(result['url'], url)
        self.assertEqual(result['service'], SERVICE)
        self.assertEqual(result['owner_attempt'], ATTEMPT)
        self.assertEqual(result['images'], [old_image, new_image])
        self.assertTrue(result['previous_task_definition_arn'].endswith(':1'))
        self.assertGreaterEqual(updated_describes[0], 2)
        self.assertTrue(result['task_definition_arn'].endswith(':2'))
        verify.assert_called_once_with(url + '/')
        self.assertFalse(any(args[:2] == ['ecs', 'create-express-gateway-service'] for args in calls))

    def test_retire_updated_service_deletes_both_release_images(self):
        second_attempt = 'b' * 16 + '-a1'
        old_image = REPOSITORY + ':' + ATTEMPT
        new_image = REPOSITORY + ':' + second_attempt
        result = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                  'service': SERVICE, 'service_arn': ARN, 'owner_attempt': ATTEMPT,
                  'image': new_image, 'images': [old_image, new_image],
                  'url': f'https://{SERVICE}.ecs.{REGION}.on.aws'}
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                state = 'INACTIVE' if any(call[:2] == ['ecs', 'delete-express-gateway-service'] for call in calls) else 'ACTIVE'
                return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': state},
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}],
                    'activeConfigurations': [{'primaryContainer': {'image': new_image}}]}})
            if args[:2] == ['ecr', 'batch-delete-image']:
                return json.dumps({'failures': []})
            return '{}'
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.adapter.retire(result, ATTEMPT)
        deleted_tags = [args[-1] for args in calls if args[:2] == ['ecr', 'batch-delete-image']]
        self.assertEqual(deleted_tags, ['imageTag=' + ATTEMPT, 'imageTag=' + second_attempt])

    def test_abandoned_image_cleanup_checks_owned_service_and_active_image(self):
        second_attempt = 'b' * 16 + '-a1'
        old_image = REPOSITORY + ':' + ATTEMPT
        new_image = REPOSITORY + ':' + second_attempt
        result = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                  'service': SERVICE, 'service_arn': ARN, 'owner_attempt': ATTEMPT,
                  'image': old_image, 'url': f'https://{SERVICE}.ecs.{REGION}.on.aws'}
        calls = []
        active_image = old_image
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'},
                    'currentDeployment': None,
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}],
                    'activeConfigurations': [{'primaryContainer': {'image': active_image}}]}})
            if args[:2] == ['ecs', 'list-service-deployments']:
                return json.dumps({'serviceDeployments': [{'status': 'ROLLBACK_SUCCESSFUL',
                    'serviceDeploymentArn': 'new-deployment', 'createdAt': '2026-09-28T01:00:00Z'}]})
            if args[:2] == ['ecr', 'batch-delete-image']:
                return json.dumps({'failures': []})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws):
            self.assertEqual(self.adapter.cleanup_abandoned_image(result, new_image, second_attempt)['state'], 'deleted')
            self.assertEqual([call[-1] for call in calls if call[:2] == ['ecr', 'batch-delete-image']],
                             ['imageTag=' + second_attempt])
            calls.clear()
            active_image = new_image
            with self.assertRaises(AwsConfigurationError):
                self.adapter.cleanup_abandoned_image(result, new_image, second_attempt)
            self.assertFalse(any(call[:2] == ['ecr', 'batch-delete-image'] for call in calls))
        active_image = old_image
        def already_deleted(args, **kwargs):
            if args[:2] == ['ecr', 'batch-delete-image']:
                return json.dumps({'failures': [{'failureCode': 'ImageNotFound'}]})
            return aws(args, **kwargs)
        with patch.object(self.adapter, 'aws', side_effect=already_deleted):
            self.assertEqual(self.adapter.cleanup_abandoned_image(result, new_image, second_attempt)['state'], 'deleted')

    def test_ongoing_update_rollback_verifies_both_revision_images(self):
        second_attempt = 'b' * 16 + '-a1'
        old_image = REPOSITORY + ':' + ATTEMPT
        new_image = REPOSITORY + ':' + second_attempt
        result = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                  'service': SERVICE, 'service_arn': ARN, 'owner_attempt': ATTEMPT,
                  'image': old_image, 'url': f'https://{SERVICE}.ecs.{REGION}.on.aws'}
        prefix = f'arn:aws:ecs:{REGION}:{ACCOUNT}'
        old_deployment = prefix + f':service-deployment/default/{SERVICE}/old'
        new_deployment = prefix + f':service-deployment/default/{SERVICE}/new'
        old_revision = prefix + f':service-revision/default/{SERVICE}/1'
        new_revision = prefix + f':service-revision/default/{SERVICE}/2'
        old_task = prefix + ':task-definition/old-task:1'
        new_task = prefix + ':task-definition/new-task:2'
        source_image = old_image
        target_image = new_image
        deployment_status = 'IN_PROGRESS'
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'},
                    'currentDeployment': new_deployment,
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}]}})
            if args[:2] == ['ecs', 'list-service-deployments']:
                return json.dumps({'serviceDeployments': [{'serviceDeploymentArn': new_deployment,
                    'status': deployment_status, 'createdAt': '2026-09-28T01:00:00Z'}]})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                return json.dumps({'serviceDeployments': [{'serviceArn': ARN,
                    'serviceDeploymentArn': new_deployment, 'status': deployment_status,
                    'sourceServiceRevisions': [{'arn': old_revision}],
                    'targetServiceRevision': {'arn': new_revision}}]})
            if args[:2] == ['ecs', 'describe-service-revisions']:
                return json.dumps({'serviceRevisions': [
                    {'serviceRevisionArn': old_revision, 'serviceArn': ARN, 'taskDefinition': old_task},
                    {'serviceRevisionArn': new_revision, 'serviceArn': ARN, 'taskDefinition': new_task}]})
            if args[:2] == ['ecs', 'describe-task-definition']:
                task = args[args.index('--task-definition') + 1]
                return json.dumps({'taskDefinition': {'taskDefinitionArn': task,
                    'containerDefinitions': [{'name': 'Main', 'image': source_image if task == old_task else target_image}]}})
            if args[:2] == ['ecs', 'stop-service-deployment']:
                return json.dumps({'serviceDeploymentArn': new_deployment})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws):
            outcome = self.adapter.request_update_rollback(result, new_image, old_deployment, second_attempt)
            self.assertEqual(outcome['state'], 'requested')
            self.assertEqual(calls[-1][-2:], ['--stop-type', 'ROLLBACK'])
            calls.clear()
            deployment_status = 'ROLLBACK_REQUESTED'
            self.assertEqual(self.adapter.request_update_rollback(
                result, new_image, old_deployment, second_attempt)['state'], 'already_requested')
            self.assertFalse(any(call[:2] == ['ecs', 'stop-service-deployment'] for call in calls))
            calls.clear()
            deployment_status = 'IN_PROGRESS'
            target_image = REPOSITORY + ':unexpected-a1'
            with self.assertRaises(AwsConfigurationError):
                self.adapter.request_update_rollback(result, new_image, old_deployment, second_attempt)
            self.assertFalse(any(call[:2] == ['ecs', 'stop-service-deployment'] for call in calls))
            calls.clear()
            target_image = new_image
            source_image = REPOSITORY + ':unexpected-a1'
            with self.assertRaises(AwsConfigurationError):
                self.adapter.request_update_rollback(result, new_image, old_deployment, second_attempt)
            self.assertFalse(any(call[:2] == ['ecs', 'stop-service-deployment'] for call in calls))

    def test_completed_release_rollback_reuses_previous_task_definition(self):
        second_attempt = 'b' * 16 + '-a1'
        old_image = REPOSITORY + ':' + ATTEMPT
        new_image = REPOSITORY + ':' + second_attempt
        url = f'https://on-853b928a17804449916e1acb4141349b.ecs.{REGION}.on.aws'
        old_task = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:1'
        old_deployment = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service-deployment/default/{SERVICE}/old'
        new_deployment = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service-deployment/default/{SERVICE}/rollback'
        previous = {'account': ACCOUNT, 'region': REGION, 'target': 'aws-ecs-express',
                    'service': SERVICE, 'service_arn': ARN, 'owner_attempt': ATTEMPT,
                    'image': old_image, 'task_definition_arn': old_task, 'url': url}
        current = {**previous, 'image': new_image, 'images': [old_image, new_image],
                   'previous_task_definition_arn': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:2'}
        calls, checkpoints = [], []
        task_image = old_image
        def aws(args, **_kwargs):
            calls.append(args)
            updated = any(call[:2] == ['ecs', 'update-express-gateway-service'] for call in calls)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                image = old_image if updated else new_image
                return json.dumps({'service': {'serviceArn': ARN, 'status': {'statusCode': 'ACTIVE'},
                    'currentDeployment': None,
                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                             {'key': 'sky-attempt', 'value': ATTEMPT}],
                    'activeConfigurations': [{'primaryContainer': {'image': image},
                        'taskDefinitionArn': old_task,
                        'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]}]}})
            if args[:2] == ['ecs', 'describe-task-definition']:
                return json.dumps({'taskDefinition': {'taskDefinitionArn': old_task,
                    'containerDefinitions': [{'name': 'Main', 'image': task_image}]}})
            if args[:2] == ['ecs', 'list-service-deployments']:
                return json.dumps({'serviceDeployments': [{'serviceDeploymentArn': new_deployment if updated else old_deployment,
                    'status': 'SUCCESSFUL', 'createdAt': '2026-09-28T01:00:00Z'}]})
            if args[:2] == ['ecs', 'update-express-gateway-service']:
                self.assertTrue(checkpoints[0]['release_rollback_submitted'])
                payload = json.loads(Path(args[args.index('--cli-input-json') + 1].removeprefix('file://')).read_text())
                self.assertEqual(payload, {'serviceArn': ARN, 'taskDefinitionArn': old_task,
                                           'healthCheckPath': '/health'})
                return json.dumps({'service': {'serviceArn': ARN, 'currentDeployment': new_deployment}})
            raise AssertionError(args)
        with patch.object(self.adapter, 'aws', side_effect=aws), patch.object(self.adapter, 'verify') as verify:
            outcome = self.adapter.rollback_release(current, previous, '/health',
                                                    lambda **updates: checkpoints.append(updates))
            self.assertEqual(outcome['image'], old_image)
            verify.assert_called_once_with(url + '/health')
            calls.clear()
            task_image = new_image
            with self.assertRaises(AwsConfigurationError):
                self.adapter.rollback_release(current, previous, '/health')
            self.assertFalse(any(call[:2] == ['ecs', 'update-express-gateway-service'] for call in calls))


if __name__ == '__main__':
    unittest.main()
