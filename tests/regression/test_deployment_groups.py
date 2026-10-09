import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.aws.ecs import AwsSettings
from application.analysis import AISettings
from application.certificate import deployment_certificate
from application.deployment_core import analyze
from application.verification_gates import static_consistency_gate
from interfaces.http.server import App, handler_for


def archive(database=False):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as bundle:
        bundle.writestr('package.json', json.dumps({'scripts': {'start': 'node server.js'}}))
        bundle.writestr('server.js', "require('node:http').createServer((req,res)=>res.end('ok')).listen(3000)")
        if database:
            bundle.writestr('app.db', b'SQLite format 3\x00')
    return data.getvalue()


class DeploymentGroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = App(self.root / 'state', AISettings('fixture-key', 'fixture-model'),
                       aws_settings=AwsSettings('ap-northeast-2', expected_account='123456789012'),
                       monitor_interval=0)
        self.source = self.root / 'upload'
        self.source.mkdir()
        (self.source / 'package.json').write_text(json.dumps({'scripts': {'start': 'node server.js'}}))
        (self.source / 'server.js').write_text(
            "require('node:http').createServer((req,res)=>res.end('ok')).listen(3000)")

    def create(self):
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None):
            return self.app.create_deployment_group(
                self.source, 'demo-app', ['local-docker', 'aws-ecs-express'], True)

    def test_one_upload_reserves_ordered_independent_jobs_and_partial_success(self):
        group = self.create()
        first, second = group['targets']
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertEqual([item['target'] for item in group['targets']],
                         ['local-docker', 'aws-ecs-express'])
        self.assertEqual(self.app.jobs[first['job_id']]['source_digest'],
                         self.app.jobs[second['job_id']]['source_digest'])
        self.assertEqual(self.app.jobs[first['job_id']]['deployment_policy']['allowed_targets'],
                         ('local-docker', 'aws-ecs-express'))
        self.assertEqual(self.app.jobs[first['job_id']]['deployment_policy'],
                         self.app.jobs[second['job_id']]['deployment_policy'])
        self.assertEqual(self.app.jobs[first['job_id']]['architecture_decision']['source_revision'],
                         self.app.jobs[first['job_id']]['source_digest'])
        self.assertEqual(self.app.jobs[second['job_id']]['architecture_decision']['selected_candidate'],
                         'aws-ecs-express')
        for item in (first, second):
            job = self.app.jobs[item['job_id']]
            compilation = job['compilation']
            self.assertEqual(compilation['architecture_decision_id'],
                             job['architecture_decision']['decision_id'])
            self.assertEqual(compilation['target_plan']['target'], job['target'])
            self.assertEqual(compilation['source_patch_plan']['compilation_id'],
                             compilation['deployment_ir']['compilation_id'])
        trace = deployment_certificate(self.app.jobs[first['job_id']])['decision_trace']
        self.assertEqual(trace['status'], 'recorded')
        self.assertEqual(trace['decision_id'],
                         self.app.jobs[first['job_id']]['architecture_decision']['decision_id'])
        calls = []
        def run(job_id):
            calls.append(job_id)
            job = self.app.jobs[job_id]
            job['status'] = 'failed' if job_id == first['job_id'] else 'succeeded'
            if job['status'] == 'succeeded':
                job['result'] = {'url': 'https://example.test'}
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=run):
            self.app.run_group(group['id'])
        result = self.app.deployment_group(group['id'])
        self.assertEqual(calls, [first['job_id'], second['job_id']])
        self.assertEqual(result['status'], 'partial_success')
        self.assertEqual([item['status'] for item in result['targets']], ['failed', 'succeeded'])
        self.assertEqual(result['targets'][1]['url'], 'https://example.test')
        self.app.jobs[second['job_id']]['deployment_state'] = 'deleted'
        self.assertIsNone(self.app.deployment_group(group['id'])['targets'][1]['url'])

    def test_changed_target_is_blocked_before_agent_runs(self):
        group = self.create()
        job_id = group['targets'][0]['job_id']
        self.app.jobs[job_id]['target'] = 'cloud-run'
        self.app.save(job_id)
        with patch('interfaces.http.server.DeploymentAgent.run') as run, \
                patch.object(self.app, 'start_group_worker'):
            self.app.run_agent(job_id)
        run.assert_not_called()
        self.assertEqual(self.app.jobs[job_id]['status'], 'failed')
        self.assertEqual(self.app.jobs[job_id]['attempts'], 0)
        self.assertIn('허용 범위', self.app.jobs[job_id]['events'][-1]['message'])

    def test_changed_architecture_decision_is_blocked_before_agent_runs(self):
        group = self.create()
        job_id = group['targets'][0]['job_id']
        self.app.jobs[job_id]['architecture_decision']['decision_id'] = 'D-' + '0' * 16
        self.app.save(job_id)
        restored = App(self.app.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        self.assertIn(job_id, restored.jobs)
        trace = deployment_certificate(restored.jobs[job_id])['decision_trace']
        self.assertEqual(trace['status'], 'incomplete')
        self.assertIsNone(trace['decision_id'])
        with patch('interfaces.http.server.DeploymentAgent.run') as run, \
                patch.object(restored, 'start_group_worker'):
            restored.run_agent(job_id)
        run.assert_not_called()
        self.assertEqual(restored.jobs[job_id]['status'], 'failed')
        self.assertEqual(restored.jobs[job_id]['attempts'], 0)
        self.assertIn('Stored architecture decision', restored.jobs[job_id]['events'][-1]['message'])

    def test_mixed_compilation_is_blocked_before_agent_runs(self):
        group = self.create()
        job_id = group['targets'][0]['job_id']
        self.app.jobs[job_id]['compilation']['target_plan']['compilation_id'] = 'comp-other'
        self.app.save(job_id)
        restored = App(self.app.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        with patch('interfaces.http.server.DeploymentAgent.run') as run, \
                patch.object(restored, 'start_group_worker'):
            restored.run_agent(job_id)
        run.assert_not_called()
        self.assertEqual(restored.jobs[job_id]['status'], 'failed')
        self.assertEqual(restored.jobs[job_id]['attempts'], 0)
        self.assertIn('Stored compilation', restored.jobs[job_id]['events'][-1]['message'])

    def test_websocket_verification_obligation_stays_pending_until_probe_record(self):
        with (self.source / 'server.js').open('a') as source:
            source.write('\n// WebSocketServer signal for architecture evaluation\n')
        group = self.create()
        job = self.app.jobs[group['targets'][0]['job_id']]
        self.assertIn('PROTOCOL-WS-01', job['architecture_decision']['pending_verification_rule_ids'])
        checks = [{'id': 'CV-03', 'status': 'unknown', 'source': 'final_working_copy'}]
        job['consistency_checks'] = checks
        job['static_consistency_gate'] = static_consistency_gate(
            job['compilation'], job['architecture_decision'], checks)
        pending = deployment_certificate(job)['verification_gates']
        self.assertEqual(pending['status'], 'recorded')
        self.assertEqual(pending['required_obligations'][0]['status'], 'pending')
        job['status'] = 'succeeded'
        job['result'] = {'url': 'http://127.0.0.1:12345'}
        job['websocket_verification'] = {
            'status': 'passed', 'protocol': 'sky.probe.v1',
            'checked_at': '2026-10-09T00:00:00Z'}
        verified = deployment_certificate(job)['verification_gates']
        self.assertEqual(verified['required_obligations'][0]['status'], 'verified')
        self.assertEqual(verified['required_obligations'][1]['status'], 'unverified')
        self.assertEqual(verified['target_verification_status'], 'unverified')
        self.assertIn('CV-03', deployment_certificate(job)['unverified'])
        self.assertEqual(job['static_consistency_gate']['required_obligations'][0]['status'], 'pending')
        job['static_consistency_gate']['compilation_id'] = 'comp-other'
        self.assertEqual(deployment_certificate(job)['verification_gates']['status'], 'incomplete')

    def test_port_obligation_requires_matching_successful_health_probe_record(self):
        group = self.create()
        job = self.app.jobs[group['targets'][0]['job_id']]
        checks = [{'id': 'CV-06', 'status': 'unknown', 'source': 'executable_dockerfile'}]
        job['consistency_checks'] = checks
        job['static_consistency_gate'] = static_consistency_gate(
            job['compilation'], job['architecture_decision'], checks)
        job['plan'] = {'target': 'local-docker', 'port': 3000, 'health_path': '/ready'}
        def obligation():
            return deployment_certificate(job)['verification_gates']['required_obligations'][0]
        self.assertEqual(obligation()['status'], 'pending')
        job['status'] = 'succeeded'
        job['result'] = {'url': 'http://127.0.0.1:12345'}
        self.assertEqual(obligation()['status'], 'pending')
        job['result']['health_url'] = 'http://127.0.0.1:12345/wrong'
        self.assertEqual(obligation()['status'], 'pending')
        job['result']['health_url'] = 'http://127.0.0.1:12345/ready'
        self.assertEqual(obligation()['status'], 'verified')
        self.assertEqual(obligation()['verification_refs'], ['deployment_http'])
        job['plan']['target'] = 'cloud-run'
        self.assertEqual(obligation()['status'], 'pending')
        job['plan']['target'] = 'local-docker'
        job['result']['url'] = 'http://example.test:12345'
        job['result']['health_url'] = 'http://example.test:12345/ready'
        self.assertEqual(obligation()['status'], 'pending')
        self.assertEqual(job['static_consistency_gate']['required_obligations'][0]['status'], 'pending')
        job['status'] = 'failed'
        self.assertEqual(obligation()['status'], 'pending')

    def test_successful_local_image_is_promoted_without_a_second_agent_run(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        image_id = 'sha256:' + 'a' * 64
        calls = []
        def run_local(job_id):
            calls.append(job_id)
            self.assertEqual(job_id, local_id)
            job = self.app.jobs[job_id]
            work = self.app.root / job_id / 'work'
            shutil.copytree(Path(job['project']), work)
            plan = analyze(work)
            job.update(status='succeeded', plan=plan.__dict__, attempts=1,
                       result={'image': f'sky/{local_id}-a1:latest', 'image_id': image_id,
                               'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=run_local), \
                patch('interfaces.http.server.AwsExpressAdapter.deploy',
                      return_value={'url': 'https://example.test',
                                    'health_url': 'https://example.test/',
                                    'promotion': {'image_id': image_id}}) as deploy:
            self.app.run_group(group['id'])
        self.assertEqual(calls, [local_id])
        self.assertEqual(deploy.call_count, 1)
        self.assertEqual(deploy.call_args.args[1].target, 'aws-ecs-express')
        self.assertEqual(self.app.jobs[aws_id]['status'], 'succeeded')
        self.assertEqual(self.app.jobs[aws_id]['attempts'], 1)
        self.assertEqual(self.app.jobs[aws_id]['source_transform']['compilation_id'],
                         self.app.jobs[aws_id]['compilation']['compilation_id'])
        self.assertEqual(self.app.jobs[aws_id]['consistency_checks'][0]['status'], 'unknown')
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'succeeded')

    def test_promoted_aws_result_without_health_path_cannot_finish_successfully(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        image_id = 'sha256:' + 'a' * 64
        def run_local(job_id):
            job = self.app.jobs[job_id]
            work = self.app.root / job_id / 'work'
            shutil.copytree(Path(job['project']), work)
            plan = analyze(work)
            job.update(status='succeeded', plan=plan.__dict__, attempts=1,
                       result={'image': f'sky/{local_id}-a1:latest', 'image_id': image_id,
                               'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=run_local), \
                patch('interfaces.http.server.AwsExpressAdapter.deploy',
                      return_value={'url': 'https://example.test'}), \
                patch('interfaces.http.server.AwsExpressAdapter.cleanup_failure'):
            self.app.run_group(group['id'])
        self.assertEqual(self.app.jobs[local_id]['status'], 'succeeded')
        self.assertEqual(self.app.jobs[aws_id]['status'], 'failed')
        self.assertFalse(self.app.jobs[aws_id].get('result'))

    def test_required_env_pauses_aws_then_resumes_same_image_after_restart(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        image_id = 'sha256:' + 'a' * 64
        def run_local(job_id):
            self.assertEqual(job_id, local_id)
            job = self.app.jobs[job_id]
            work = self.app.root / job_id / 'work'
            shutil.copytree(Path(job['project']), work)
            plan = analyze(work)
            plan.required_env = ['APP_MODE']
            job.update(status='succeeded', plan=plan.__dict__, attempts=1,
                       result={'image': f'sky/{local_id}-a1:latest', 'image_id': image_id,
                               'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=run_local) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(local_id)
        waiting = self.app.jobs[aws_id]
        self.assertEqual(waiting['status'], 'waiting_input')
        self.assertEqual(waiting['missing_environment'], ['APP_MODE'])
        self.assertEqual(waiting['attempts'], 0)
        self.assertEqual(waiting['promotion_source_job_id'], local_id)
        self.assertEqual(waiting['source_transform']['target_plan_id'],
                         waiting['compilation']['target_plan']['id'])
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'waiting_input')
        secret = 'synthetic-private-value'
        self.assertNotIn(secret, (self.app.root / aws_id / 'job.json').read_text())
        restored = App(self.app.root, AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        self.assertEqual(restored.jobs[aws_id]['status'], 'waiting_input')
        restored.jobs[aws_id]['status'] = 'running'
        restored.save(aws_id)
        supplied = {'APP_MODE': secret}
        received = []
        def deploy(_project, _plan, _attempt_id, environment):
            received.append(dict(environment))
            return {'url': 'https://example.test', 'health_url': 'https://example.test/',
                    'promotion': {'source_job_id': local_id}}
        with patch('interfaces.http.server.AwsExpressAdapter.deploy', side_effect=deploy) as aws_deploy, \
                patch.object(restored, 'start_group_worker') as continue_group:
            restored.resume_promoted_aws(aws_id, supplied)
        self.assertEqual(aws_deploy.call_count, 1)
        self.assertEqual(received, [{'APP_MODE': secret}])
        self.assertEqual(supplied, {})
        self.assertEqual(restored.jobs[aws_id]['status'], 'succeeded')
        self.assertEqual(restored.jobs[aws_id]['attempts'], 1)
        self.assertNotIn(secret, (self.app.root / aws_id / 'job.json').read_text())
        self.assertEqual(restored.deployment_group(group['id'])['status'], 'succeeded')
        continue_group.assert_called_once_with(group['id'])

    def test_changed_work_cannot_resume_promoted_aws(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        work = self.app.root / local_id / 'work'
        shutil.copytree(Path(self.app.jobs[local_id]['project']), work)
        plan = analyze(work)
        plan.required_env = ['APP_MODE']
        self.app.jobs[local_id].update(status='succeeded', plan=plan.__dict__, attempts=1,
            result={'image': f'sky/{local_id}-a1:latest', 'image_id': 'sha256:' + 'a' * 64,
                    'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
        self.app.save(local_id)
        with patch.object(self.app, 'run_agent') as agent:
            self.app.run_group(group['id'])
        agent.assert_not_called()
        aws_work = self.app.root / aws_id / 'work'
        (aws_work / 'server.js').write_text('changed after waiting')
        self.app.jobs[aws_id]['status'] = 'running'
        self.app.save(aws_id)
        supplied = {'APP_MODE': 'synthetic-private-value'}
        with patch('interfaces.http.server.AwsExpressAdapter.deploy') as deploy, \
                patch.object(self.app, 'start_group_worker'):
            self.app.resume_promoted_aws(aws_id, supplied)
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[aws_id]['status'], 'failed')
        self.assertEqual(supplied, {})

    def test_resume_http_routes_waiting_promotion_to_promotion_worker(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        job = self.app.jobs[aws_id]
        work = self.app.root / aws_id / 'work'
        shutil.copytree(Path(job['project']), work)
        plan = analyze(work)
        plan.target = 'aws-ecs-express'
        plan.required_env = ['APP_SECRET']
        job.update(status='waiting_input', plan=plan.__dict__, missing_environment=['APP_SECRET'],
                   promotion_source_job_id=local_id, work_digest=plan.source_digest)
        self.app.save(aws_id)
        payload = json.dumps({'environment': {'APP_SECRET': 'synthetic-private-value'}}).encode()
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = f'/api/deployments/{aws_id}/resume'
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': str(len(payload))}
        handler.json_response = Mock()
        with patch.object(self.app, 'start_job_worker', return_value=True) as start:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        self.assertEqual(start.call_args.args[1], self.app.resume_promoted_aws)
        self.assertEqual(self.app.jobs[aws_id]['status'], 'running')
        self.assertNotIn('synthetic-private-value', (self.app.root / aws_id / 'job.json').read_text())

    def test_promotion_failure_does_not_record_environment_value(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        local = self.app.jobs[local_id]
        work = self.app.root / local_id / 'work'
        shutil.copytree(Path(local['project']), work)
        plan = analyze(work)
        plan.required_env = ['APP_MODE']
        local.update(status='succeeded', plan=plan.__dict__, attempts=1,
                     result={'image': f'sky/{local_id}-a1:latest',
                             'image_id': 'sha256:' + 'a' * 64,
                             'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
        self.app.save(local_id)
        self.app.jobs[aws_id]['status'] = 'running'
        self.app.save(aws_id)
        secret = 'synthetic-private-value'
        supplied = {'APP_MODE': secret}
        with patch('interfaces.http.server.AwsExpressAdapter.deploy',
                   side_effect=RuntimeError('failed: ' + secret)), \
                patch('interfaces.http.server.AwsExpressAdapter.cleanup_failure'):
            self.app.run_promoted_aws(aws_id, local_id, supplied)
        self.assertEqual(self.app.jobs[aws_id]['status'], 'failed')
        self.assertEqual(supplied, {})
        self.assertNotIn(secret, str(self.app.jobs[aws_id]))
        self.assertNotIn(secret, (self.app.root / aws_id / 'job.json').read_text())
        self.assertIn('[REDACTED]', self.app.jobs[aws_id]['events'][-1]['message'])

    def test_promotion_rejects_supplied_secret_before_aws_attempt(self):
        group = self.create()
        local_id, aws_id = [item['job_id'] for item in group['targets']]
        local = self.app.jobs[local_id]
        work = self.app.root / local_id / 'work'
        shutil.copytree(Path(local['project']), work)
        plan = analyze(work)
        plan.required_env = ['APP_SECRET']
        local.update(status='succeeded', plan=plan.__dict__, attempts=1,
                     result={'image': f'sky/{local_id}-a1:latest',
                             'image_id': 'sha256:' + 'a' * 64,
                             'platform': 'linux/amd64', 'url': 'http://127.0.0.1:12345'})
        self.app.save(local_id)
        self.app.jobs[aws_id]['status'] = 'running'
        self.app.save(aws_id)
        value = 'synthetic-private-value'
        supplied = {'APP_SECRET': value}
        with patch('interfaces.http.server.AwsExpressAdapter.deploy') as deploy:
            self.app.run_promoted_aws(aws_id, local_id, supplied)
        deploy.assert_not_called()
        self.assertEqual(self.app.jobs[aws_id]['attempts'], 0)
        self.assertEqual(self.app.jobs[aws_id]['status'], 'failed')
        self.assertEqual(supplied, {})
        self.assertNotIn(value, str(self.app.jobs[aws_id]))
        self.assertIn('CV-07', self.app.jobs[aws_id]['events'][-1]['message'])

    def test_waiting_for_environment_pauses_next_target(self):
        group = self.create()
        first, second = [item['job_id'] for item in group['targets']]
        def wait_for_input(job_id):
            self.app.jobs[job_id]['status'] = 'waiting_input'
            self.app.save(job_id)
        with patch.object(self.app, 'run_agent', side_effect=wait_for_input) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(first)
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'waiting_input')
        self.app.jobs[first]['status'] = 'succeeded'
        self.app.save(first)
        with patch.object(self.app, 'run_agent', side_effect=lambda job_id:
                          self.app.jobs[job_id].update(status='failed')) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(second)

    def test_unexpected_runner_error_stops_before_next_target(self):
        group = self.create()
        first, second = [item['job_id'] for item in group['targets']]
        with patch.object(self.app, 'run_agent', side_effect=RuntimeError('runner stopped')) as run:
            self.app.run_group(group['id'])
        run.assert_called_once_with(first)
        self.assertEqual(self.app.jobs[first]['status'], 'interrupted')
        self.assertEqual(self.app.jobs[second]['status'], 'planned')
        self.assertEqual(self.app.deployment_group(group['id'])['status'], 'interrupted')

    def test_restart_marks_running_child_interrupted_without_replaying_it(self):
        group = self.create()
        first, second = [item['job_id'] for item in group['targets']]
        self.app.jobs[first]['status'] = 'running'
        self.app.save(first)
        restored = App(self.root / 'state', AISettings('fixture-key', 'fixture-model'),
                       aws_settings=self.app.aws_settings, monitor_interval=0)
        self.assertEqual(restored.jobs[first]['status'], 'interrupted')
        self.assertEqual(restored.jobs[second]['status'], 'planned')
        self.assertEqual(restored.deployment_group(group['id'])['status'], 'interrupted')
        with patch.object(restored, 'run_agent', side_effect=lambda job_id:
                          restored.jobs[job_id].update(status='succeeded')) as run:
            restored.run_group(group['id'])
        run.assert_called_once_with(second)

    def test_database_app_is_rejected_before_any_job_is_recorded(self):
        (self.source / 'app.db').write_bytes(b'SQLite format 3\x00')
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None):
            with self.assertRaisesRegex(ValueError, 'SQLite'):
                self.app.create_deployment_group(
                    self.source, 'demo-app', ['local-docker', 'aws-ecs-express'], True)
        self.assertFalse(self.app.jobs)

    def test_group_http_endpoint_accepts_one_zip_and_rejects_database_headers(self):
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.path = '/api/deployment-groups'
        handler.rfile = io.BytesIO(archive())
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': str(len(archive())),
                           'X-Application-Id': 'demo-app',
                           'X-Deploy-Targets': 'local-docker,aws-ecs-express',
                           'X-Public-Access': 'true'}
        handler.json_response = Mock()
        with patch.object(AwsSettings, 'unavailable_reason', return_value=None), \
             patch.object(self.app, 'start_group_worker', return_value=True):
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        self.assertEqual(len(self.app.jobs), 2)
        handler.headers['X-Postgres-Existing'] = 'true'
        handler.rfile = io.BytesIO(archive())
        handler.json_response.reset_mock()
        handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        self.assertEqual(len(self.app.jobs), 2)

    def test_read_and_continue_require_a_real_interrupted_group(self):
        group = self.create()
        first = group['targets'][0]['job_id']
        handler = handler_for(self.app).__new__(handler_for(self.app))
        handler.headers = {'X-Sky-Token': self.app.token, 'Content-Length': '0'}
        handler.json_response = Mock()
        handler.path = '/api/deployment-groups/' + group['id']
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        handler.path += '/continue'
        handler.json_response.reset_mock()
        with patch.object(self.app, 'start_group_worker') as start:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)
        start.assert_not_called()
        self.app.jobs[first]['status'] = 'interrupted'
        self.app.save(first)
        handler.json_response.reset_mock()
        with patch.object(self.app, 'start_group_worker', return_value=True) as start:
            handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 202)
        start.assert_called_once_with(group['id'])


if __name__ == '__main__':
    unittest.main()
