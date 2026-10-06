import io
import hashlib
import json
import shutil
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tests.support.agent_fixture import RepairFixture, call
from application.agent import AgentError, DeploymentAgent, DeploymentCancelled, DeploymentTools, NeedsEnvironment, OpenAIDeployAgent, MAX_AGENT_REQUEST_BYTES, COMPACT_AGENT_REQUEST_BYTES
from application.analysis import AISettings
from adapters.local.docker import LocalDockerAdapter
from interfaces.http.server import App


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original'
        shutil.copytree('tests/fixtures/apps/unready-node', self.original)
        self.events, self.updates = [], []
        self.tools = DeploymentTools(self.original, self.root / 'work', 'a' * 16, {},
            lambda s, m: self.events.append((s, m)), lambda **u: self.updates.append(u), adapter_factory=LocalDockerAdapter)

    def test_patches_working_copy_only_and_invalidates_plan(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        self.tools.read_project_files(['server.js'])
        self.tools.apply_project_patch('server.js', "'127.0.0.1'", "'0.0.0.0'")
        self.assertIn("'127.0.0.1'", (self.original / 'server.js').read_text())
        self.assertIsNone(self.tools.plan)
        with self.assertRaisesRegex(ValueError, 'Configure'):
            self.tools.deploy_application()

    def test_external_source_change_requires_new_plan_before_attempt(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}',
                                       '"scripts": {"start": "node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        (self.tools.work / 'server.js').write_text('changed after configuration')
        with patch.object(LocalDockerAdapter, 'deploy') as deploy:
            with self.assertRaisesRegex(ValueError, '배포 설정 이후 변경'):
                self.tools.deploy_application()
        self.assertEqual(self.tools.attempts, 0)
        deploy.assert_not_called()

    def test_patch_requires_read_and_exact_match(self):
        with self.assertRaisesRegex(ValueError, 'Read'):
            self.tools.apply_project_patch('server.js', 'http', 'https')
        self.tools.read_project_files(['server.js'])
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.tools.apply_project_patch('server.js', 'not-found', 'new')

    def test_patch_write_failure_preserves_existing_file_and_plan(self):
        target = self.tools.work / 'server.js'
        before = target.read_bytes()
        self.tools.read_project_files(['server.js'])
        plan = object()
        self.tools.plan = plan
        with patch('application.agent.os.replace', side_effect=OSError('write failed')):
            with self.assertRaisesRegex(OSError, 'write failed'):
                self.tools.apply_project_patch('server.js', "'127.0.0.1'", "'0.0.0.0'")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.tools.plan, plan)
        self.assertEqual(self.tools.read_versions['server.js'], before.decode())
        self.assertFalse(list(self.tools.work.parent.glob('.sky-patch-*')))
        self.assertFalse(any(update.get('change') for update in self.updates))

    def test_patch_preserves_executable_mode(self):
        target = self.tools.work / 'start.sh'
        target.write_text('#!/bin/sh\necho old\n')
        target.chmod(0o755)
        self.tools.read_project_files(['start.sh'])
        self.tools.apply_project_patch('start.sh', 'old', 'new')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertIn('new', target.read_text())

    def test_patch_rejects_external_oversized_change(self):
        target = self.tools.work / 'server.js'
        self.tools.read_project_files(['server.js'])
        target.write_text('x' * 40001)
        with self.assertRaisesRegex(ValueError, '40,000 character patch limit'):
            self.tools.apply_project_patch('server.js', 'x', 'y')
        self.assertEqual(target.stat().st_size, 40001)
        self.assertFalse(list(self.tools.work.parent.glob('.sky-patch-*')))

    def test_patch_keeps_full_review_diff_out_of_ai_tool_history(self):
        content = 'x' * 4096
        result = self.tools.apply_project_patch('large.js', '', content)
        self.assertLess(len(json.dumps(result)), 250)
        self.assertNotIn('diff', result)
        self.assertEqual(result['patch_sha256'], hashlib.sha256(content.encode()).hexdigest())
        self.assertIn(content, self.updates[-1]['change']['diff'])
        self.assertEqual((self.tools.work / 'large.js').read_text(), content)

    def test_large_source_file_is_not_returned_to_the_agent(self):
        (self.tools.work / 'large.js').write_text('x' * 30000)
        result = self.tools.read_project_files(['large.js'])
        self.assertEqual(result['files']['large.js'],
                         {'error': 'File exceeds 20,000 character read limit'})
        self.assertNotIn('large.js', self.tools.read_versions)

    def test_oversized_agent_history_is_rejected_before_api_call(self):
        with patch('application.agent.urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(AgentError, '1 MiB'):
                OpenAIDeployAgent(AISettings('fake', 'model')).next([
                    {'role': 'user', 'content': 'x' * MAX_AGENT_REQUEST_BYTES}])
            opener.assert_not_called()

    def test_agent_compacts_long_stateless_history_before_next_tool_call(self):
        history = [{'role': 'user', 'content': 'x' * COMPACT_AGENT_REQUEST_BYTES}]
        compacted = [{'type': 'compaction', 'id': 'cmp_1', 'encrypted_content': 'opaque-state'}]
        completion = {'status': 'completed', 'output': call('read_runtime_logs', {})}
        with patch('application.agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [
                io.BytesIO(json.dumps({'object': 'response.compaction', 'output': compacted}).encode()),
                io.BytesIO(json.dumps(completion).encode()),
            ]
            result = OpenAIDeployAgent(AISettings('fake', 'model')).next(history)
        self.assertEqual(result, completion['output'])
        self.assertEqual(history, compacted)
        requests = [entry.args[0] for entry in opener.return_value.open.call_args_list]
        self.assertEqual([request.full_url for request in requests], [
            'https://api.openai.com/v1/responses/compact',
            'https://api.openai.com/v1/responses'])
        self.assertEqual(json.loads(requests[0].data)['input'][0]['content'], 'x' * COMPACT_AGENT_REQUEST_BYTES)
        self.assertEqual(json.loads(requests[1].data)['input'], compacted)
        self.assertFalse(json.loads(requests[1].data)['store'])

    def test_invalid_compaction_keeps_history_and_stops_before_next_model_call(self):
        history = [{'role': 'user', 'content': 'x' * COMPACT_AGENT_REQUEST_BYTES}]
        with patch('application.agent.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"output": []}')
            with self.assertRaisesRegex(AgentError, '압축 응답 형식'):
                OpenAIDeployAgent(AISettings('fake', 'model')).next(history)
        self.assertEqual(len(history), 1)
        opener.return_value.open.assert_called_once()

    def test_unsupported_compaction_uses_original_bounded_request_once(self):
        history = [{'role': 'user', 'content': 'x' * COMPACT_AGENT_REQUEST_BYTES}]
        unsupported = urllib.error.HTTPError('https://api.openai.com/v1/responses/compact',
            400, 'unsupported', {}, io.BytesIO(b'{}'))
        completion = {'status': 'completed', 'output': call('read_runtime_logs', {})}
        with patch('application.agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [
                unsupported, io.BytesIO(json.dumps(completion).encode()),
                io.BytesIO(json.dumps(completion).encode())]
            agent = OpenAIDeployAgent(AISettings('fake', 'model'))
            agent.next(history)
            agent.next(history)
        requests = [entry.args[0] for entry in opener.return_value.open.call_args_list]
        self.assertEqual([request.full_url for request in requests], [
            'https://api.openai.com/v1/responses/compact',
            'https://api.openai.com/v1/responses',
            'https://api.openai.com/v1/responses'])
        self.assertEqual(json.loads(requests[1].data)['input'], history)

    def test_paths_cannot_escape_working_directory(self):
        for name in ('../secret.js', '/tmp/secret.js', '.env', '.git/config.json', 'Dockerfile', 'package-lock.json', 'node_modules/x.js'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.tools.file_path(name)
        (self.tools.work / 'link.js').symlink_to(self.original / 'server.js')
        with self.assertRaises(ValueError):
            self.tools.read_project_files(['link.js'])

    def test_existing_dockerfile_can_be_repaired_in_working_copy(self):
        original_file = self.original / 'Dockerfile'
        original_file.write_text('FROM node:22-alpine\nCMD ["npm", "start"]\n')
        custom = DeploymentTools(self.original, self.root / 'custom-work', 'b' * 16, {},
            lambda s, m: self.events.append((s, m)), lambda **u: self.updates.append(u), adapter_factory=LocalDockerAdapter)
        self.assertIn('Dockerfile', custom.read_project_files(['Dockerfile'])['files'])
        custom.apply_project_patch('Dockerfile', 'FROM node:22-alpine', 'FROM node:22-bookworm-slim')
        custom.configure_deployment('dockerfile', None, 3000, '/', [])
        self.assertEqual(custom.plan.dockerfile_source, 'existing')
        self.assertIn('bookworm-slim', custom.plan.dockerfile)
        self.assertIn('node:22-alpine', original_file.read_text())

    def test_python_dockerfile_project_can_be_repaired(self):
        original = self.root / 'python-original'
        original.mkdir()
        (original / 'Dockerfile').write_text('FROM python:3.13-alpine\nCOPY . /app\nCMD ["python", "/app/server.py"]\n')
        (original / 'server.py').write_text('host = "127.0.0.1"\n')
        custom = DeploymentTools(original, self.root / 'python-work', 'c' * 16, {},
            lambda s, m: self.events.append((s, m)), lambda **u: self.updates.append(u), adapter_factory=LocalDockerAdapter)
        custom.read_project_files(['Dockerfile', 'server.py'])
        custom.apply_project_patch('server.py', '127.0.0.1', '0.0.0.0')
        custom.configure_deployment('dockerfile', None, 3000, '/', [])
        self.assertEqual(custom.plan.runtime, 'custom-dockerfile')
        self.assertIn('127.0.0.1', (original / 'server.py').read_text())
        self.assertIn('0.0.0.0', (custom.work / 'server.py').read_text())

    def test_environment_pause_never_returns_values(self):
        with self.assertRaises(NeedsEnvironment) as raised:
            self.tools.request_environment(['DATABASE_URL'], 'DB 접속값이 필요합니다.')
        self.assertEqual(raised.exception.names, ['DATABASE_URL'])
        self.tools.environment['DATABASE_URL'] = 'secret-database-url'
        self.assertEqual(self.tools.request_environment(['DATABASE_URL'], 'DB'), {'available': True})
        self.tools.event('output', 'value=secret-database-url')
        self.assertNotIn('secret-database-url', str(self.events))

    def test_cannot_finish_by_claiming_success(self):
        class Provider:
            def next(self, history):
                return [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Deployment successful'}]}]
        with self.assertRaises(AgentError):
            DeploymentAgent(Provider(), self.tools).run()
        self.assertIsNone(self.tools.result)

    def test_unknown_tools_never_execute_and_steps_are_bounded(self):
        class Provider:
            def next(self, history):
                return call('shell', {'command': 'bad'})
        with self.assertRaisesRegex(AgentError, '횟수 제한'):
            DeploymentAgent(Provider(), self.tools, max_steps=2).run()
        self.assertEqual(len([u for u in self.updates if 'steps' in u]), 2)

    def test_failed_deployment_is_returned_to_agent_then_repaired(self):
        attempts = []
        class Adapter:
            def __init__(self, event):
                self.event = event
            def deploy(self, project, plan, attempt_id, environment):
                attempts.append(plan)
                if len(attempts) == 1:
                    self.event('output', 'HTTP refused: bound to localhost')
                    raise RuntimeError('HTTP connection failed')
                self.assert_binding = "'0.0.0.0'" in (project / 'server.js').read_text()
                assert self.assert_binding
                return {'url': 'http://127.0.0.1:1234'}
            def command(self, args, timeout):
                return ''
            def cleanup_failure(self, attempt_id):
                pass
        self.tools.adapter_factory = Adapter
        result = DeploymentAgent(RepairFixture(), self.tools).run()
        self.assertEqual(result['url'], 'http://127.0.0.1:1234')
        self.assertEqual(self.tools.attempts, 2)
        self.assertEqual(attempts[-1].port, 3000)

    def test_accepted_aws_update_stops_automatic_retry(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        class Adapter:
            updated_existing = True
            previous_deployment_arn = 'old-deployment'
            image = 'example.amazonaws.com/sky-managed:aaaaaaaaaaaaaaaa-a1'
            def __init__(self, event):
                self.event = event
            def deploy(self, *_args):
                raise RuntimeError('HTTP verification failed')
            def cleanup_failure(self, _attempt_id):
                pass
        self.tools.adapter_factory = Adapter
        with self.assertRaisesRegex(AgentError, '자동 재시도를 중단'):
            self.tools.deploy_application()
        self.assertEqual(self.tools.attempts, 1)
        self.assertTrue(any(update.get('aws_update_submitted') for update in self.updates))

    def test_three_attempt_limit_survives_new_agent_run(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        self.tools.attempts = 3
        with self.assertRaisesRegex(AgentError, '2회'):
            self.tools.deploy_application()

    def test_cancelled_before_first_attempt_never_calls_adapter(self):
        self.tools.read_project_files(['package.json'])
        self.tools.apply_project_patch('package.json', '"scripts": {}', '"scripts":{"start":"node server.js"}')
        self.tools.configure_deployment('start', None, 4321, '/', [])
        self.tools.cancel_check = lambda: True
        with patch.object(LocalDockerAdapter, 'deploy') as deploy:
            with self.assertRaises(DeploymentCancelled):
                self.tools.deploy_application()
            deploy.assert_not_called()
        self.assertEqual(self.tools.attempts, 0)

    def test_tool_protocol_preserves_reasoning_items(self):
        output = [{'type': 'reasoning', 'encrypted_content': 'opaque'}, *call('read_runtime_logs', {})]
        with patch('application.agent.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps({'status': 'completed', 'output': output}).encode())
            result = OpenAIDeployAgent(AISettings('fake', 'model')).next([{'role': 'user', 'content': 'deploy'}])
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result, output)
        self.assertFalse(payload['parallel_tool_calls'])
        self.assertFalse(payload['store'])
        self.assertIn('reasoning.encrypted_content', payload['include'])

    def test_incomplete_response_never_exposes_partial_tool_call(self):
        body = {'status': 'incomplete',
                'incomplete_details': {'reason': 'max_output_tokens'},
                'output': call('deploy_application', {})}
        with patch('application.agent.urllib.request.build_opener') as opener, \
                patch.object(LocalDockerAdapter, 'deploy') as deploy:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(body).encode())
            with self.assertRaisesRegex(AgentError, '토큰 한도'):
                DeploymentAgent(OpenAIDeployAgent(AISettings('fake', 'model')), self.tools).run()
            deploy.assert_not_called()
        self.assertEqual(self.tools.attempts, 0)

    def test_temporary_rate_limit_retries_once_after_server_delay(self):
        failure = urllib.error.HTTPError('https://api.openai.com/v1/responses', 429, 'rate limit',
                                         {'Retry-After': '2'}, io.BytesIO(json.dumps({
                                             'error': {'code': 'slow_down'}}).encode()))
        output = call('read_runtime_logs', {})
        success = io.BytesIO(json.dumps({'status': 'completed', 'output': output}).encode())
        with patch('application.agent.urllib.request.build_opener') as opener, \
                patch('adapters.ai.openai_http.time.sleep') as sleep, \
                patch('adapters.ai.openai_http.random.uniform', return_value=0.1):
            opener.return_value.open.side_effect = [failure, success]
            result = OpenAIDeployAgent(AISettings('fake', 'model')).next(
                [{'role': 'user', 'content': 'deploy'}])
        self.assertEqual(result, output)
        self.assertEqual(opener.return_value.open.call_count, 2)
        self.assertGreaterEqual(sleep.call_args.args[0], 2)

    def test_quota_error_and_long_server_delay_are_not_retried(self):
        for status, code, headers in [
                (429, 'credit_balance_exhausted', {}),
                (503, 'server_is_overloaded', {'Retry-After': '30'})]:
            with self.subTest(status=status, code=code):
                failure = urllib.error.HTTPError('https://api.openai.com/v1/responses',
                    status, 'unavailable', headers,
                    io.BytesIO(json.dumps({'error': {'code': code}}).encode()))
                with patch('application.agent.urllib.request.build_opener') as opener, \
                        patch('adapters.ai.openai_http.time.sleep') as sleep:
                    opener.return_value.open.side_effect = failure
                    with self.assertRaisesRegex(AgentError, f'HTTP {status}'):
                        OpenAIDeployAgent(AISettings('fake', 'model')).next(
                            [{'role': 'user', 'content': 'deploy'}])
                opener.return_value.open.assert_called_once()
                sleep.assert_not_called()

    def test_server_error_retries_only_once(self):
        failures = [urllib.error.HTTPError('https://api.openai.com/v1/responses',
                    500, 'server error', {}, io.BytesIO(b'{}')) for _ in range(2)]
        with patch('application.agent.urllib.request.build_opener') as opener, \
                patch('adapters.ai.openai_http.time.sleep') as sleep:
            opener.return_value.open.side_effect = failures
            with self.assertRaisesRegex(AgentError, '일시 오류 HTTP 500'):
                OpenAIDeployAgent(AISettings('fake', 'model')).next(
                    [{'role': 'user', 'content': 'deploy'}])
        self.assertEqual(opener.return_value.open.call_count, 2)
        sleep.assert_called_once()

    def test_waiting_job_and_unconfigured_agent_job_restore(self):
        root = self.root / 'state'
        job_id = 'b' * 16
        project = root / job_id / 'source'
        shutil.copytree(self.original, project)
        app = App(root, AISettings())
        app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'plan': None, 'status': 'waiting_input',
                            'project': str(project), 'events': [], 'missing_environment': ['DB_URL']}
        app.save(job_id)
        restored = App(root, AISettings())
        self.assertEqual(restored.jobs[job_id]['status'], 'waiting_input')
        self.assertEqual(restored.summaries()[0]['analyzer'], 'agent')


if __name__ == '__main__':
    unittest.main()
