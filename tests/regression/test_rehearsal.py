import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from adapters.aws.ecs import AwsExpressAdapter, AwsSettings
from application.deployment_core import analyze
from adapters.local.rehearsal import rehearse_image


ATTEMPT = 'a' * 16 + '-a1'
IMAGE = 'example.invalid/sky-managed:' + ATTEMPT
IMAGE_ID = 'sha256:' + 'b' * 64


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.project = Path('tests/fixtures/apps/hello-node')
        self.plan = analyze(self.project)
        self.plan.target = 'aws-ecs-express'

    def test_local_rehearsal_uses_built_image_and_removes_container(self):
        commands = []
        def command(args, **kwargs):
            commands.append((args, kwargs))
            if args[1:3] == ['image', 'inspect']:
                return IMAGE_ID
            if args[1] == 'port':
                return '127.0.0.1:49152'
            return ''
        response = MagicMock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch('adapters.local.rehearsal.urllib.request.build_opener', return_value=opener):
            result = rehearse_image(command, lambda *_: None, IMAGE, self.plan, ATTEMPT,
                                    {'APP_SECRET': 'synthetic-private-value'})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['image_id'], IMAGE_ID)
        run = next(args for args, _ in commands if args[1] == 'run')
        self.assertIn('--env-file', run)
        self.assertNotIn('synthetic-private-value', str(commands))
        self.assertEqual(commands[-1][0][:4], ['docker', 'rm', '-f', 'sky-rehearsal-' + ATTEMPT])
        self.assertEqual(opener.open.call_args.args[0], 'http://127.0.0.1:49152' + self.plan.health_path)

    def test_failed_rehearsal_blocks_ecr_and_ecs(self):
        adapter = AwsExpressAdapter(lambda *_: None,
                                    AwsSettings('ap-northeast-2', expected_account='123456789012'),
                                    rehearsal=True)
        with patch.object(adapter, 'prepare_infrastructure', return_value=(
                '123456789012', '123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/sky-managed',
                'execution', 'infrastructure')) as prepare, \
                patch('adapters.aws.ecs.ImageBuilder.build'), \
                patch('adapters.aws.ecs.rehearse_image', side_effect=RuntimeError('HTTP failed')), \
                patch.object(adapter, 'aws') as aws:
            with self.assertRaisesRegex(RuntimeError, 'HTTP failed'):
                adapter.deploy(self.project, self.plan, ATTEMPT)
        prepare.assert_not_called()
        aws.assert_not_called()
        self.assertTrue(adapter.rehearsal_image_built)
        self.assertFalse(adapter.image_built)
        self.assertFalse(adapter.image_pushed)
        self.assertFalse(adapter.service_created)

    def test_failed_docker_run_only_cleans_matching_rehearsal_container(self):
        removed = []
        def command(args, **_kwargs):
            if args[1:3] == ['image', 'inspect']:
                return IMAGE_ID
            if args[1] == 'run':
                raise RuntimeError('run failed')
            if args[1] == 'inspect':
                return json.dumps({'app': 'sky', 'sky-attempt': ATTEMPT})
            if args[1:3] == ['rm', '-f']:
                removed.append(args[-1])
            return ''
        with self.assertRaisesRegex(RuntimeError, 'run failed'):
            rehearse_image(command, lambda *_: None, IMAGE, self.plan, ATTEMPT, {})
        self.assertEqual(removed, ['sky-rehearsal-' + ATTEMPT])

    def test_aws_promotes_the_rehearsed_build_without_rebuilding(self):
        account = '123456789012'
        region = 'ap-northeast-2'
        repository = f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed'
        image = repository + ':' + ATTEMPT
        service = 'sky-' + ATTEMPT
        arn = f'arn:aws:ecs:{region}:{account}:service/default/{service}'
        digest = 'sha256:' + 'c' * 64
        requests = []
        def aws(args, **_kwargs):
            requests.append(args)
            if args[:2] == ['ecr', 'get-login-password']:
                return 'synthetic-password'
            if args[:2] == ['ecr', 'describe-images']:
                return json.dumps({'imageDetails': [{'imageDigest': digest}]})
            if args[:2] == ['ecs', 'create-express-gateway-service']:
                return json.dumps({'service': {'serviceArn': arn,
                                               'currentDeployment': 'deployment-1'}})
            if args[:2] == ['ecs', 'describe-express-gateway-service']:
                return json.dumps({'service': {'status': {'statusCode': 'ACTIVE'},
                    'activeConfigurations': [{'primaryContainer': {'image': image},
                        'taskDefinitionArn': f'arn:aws:ecs:{region}:{account}:task-definition/{service}:1',
                        'ingressPaths': [{'accessType': 'PUBLIC',
                                          'endpoint': f'on-demo.ecs.{region}.on.aws'}]}]}})
            if args[:2] == ['ecs', 'describe-service-deployments']:
                return json.dumps({'serviceDeployments': [{'status': 'SUCCESSFUL'}]})
            raise AssertionError(args)
        def command(args, **_kwargs):
            if args[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock'
            if args[1:3] == ['image', 'inspect']:
                return IMAGE_ID
            return ''
        adapter = AwsExpressAdapter(lambda *_: None,
                                    AwsSettings(region, expected_account=account), rehearsal=True)
        with patch.object(adapter, 'prepare_infrastructure', return_value=(
                account, repository, 'execution', 'infrastructure')), \
                patch('adapters.aws.ecs.ImageBuilder.build') as build, \
                patch('adapters.aws.ecs.rehearse_image', return_value={
                    'status': 'passed', 'image_id': IMAGE_ID,
                    'platform': 'linux/amd64', 'health_path': self.plan.health_path}) as rehearse, \
                patch.object(adapter, 'command', side_effect=command), \
                patch.object(adapter, 'aws', side_effect=aws), \
                patch.object(adapter, 'verify'):
            result = adapter.deploy(self.project, self.plan, ATTEMPT)
        build.assert_called_once()
        rehearse.assert_called_once()
        local_image = f'sky/rehearsal-{ATTEMPT}:latest'
        self.assertEqual(build.call_args.args[2], local_image)
        self.assertEqual(rehearse.call_args.args[2], local_image)
        self.assertEqual(result['rehearsal']['image_id'], IMAGE_ID)
        self.assertEqual(result['image_digest'], digest)
        self.assertEqual(sum(args[:2] == ['ecr', 'describe-images'] for args in requests), 2)


if __name__ == '__main__':
    unittest.main()
