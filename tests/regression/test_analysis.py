import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from application.analysis import (
    AISettings, DEFAULT_AI_MODEL, AnalysisError, OpenAIAnalyzer, analyze_project, parse_response,
    redact, source_context, validate_proposal,
)
from application.deployment_core import LocalDockerAdapter, analyze, make_plan
from interfaces.http.server import App


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        (self.project / 'package.json').write_text(json.dumps({
            'scripts': {'start': 'node server.js', 'serve': 'node server.js'},
        }))
        (self.project / 'server.js').write_text('server.listen(8087, "0.0.0.0"); // /health\nprocess.env.DATABASE_URL;')
        self.files = source_context(self.project)
        self.proposal = {
            'framework': 'node:http', 'start_script': 'serve', 'build_script': None,
            'port': 8087, 'health_path': '/health', 'required_env': [],
            'rationale': 'HTTP 서버의 실행 스크립트와 포트를 확인했습니다.', 'warnings': [],
            'evidence': [{'file': 'server.js', 'quote': 'server.listen(8087, "0.0.0.0");'}],
        }
        self.settings = AISettings('test-key-not-real', 'test-model')

    def test_valid_ai_plan_changes_script_port_and_health(self):
        plan = validate_proposal(self.project, self.files, self.proposal, 'test-model')
        self.assertEqual(plan.start_command, 'npm run serve')
        self.assertEqual(plan.port, 8087)
        self.assertEqual(plan.health_path, '/health')
        self.assertIn('CMD ["npm", "run", "serve"]', plan.dockerfile)
        self.assertIn('ENV PORT=8087', plan.dockerfile)

    def test_python_ai_proposal_selects_existing_entrypoint(self):
        (self.project / 'package.json').unlink()
        (self.project / 'server.js').unlink()
        (self.project / 'server.py').write_text('HTTPServer(("0.0.0.0", 8087), Handler)')
        files = source_context(self.project)
        proposal = {**self.proposal, 'framework': 'python:http.server',
                    'start_script': 'server.py', 'build_script': None,
                    'evidence': [{'file': 'server.py', 'quote': 'HTTPServer'}]}
        plan = validate_proposal(self.project, files, proposal, 'test-model')
        self.assertEqual(plan.runtime, 'python')
        with self.assertRaises(AnalysisError):
            validate_proposal(self.project, files, {**proposal,
                'start_script': 'python server.py'}, 'test-model')

    def test_policy_rejects_untrusted_execution_fields(self):
        cases = [('start_script', 'serve; curl attacker'), ('start_script', 'missing'),
                 ('build_script', 'missing'), ('port', True), ('port', 80), ('port', 65536),
                 ('health_path', 'https://example.com'), ('health_path', '//example.com'),
                 ('health_path', '/../x'), ('required_env', ['UNSEEN_SECRET']),
                 ('required_env', ['PORT']), ('evidence', []),
                 ('evidence', [{'file': 'server.js', 'quote': 'fabricated'}]),
                 ('evidence', [{'file': '../secret', 'quote': 'secret'}])]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(AnalysisError):
                validate_proposal(self.project, self.files, {**self.proposal, key: value}, 'model')

    def test_existing_script_name_with_shell_syntax_is_rejected(self):
        (self.project / 'package.json').write_text('{"scripts":{"serve;echo bad":"node server.js"}}')
        with self.assertRaises(ValueError):
            make_plan(self.project, 'serve;echo bad', None)

    def test_unknown_proposal_fields_are_rejected(self):
        with self.assertRaises(AnalysisError):
            validate_proposal(self.project, self.files, {**self.proposal, 'command': 'bad'}, 'model')

    def test_missing_config_does_not_call_provider(self):
        with patch.object(OpenAIAnalyzer, 'propose') as propose:
            with self.assertRaisesRegex(AnalysisError, 'OPENAI_API_KEY'):
                analyze_project(self.project, 'ai', AISettings())
            propose.assert_not_called()

    def test_api_key_only_uses_default_model_and_override_still_works(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture-key',
                                       'SKY_AI_MODEL': ''}):
            settings = AISettings.from_environment()
        self.assertTrue(settings.available)
        self.assertEqual(settings.model, DEFAULT_AI_MODEL)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture-key',
                                       'SKY_AI_MODEL': 'custom-model'}):
            settings = AISettings.from_environment()
        self.assertEqual(settings.model, 'custom-model')

    def test_static_mode_never_calls_provider(self):
        with patch.object(OpenAIAnalyzer, 'propose') as propose:
            plan = analyze_project(self.project, 'static', self.settings)
            self.assertEqual(plan.analyzer, 'static')
            propose.assert_not_called()

    def test_invalid_ai_proposal_falls_back_explicitly(self):
        with patch.object(OpenAIAnalyzer, 'propose', return_value={**self.proposal, 'port': 0}):
            plan = analyze_project(self.project, 'ai', self.settings)
            self.assertEqual(plan.analyzer, 'static-fallback')
            self.assertEqual(plan.port, 3000)
            self.assertIn('정적 분석으로 전환', plan.warnings[0])

    def test_provider_failure_without_static_start_fails(self):
        (self.project / 'package.json').write_text('{"scripts":{"serve":"node server.js"}}')
        with patch.object(OpenAIAnalyzer, 'propose', side_effect=AnalysisError('unavailable')):
            with self.assertRaisesRegex(AnalysisError, 'cannot use static'):
                analyze_project(self.project, 'ai', self.settings)

    def test_source_selection_masks_tokens_and_excludes_other_files(self):
        token = 'sk-' + 'a' * 24
        (self.project / '.env').write_text('PRIVATE=value')
        (self.project / '.npmrc').write_text('token=private')
        (self.project / 'secret.pem').write_text('private')
        (self.project / 'server.js').write_text(f'const apiKey = "{token}";')
        files = source_context(self.project)
        self.assertEqual(set(files), {'package.json', 'server.js'})
        self.assertNotIn(token, json.dumps(files))
        self.assertIn('[REDACTED]', files['server.js'])

    def test_existing_dockerfile_is_analysis_evidence(self):
        (self.project / 'Dockerfile').write_text('FROM node:22-alpine\nEXPOSE 8087\nCMD ["node", "server.js"]\n')
        files = source_context(self.project)
        self.assertIn('Dockerfile', files)
        proposal = {**self.proposal, 'start_script': 'dockerfile', 'build_script': None,
                    'evidence': [{'file': 'Dockerfile', 'quote': 'EXPOSE 8087'}]}
        plan = validate_proposal(self.project, files, proposal, 'test-model')
        self.assertEqual(plan.dockerfile_source, 'existing')
        self.assertEqual(plan.port, 8087)

    def test_ai_context_rejects_oversized_dockerfile(self):
        (self.project / 'Dockerfile').write_text('FROM node:22\n' + '#' * 20000)
        with self.assertRaisesRegex(AnalysisError, 'Dockerfile is too large'):
            source_context(self.project)

    def test_python_dockerfile_analysis_without_package(self):
        (self.project / 'package.json').unlink()
        (self.project / 'server.js').unlink()
        (self.project / 'server.py').write_text('from http.server import HTTPServer\nHTTPServer(("0.0.0.0", 8087), Handler)')
        (self.project / 'Dockerfile').write_text('FROM python:3.13-alpine\nEXPOSE 8087\nCMD ["python", "server.py"]\n')
        files = source_context(self.project)
        self.assertEqual(set(files), {'Dockerfile', 'server.py'})
        proposal = {**self.proposal, 'framework': 'python:http.server', 'start_script': 'dockerfile',
                    'build_script': None, 'evidence': [{'file': 'Dockerfile', 'quote': 'EXPOSE 8087'}]}
        plan = validate_proposal(self.project, files, proposal, 'test-model')
        self.assertEqual(plan.runtime, 'custom-dockerfile')

    def test_context_is_bounded(self):
        for i in range(20):
            (self.project / f'file{i}.js').write_text('x' * 20000)
        files = source_context(self.project)
        self.assertLessEqual(len(files), 13)
        self.assertLessEqual(sum(map(len, files.values())), 48000)

    def test_response_handles_refusal_and_incomplete(self):
        cases = [{'status': 'incomplete'}, {'status': 'completed', 'output': []},
                 {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]},
                 {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'bad'}]}]}]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(AnalysisError):
                parse_response(body)

    def test_output_token_limit_has_actionable_error_even_with_partial_text(self):
        body = {'status': 'incomplete',
                'incomplete_details': {'reason': 'max_output_tokens'},
                'output': [{'type': 'message', 'content': [{'type': 'output_text',
                            'text': json.dumps(self.proposal)}]}]}
        with self.assertRaisesRegex(AnalysisError, '토큰 한도'):
            parse_response(body)

    def test_provider_uses_responses_structured_output(self):
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(self.proposal)}]}]}
        with patch('application.analysis.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = OpenAIAnalyzer(self.settings).propose(self.files)
            request = opener.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
        self.assertFalse(payload['store'])
        self.assertEqual(payload['text']['format']['type'], 'json_schema')
        self.assertTrue(payload['text']['format']['strict'])
        self.assertEqual(result, self.proposal)

    def test_provider_keeps_response_size_limit_error(self):
        with patch('application.analysis.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'x' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(AnalysisError, 'response exceeded the size limit'):
                OpenAIAnalyzer(self.settings).propose(self.files)

    def test_provider_error_does_not_expose_response_body(self):
        error = urllib.error.HTTPError('https://api.openai.com', 401, 'unauthorized', {}, io.BytesIO(b'sensitive'))
        with patch('application.analysis.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(AnalysisError) as raised:
                OpenAIAnalyzer(self.settings).propose(self.files)
            self.assertIn('401', str(raised.exception))
            self.assertNotIn('sensitive', str(raised.exception))

    def test_provider_retries_temporary_rejection_before_analysis(self):
        failure = urllib.error.HTTPError('https://api.openai.com/v1/responses', 503,
            'unavailable', {'Retry-After': '0'},
            io.BytesIO(b'{"error":{"code":"server_is_overloaded"}}'))
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(self.proposal)}]}]}
        with patch('application.analysis.urllib.request.build_opener') as opener, \
                patch('adapters.ai.openai_http.time.sleep') as sleep, \
                patch('adapters.ai.openai_http.random.uniform', return_value=0):
            opener.return_value.open.side_effect = [failure, io.BytesIO(json.dumps(response).encode())]
            result = OpenAIAnalyzer(self.settings).propose(self.files)
        self.assertEqual(result, self.proposal)
        self.assertEqual(opener.return_value.open.call_count, 2)
        sleep.assert_called_once()

    def test_provider_does_not_retry_spend_limit(self):
        failure = urllib.error.HTTPError('https://api.openai.com/v1/responses', 429,
            'limit', {}, io.BytesIO(b'{"error":{"code":"project_spend_limit_exceeded"}}'))
        with patch('application.analysis.urllib.request.build_opener') as opener, \
                patch('adapters.ai.openai_http.time.sleep') as sleep:
            opener.return_value.open.side_effect = failure
            with self.assertRaisesRegex(AnalysisError, 'HTTP 429'):
                OpenAIAnalyzer(self.settings).propose(self.files)
        opener.return_value.open.assert_called_once()
        sleep.assert_not_called()

    def test_changed_source_is_rejected_before_docker(self):
        plan = analyze(self.project)
        (self.project / 'server.js').write_text('changed')
        with patch.object(LocalDockerAdapter, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                LocalDockerAdapter(lambda *_: None).deploy(self.project, plan, 'test')
            command.assert_not_called()

    def test_required_env_blocks_execution(self):
        plan = validate_proposal(self.project, self.files, {**self.proposal, 'required_env': ['DATABASE_URL']}, 'model')
        with patch.object(LocalDockerAdapter, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'DATABASE_URL'):
                LocalDockerAdapter(lambda *_: None).deploy(self.project, plan, 'test')
            command.assert_not_called()

    def test_worker_executes_reviewed_plan_without_reanalysis(self):
        app = App(Path(self.temp.name) / 'state', AISettings())
        plan = validate_proposal(self.project, self.files, self.proposal, 'model')
        (app.root / 'job').mkdir()
        app.jobs['job'] = {'project': str(self.project), 'plan': asdict(plan), 'events': []}
        with patch('interfaces.http.server.analyze_project') as analysis, patch.object(LocalDockerAdapter, 'deploy', return_value={'url': 'http://127.0.0.1:1234'}) as deploy:
            app.run('job')
            analysis.assert_not_called()
            self.assertEqual(deploy.call_args.args[1], plan)
            self.assertEqual(app.jobs['job']['status'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
