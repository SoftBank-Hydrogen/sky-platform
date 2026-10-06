import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from planning.analysis import AISettings
from api.certificate import deployment_certificate
from api.server import App, handler_for


class CertificateTests(unittest.TestCase):
    def test_success_records_http_without_inventing_missing_evidence(self):
        job = {'id': 'a' * 16, 'application_id': 'sample-app', 'status': 'succeeded',
               'target': 'aws-ecs-express', 'created_at': '2026-10-06T00:00:00+00:00',
               'source_digest': 'a' * 64, 'plan': {'source_digest': 'b' * 64},
               'changes': [{'path': 'server.js', 'diff': '+SECRET=synthetic-private-value'}],
               'infrastructure_plan': {'resources': ['ECS', 'RDS'], 'database': {'binding': 'existing'},
                                       'compatibility': {'access_mode': 'public'}},
               'result': {'url': 'https://example.com', 'image': 'example:v1',
                          'region': 'ap-northeast-2', 'service': 'example',
                          'migration': {'applied': 1}, 'secret': 'synthetic-private-value'}}
        certificate = deployment_certificate(job)
        self.assertEqual(certificate['schema_version'], 1)
        self.assertEqual(certificate['source']['uploaded_sha256'], 'a' * 64)
        self.assertEqual(certificate['source']['prepared_sha256'], 'b' * 64)
        self.assertEqual(certificate['source']['changed_paths'], ['server.js'])
        self.assertEqual(certificate['destination']['access_mode'], 'public')
        self.assertEqual(certificate['artifact']['image_reference'], 'example:v1')
        self.assertIsNone(certificate['artifact']['registry_manifest_digest'])
        self.assertIsNone(certificate['artifact']['local_image_id'])
        statuses = {item['name']: item['status'] for item in certificate['verification']}
        self.assertEqual(statuses['deployment_http'], 'passed')
        self.assertEqual(statuses['schema_migration'], 'passed')
        self.assertEqual(statuses['image_identity'], 'unverified')
        self.assertEqual(statuses['ai_model_execution'], 'unverified')
        self.assertEqual(statuses['cross_environment_data_migration'], 'unverified')
        self.assertNotIn('synthetic-private-value', str(certificate))
        self.assertNotIn('result', certificate)

    def test_failed_job_and_old_health_do_not_become_current_success(self):
        job = {'id': 'a' * 16, 'status': 'failed', 'target': 'local-docker',
               'result': {'url': 'http://127.0.0.1:1234'}}
        history = [{'healthy': True, 'checked_at': '2026-10-05T00:00:00+00:00'}]
        certificate = deployment_certificate(job, history)
        checks = {item['name']: item for item in certificate['verification']}
        self.assertEqual(checks['deployment_http']['status'], 'unverified')
        self.assertEqual(checks['latest_health']['status'], 'passed')
        self.assertIsNone(certificate['destination']['url'])
        self.assertIn('deployment_http', certificate['unverified'])
        self.assertEqual(job['status'], 'failed')

    def test_rehearsal_and_registry_evidence_do_not_claim_running_task_digest(self):
        job = {'id': 'a' * 16, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'result': {'url': 'https://example.com', 'image': 'example:v1',
                          'image_digest': 'sha256:' + 'c' * 64,
                          'rehearsal': {'status': 'passed', 'image_id': 'sha256:' + 'b' * 64}}}
        certificate = deployment_certificate(job)
        checks = {item['name']: item['status'] for item in certificate['verification']}
        self.assertEqual(checks['local_rehearsal'], 'passed')
        self.assertEqual(checks['registry_manifest'], 'passed')
        self.assertEqual(checks['image_identity'], 'unverified')
        self.assertEqual(certificate['artifact']['local_image_id'], 'sha256:' + 'b' * 64)
        self.assertEqual(certificate['artifact']['registry_manifest_digest'], 'sha256:' + 'c' * 64)

    def test_certificate_api_requires_session_and_existing_job(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings(), monitor_interval=0)
            job_id = 'a' * 16
            app.jobs[job_id] = {'id': job_id, 'status': 'succeeded',
                                'result': {'url': 'http://127.0.0.1:1234'}, 'events': []}
            handler_type = handler_for(app)
            handler = handler_type.__new__(handler_type)
            handler.path = f'/api/jobs/{job_id}/certificate'
            handler.json_response = Mock()
            handler.headers = {'X-Sky-Token': 'wrong'}
            handler.do_GET()
            self.assertEqual(handler.json_response.call_args.args[0], 403)
            handler.headers = {'X-Sky-Token': app.token}
            handler.do_GET()
            status, payload = handler.json_response.call_args.args
            self.assertEqual(status, 200)
            self.assertEqual(payload['job']['id'], job_id)
            handler.path = '/api/jobs/' + 'b' * 16 + '/certificate'
            handler.do_GET()
            self.assertEqual(handler.json_response.call_args.args[0], 404)


if __name__ == '__main__':
    unittest.main()
