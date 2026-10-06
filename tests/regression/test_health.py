import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from application.health import check_deployment
from interfaces.http.server import App, handler_for


JOB_ID = 'a' * 16


class HealthTests(unittest.TestCase):
    def local_job(self):
        return {'id': JOB_ID, 'status': 'succeeded', 'target': 'local-docker',
                'plan': {'health_path': '/health'},
                'result': {'container': f'sky-{JOB_ID}-a1',
                           'image': f'sky/{JOB_ID}-a1:latest',
                           'url': 'http://127.0.0.1:49152'}}

    def test_local_checks_running_owned_container_and_http(self):
        job = self.local_job()
        inspect = [{'State': {'Running': True}, 'Config': {'Labels': {'app': 'sky'},
                                                          'Image': job['result']['image']}}]
        with patch('application.health.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(inspect), '')) as command, \
                patch('application.health.probe', return_value=True) as probe:
            self.assertTrue(check_deployment(job)['healthy'])
        command.assert_called_once_with(['docker', 'inspect', job['result']['container']],
                                        capture_output=True, text=True, timeout=10)
        probe.assert_called_once_with('http://127.0.0.1:49152/health')

    def test_local_rejects_changed_container_or_non_loopback_url(self):
        job = self.local_job()
        inspect = [{'State': {'Running': True}, 'Config': {'Labels': {'app': 'other'},
                                                          'Image': job['result']['image']}}]
        with patch('application.health.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(inspect), '')), \
                patch('application.health.probe') as probe:
            self.assertFalse(check_deployment(job)['healthy'])
            probe.assert_not_called()
        job['result']['url'] = 'http://example.com:49152'
        inspect[0]['Config']['Labels']['app'] = 'sky'
        with patch('application.health.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(inspect), '')), \
                patch('application.health.probe') as probe:
            self.assertFalse(check_deployment(job)['healthy'])
            probe.assert_not_called()

    def test_cloud_checks_managed_service_before_request(self):
        service = f'sky-{JOB_ID}-a1'
        url = 'https://sky-example.a.run.app'
        job = {'id': JOB_ID, 'status': 'succeeded', 'target': 'cloud-run',
               'plan': {'health_path': '/health'},
               'cloud': {'project': 'example-project', 'region': 'asia-northeast3',
                         'repository': 'sky', 'service_account': ''},
               'result': {'service': service, 'url': url, 'project': 'example-project',
                          'region': 'asia-northeast3', 'public': False}}
        service_data = {'metadata': {'labels': {'sky-managed': 'true',
                                                'sky-attempt': f'{JOB_ID}-a1'}},
                        'status': {'url': url}}
        def gcloud(_, args, **kwargs):
            return json.dumps(service_data) if args[1] == 'services' else 'identity-token'
        with patch('application.health.CloudRunAdapter.gcloud', autospec=True, side_effect=gcloud), \
                patch('application.health.probe', return_value=True) as probe:
            self.assertTrue(check_deployment(job)['healthy'])
            probe.assert_called_once_with(url + '/health', 'identity-token')
            service_data['metadata']['labels']['sky-managed'] = 'false'
            self.assertFalse(check_deployment(job)['healthy'])
            self.assertEqual(probe.call_count, 1)

    def test_health_api_checks_only_completed_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            job = self.local_job()
            app.jobs[JOB_ID] = job
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = f'/api/jobs/{JOB_ID}/health'
            handler.headers = {'X-Sky-Token': app.token}
            handler.json_response = Mock()
            with patch('interfaces.http.server.check_deployment', return_value={'healthy': True}) as check:
                handler.do_GET()
                handler.json_response.assert_called_once_with(200, {'healthy': True})
                check.assert_called_once()
            job['status'] = 'failed'
            handler.json_response.reset_mock()
            handler.do_GET()
            handler.json_response.assert_called_once_with(409, {'error': 'Only completed deployments can be checked'})

    def test_aws_checks_owned_active_service_and_image(self):
        service = f'sky-{JOB_ID}-a1'
        arn = f'arn:aws:ecs:ap-northeast-2:123456789012:service/default/{service}'
        image = f'123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/sky-managed:{JOB_ID}-a1'
        url = f'https://{service}.ecs.ap-northeast-2.on.aws'
        job = {'id': JOB_ID, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'plan': {'health_path': '/health'}, 'aws': {'region': 'ap-northeast-2'},
               'result': {'service': service, 'service_arn': arn, 'account': '123456789012',
                          'region': 'ap-northeast-2', 'image': image, 'url': url}}
        service_data = {'service': {'serviceArn': arn, 'status': {'statusCode': 'ACTIVE'},
                                    'tags': [{'key': 'sky-managed', 'value': 'true'},
                                             {'key': 'sky-attempt', 'value': JOB_ID + '-a1'}],
                                    'activeConfigurations': [{'primaryContainer': {'image': image},
                                        'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]}]}}
        with patch('application.health.AwsExpressAdapter.aws', side_effect=lambda *_, **__: json.dumps(service_data)), \
                patch('application.health.probe', return_value=True) as probe:
            self.assertTrue(check_deployment(job)['healthy'])
            probe.assert_called_once_with(url + '/health')
            service_data['service']['tags'][0]['value'] = 'false'
            self.assertFalse(check_deployment(job)['healthy'])
            self.assertEqual(probe.call_count, 1)

    def test_aws_health_detects_removed_service_security_group(self):
        service = f'sky-{JOB_ID}-a1'
        group = 'sg-12345678'
        arn = f'arn:aws:ecs:ap-northeast-2:123456789012:service/default/{service}'
        image = f'123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/sky-managed:{JOB_ID}-a1'
        url = f'https://{service}.ecs.ap-northeast-2.on.aws'
        job = {'id': JOB_ID, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'plan': {'health_path': '/'},
               'aws': {'region': 'ap-northeast-2', 'service_security_group': group},
               'result': {'service': service, 'service_arn': arn, 'account': '123456789012',
                          'region': 'ap-northeast-2', 'image': image, 'url': url,
                          'service_security_group': group}}
        active = {'primaryContainer': {'image': image},
                  'networkConfiguration': {'securityGroups': [group]},
                  'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]}
        service_data = {'service': {'serviceArn': arn, 'status': {'statusCode': 'ACTIVE'},
            'tags': [{'key': 'sky-managed', 'value': 'true'},
                     {'key': 'sky-attempt', 'value': JOB_ID + '-a1'}],
            'activeConfigurations': [active]}}
        with patch('application.health.AwsExpressAdapter.aws',
                   side_effect=lambda *_, **__: json.dumps(service_data)), \
                patch('application.health.probe', return_value=True):
            self.assertTrue(check_deployment(job)['healthy'])
            active['networkConfiguration']['securityGroups'] = []
            self.assertFalse(check_deployment(job)['healthy'])

    def test_updated_aws_release_uses_original_service_owner_and_new_image(self):
        newer_job_id = 'b' * 16
        owner_attempt = JOB_ID + '-a1'
        service = 'sky-' + owner_attempt
        image = f'123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/sky-managed:{newer_job_id}-a1'
        url = 'https://on-853b928a17804449916e1acb4141349b.ecs.ap-northeast-2.on.aws'
        job = {'id': newer_job_id, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'aws': {'region': 'ap-northeast-2'}, 'plan': {'health_path': '/'},
               'result': {'service': service, 'owner_attempt': owner_attempt, 'image': image,
                          'service_arn': f'arn:aws:ecs:ap-northeast-2:123456789012:service/default/{service}',
                          'account': '123456789012', 'region': 'ap-northeast-2', 'url': url}}
        service_data = {'service': {'serviceArn': job['result']['service_arn'],
            'status': {'statusCode': 'ACTIVE'},
            'tags': [{'key': 'sky-managed', 'value': 'true'},
                     {'key': 'sky-attempt', 'value': owner_attempt}],
            'activeConfigurations': [{'primaryContainer': {'image': image},
                'ingressPaths': [{'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]}]}}
        with patch('application.health.AwsExpressAdapter.aws', return_value=json.dumps(service_data)), \
                patch('application.health.probe', return_value=True):
            self.assertTrue(check_deployment(job)['healthy'])
        job['deployment_state'] = 'superseded'
        self.assertFalse(check_deployment(job)['healthy'])

    def test_aws_health_requires_one_completed_matching_task_definition(self):
        service = f'sky-{JOB_ID}-a1'
        image = f'123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/sky-managed:{JOB_ID}-a1'
        task_arn = f'arn:aws:ecs:ap-northeast-2:123456789012:task-definition/{service}:1'
        job = {'id': JOB_ID, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'aws': {'region': 'ap-northeast-2'}, 'plan': {'health_path': '/'},
               'result': {'service': service, 'service_arn':
                          f'arn:aws:ecs:ap-northeast-2:123456789012:service/default/{service}',
                          'account': '123456789012', 'region': 'ap-northeast-2',
                          'image': image, 'task_definition_arn': task_arn,
                          'url': f'https://{service}.ecs.ap-northeast-2.on.aws'}}
        active = {'primaryContainer': {'image': image}, 'taskDefinitionArn': task_arn,
                  'ingressPaths': [{'accessType': 'PUBLIC',
                                    'endpoint': job['result']['url'].removeprefix('https://')}]}
        service_data = {'service': {'serviceArn': job['result']['service_arn'],
            'status': {'statusCode': 'ACTIVE'}, 'currentDeployment': None,
            'tags': [{'key': 'sky-managed', 'value': 'true'},
                     {'key': 'sky-attempt', 'value': JOB_ID + '-a1'}],
            'activeConfigurations': [active]}}
        with patch('application.health.AwsExpressAdapter.aws',
                   side_effect=lambda *_, **__: json.dumps(service_data)), \
                patch('application.health.probe', return_value=True) as probe:
            self.assertTrue(check_deployment(job)['healthy'])
            service_data['service']['currentDeployment'] = 'in-progress'
            self.assertFalse(check_deployment(job)['healthy'])
            service_data['service']['currentDeployment'] = None
            active['taskDefinitionArn'] = task_arn.removesuffix(':1') + ':99'
            self.assertFalse(check_deployment(job)['healthy'])
            active['taskDefinitionArn'] = task_arn
            service_data['service']['activeConfigurations'].append(dict(active))
            self.assertFalse(check_deployment(job)['healthy'])
            self.assertEqual(probe.call_count, 1)

    def test_aws_health_rejects_url_not_owned_by_service(self):
        service = f'sky-{JOB_ID}-a1'
        image = f'123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/sky-managed:{JOB_ID}-a1'
        arn = f'arn:aws:ecs:ap-northeast-2:123456789012:service/default/{service}'
        url = f'https://{service}.ecs.ap-northeast-2.on.aws'
        job = {'id': JOB_ID, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'aws': {'region': 'ap-northeast-2'}, 'plan': {'health_path': '/'},
               'result': {'service': service, 'service_arn': arn, 'account': '123456789012',
                          'region': 'ap-northeast-2', 'image': image, 'url': url}}
        service_data = {'service': {'serviceArn': arn, 'status': {'statusCode': 'ACTIVE'},
            'tags': [{'key': 'sky-managed', 'value': 'true'},
                     {'key': 'sky-attempt', 'value': JOB_ID + '-a1'}],
            'activeConfigurations': [{'primaryContainer': {'image': image},
                'ingressPaths': [{'accessType': 'PUBLIC',
                                  'endpoint': f'other.ecs.ap-northeast-2.on.aws'}]}]}}
        with patch('application.health.AwsExpressAdapter.aws',
                   side_effect=lambda *_, **__: json.dumps(service_data)), \
                patch('application.health.probe', return_value=True) as probe:
            self.assertFalse(check_deployment(job)['healthy'])
            probe.assert_not_called()
            service_data['service']['activeConfigurations'][0]['ingressPaths'] = [
                {'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')},
                {'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]
            self.assertFalse(check_deployment(job)['healthy'])
            probe.assert_not_called()
            service_data['service']['activeConfigurations'][0]['ingressPaths'] = [
                {'accessType': 'PUBLIC', 'endpoint': url.removeprefix('https://')}]
            self.assertTrue(check_deployment(job)['healthy'])


if __name__ == '__main__':
    unittest.main()
