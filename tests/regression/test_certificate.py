import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from application.analysis import AISettings
from application.certificate import deployment_certificate
from engine.application_ir import application_ir
from engine.compatibility import InfrastructureProfile, infrastructure_compatibility
from interfaces.http.server import App, handler_for


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
        self.assertEqual(statuses['schema_migration'], 'unverified')
        self.assertEqual(statuses['image_identity'], 'unverified')
        self.assertEqual(statuses['ai_model_execution'], 'unverified')
        self.assertEqual(statuses['cross_environment_data_migration'], 'unverified')
        self.assertEqual(certificate['decision_trace']['status'], 'incomplete')
        self.assertNotIn('synthetic-private-value', str(certificate))
        self.assertNotIn('result', certificate)

    def test_schema_migration_requires_owned_successful_task_and_matching_journal(self):
        job_id = 'a' * 16
        attempt = job_id + '-a1'
        account, region = '123456789012', 'ap-northeast-2'
        migration = {
            'task_arn': f'arn:aws:ecs:{region}:{account}:task/default/' + 'b' * 32,
            'task_definition_arn': f'arn:aws:ecs:{region}:{account}:task-definition/sky-migrate-{attempt}:1',
            'image': f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed:{attempt}-db',
            'image_digest': 'sha256:' + 'c' * 64,
            'bundle_digest': 'd' * 64,
            'cleanup_complete': True,
        }
        job = {'id': job_id, 'status': 'succeeded', 'target': 'aws-ecs-express', 'attempts': 1,
               'aws': {'region': region, 'expected_account': account},
               'aws_migration_status': 'succeeded',
               'aws_migration_bundle_digest': migration['bundle_digest'],
               'aws_migration_result': dict(migration),
               'postgres': {'application_id': 'sample-app'},
               'result': {'url': 'https://example.com', 'region': region, 'account': account,
                          'migration': dict(migration)}}

        def status():
            checks = deployment_certificate(job)['verification']
            return {item['name']: item['status'] for item in checks}

        self.assertEqual(status()['schema_migration'], 'passed')
        self.assertEqual(status()['cross_environment_data_migration'], 'unverified')
        job['result']['migration']['bundle_digest'] = 'e' * 64
        self.assertEqual(status()['schema_migration'], 'unverified')
        job['result']['migration']['bundle_digest'] = migration['bundle_digest']
        job['result']['migration']['task_definition_arn'] = (
            f'arn:aws:ecs:{region}:{account}:task-definition/sky-migrate-other-a1:1')
        self.assertEqual(status()['schema_migration'], 'unverified')
        job['result']['migration']['task_definition_arn'] = migration['task_definition_arn']
        job['aws_migration_status'] = 'running'
        self.assertEqual(status()['schema_migration'], 'unverified')
        job['aws_migration_status'] = 'succeeded'
        job['status'] = 'failed'
        self.assertEqual(status()['schema_migration'], 'unverified')

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

    def test_snapshot_integrity_requires_source_bundle_and_owned_execution_links(self):
        job_id, account, region = 'a' * 16, '123456789012', 'ap-northeast-2'
        attempt = job_id + '-a1'
        counts = {'posts': 2}
        schema = {'posts': [{'name': 'title', 'type': 'TEXT'}]}
        sql_hash = 'c' * 64
        bundle = hashlib.sha256(b'0000_sky_sqlite_import.sql\0' + bytes.fromhex(sql_hash)).hexdigest()
        evidence = {'protocol': 'sqlite-snapshot-multiset-v1', 'source_revision': 'a' * 64,
                    'prepared_revision': 'b' * 64, 'snapshot_sha256': 'd' * 64,
                    'sql_sha256': sql_hash, 'bundle_digest': bundle, 'row_counts': counts,
                    'schema_sha256': hashlib.sha256(json.dumps(
                        schema, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        migration = {
            'task_arn': f'arn:aws:ecs:{region}:{account}:task/default/' + 'e' * 32,
            'task_definition_arn': f'arn:aws:ecs:{region}:{account}:task-definition/sky-migrate-{attempt}:1',
            'image': f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed:{attempt}-db',
            'image_digest': 'sha256:' + 'f' * 64, 'bundle_digest': bundle,
        }
        check = {'id': 'CV-04', 'status': 'pass', 'source': 'reviewed_sqlite_migration',
                 'integrity': evidence}
        job = {'id': job_id, 'status': 'succeeded', 'target': 'aws-ecs-express', 'attempts': 1,
               'source_digest': 'a' * 64, 'plan': {'source_digest': 'b' * 64},
               'sqlite_conversion': {'source_sha256': 'd' * 64, 'schema': schema, 'row_counts': counts},
               'consistency_checks': [check], 'postgres': {'application_id': 'sample-app'},
               'aws': {'region': region, 'expected_account': account},
               'aws_migration_status': 'succeeded', 'aws_migration_bundle_digest': bundle,
               'aws_migration_result': dict(migration),
               'result': {'url': 'https://example.com', 'region': region, 'account': account,
                          'migration': dict(migration)}}

        def observed():
            return next(item for item in deployment_certificate(job)['verification']
                        if item['name'] == 'cross_environment_data_migration')

        self.assertEqual(observed()['status'], 'passed')
        self.assertEqual(observed()['evidence'], evidence)
        for key, value in list(evidence.items()):
            with self.subTest(key=key):
                evidence[key] = None
                self.assertEqual(observed()['status'], 'unverified')
                evidence[key] = value
        job['consistency_checks'].append(check)
        self.assertEqual(observed()['status'], 'unverified')
        job['consistency_checks'].pop()
        job['aws_migration_status'] = 'running'
        self.assertEqual(observed()['status'], 'unverified')
        job['aws_migration_status'] = 'succeeded'
        job['status'] = 'failed'
        self.assertEqual(observed()['status'], 'unverified')

    def test_model_execution_requires_completed_job_and_valid_response_metadata(self):
        job = {'id': 'a' * 16, 'status': 'succeeded', 'target': 'local-docker',
               'result': {'url': 'http://127.0.0.1:1234'},
               'ai_model_execution': {'provider': 'openai-responses', 'response_id': 'resp_123abc',
                                      'model': 'gpt-5.4-mini', 'response_count': 2,
                                      'recorded_at': '2026-10-09T00:00:00+00:00'}}
        def status():
            return {item['name']: item['status'] for item in deployment_certificate(job)['verification']}['ai_model_execution']
        self.assertEqual(status(), 'passed')
        job['status'] = 'failed'
        self.assertEqual(status(), 'unverified')
        job['status'] = 'succeeded'
        job['ai_model_execution']['response_id'] = 'invalid'
        self.assertEqual(status(), 'unverified')

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

    def test_group_promotion_records_source_image_without_claiming_ecs_task_digest(self):
        source_job_id = 'b' * 16
        job = {'id': 'a' * 16, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'result': {'url': 'https://example.com', 'image': 'example:v1',
                          'image_digest': 'sha256:' + 'c' * 64,
                          'promotion': {'source_job_id': source_job_id,
                                        'image_id': 'sha256:' + 'd' * 64,
                                        'platform': 'linux/amd64'}}}
        certificate = deployment_certificate(job)
        checks = {item['name']: item['status'] for item in certificate['verification']}
        self.assertEqual(checks['cross_target_promotion'], 'passed')
        self.assertEqual(checks['local_rehearsal'], 'unverified')
        self.assertEqual(checks['image_identity'], 'unverified')
        self.assertEqual(certificate['artifact']['local_image_id'], 'sha256:' + 'd' * 64)
        self.assertEqual(certificate['artifact']['promoted_from_job_id'], source_job_id)

    def test_decision_trace_links_source_constraint_and_selected_candidate(self):
        job = {'id': 'a' * 16, 'status': 'succeeded', 'target': 'aws-ecs-express',
               'source_digest': 'a' * 64,
               'application_ir': {'topology_status': 'unresolved',
                                  'requirements': [{'id': 'R-image-platform', 'kind': 'image-platform',
                                                    'evidence_ids': ['E-1']}],
                                  'evidence': [{'id': 'E-1', 'path': 'Dockerfile',
                                                'signal': 'image-platform', 'excerpt': 'private-value'}]},
               'infrastructure_plan': {'planner': 'openai',
                                       'compatibility': {'constraint_results': [
                                          {'rule_id': 'IMAGE-PLATFORM-01', 'status': 'satisfied',
                                            'requirement': 'image-platform',
                                            'evidence_ids': ['E-1'], 'reason': '플랫폼 일치'}]},
                                       'candidates': [{'id': 'aws-ecs-express', 'status': 'eligible',
                                                       'selected': True, 'violated_rule_ids': []}]},
               'result': {'url': 'https://example.com'}}
        trace = deployment_certificate(job)['decision_trace']
        self.assertEqual(trace['status'], 'recorded')
        self.assertEqual(trace['applies_to_uploaded_source_sha256'], 'a' * 64)
        self.assertEqual(trace['source_evidence'][0]['id'], 'E-1')
        self.assertEqual(trace['requirements'][0]['evidence_ids'], ['E-1'])
        self.assertEqual(trace['constraint_results'][0]['evidence_ids'], ['E-1'])
        self.assertTrue(trace['candidate_evaluations'][0]['selected'])
        self.assertEqual(trace['selection_basis'], 'openai')
        self.assertNotIn('private-value', str(trace))

    def test_versioned_evidence_connects_source_constraint_and_runtime_scope(self):
        revision = 'a' * 64
        profile = InfrastructureProfile(
            'unconfirmed', ('server.js',), 1,
            source_signals=(('websocket', ('server.js',)),),
        )
        ir = application_ir(profile, revision).as_dict()
        compatibility = infrastructure_compatibility(profile, 'local-docker', public_access=False)
        job = {'id': 'a' * 16, 'status': 'succeeded', 'target': 'local-docker',
               'source_digest': revision, 'application_ir': ir,
               'infrastructure_plan': {'target': 'local-docker', 'planner': 'user',
                                       'compatibility': compatibility},
               'result': {'url': 'http://127.0.0.1:1234'},
               'websocket_verification': {'status': 'passed',
                                          'checked_at': '2026-10-09T00:00:00+00:00'}}
        certificate = deployment_certificate(job)
        trace = certificate['decision_trace']
        evidence_id = ir['evidence'][0]['id']
        self.assertEqual(trace['status'], 'recorded')
        self.assertEqual(trace['ir_source_revision'], revision)
        self.assertEqual(trace['source_evidence'][0]['id'], evidence_id)
        self.assertEqual(trace['source_evidence'][0]['signal'], 'websocket')
        self.assertEqual(trace['source_evidence'][0]['status'], 'inferred')
        self.assertEqual(trace['unresolved_evidence_ids'], [])
        self.assertEqual(next(item for item in trace['constraint_results']
                              if item['rule_id'] == 'PROTOCOL-WS-01')['evidence_ids'], [evidence_id])
        websocket = next(item for item in certificate['verification']
                         if item['name'] == 'websocket_round_trip')
        self.assertEqual(websocket['status'], 'passed')
        self.assertEqual(websocket['source_evidence_ids'], [evidence_id])
        continuity = next(item for item in certificate['verification']
                          if item['name'] == 'websocket_session_continuity')
        self.assertEqual(continuity['status'], 'unverified')
        self.assertIn('websocket_session_continuity', certificate['unverified'])
        self.assertEqual(ir['evidence'][0]['verified_by'], ())
        ir['evidence'][0]['source']['revision'] = 'b' * 64
        tampered = deployment_certificate(job)['decision_trace']
        self.assertEqual(tampered['status'], 'incomplete')
        self.assertEqual(tampered['source_evidence'], [])
        self.assertEqual(tampered['unresolved_evidence_ids'], [evidence_id])
        tampered_probe = next(item for item in deployment_certificate(job)['verification']
                              if item['name'] == 'websocket_round_trip')
        self.assertEqual(tampered_probe['source_evidence_ids'], [])

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
