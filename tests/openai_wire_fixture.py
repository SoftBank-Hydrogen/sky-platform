"""Fake Responses transport for a real local deployment; never a product model."""
from __future__ import annotations

import io
import json
import threading
import urllib.request

from sky_platform.runtime.agent import COMPACT_AGENT_REQUEST_BYTES


class ResponsesWireFixture:
    def __init__(self, actions=None, *, expected_target='local-docker', planner_requests=1,
                 managed_postgres=False, expected_model='wire-fixture-model', compact_after_first=False,
                 python_generated=False, asgi_generated=False, wsgi_generated=False):
        self.local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.lock = threading.Lock()
        self.planner_requests = 0
        self.agent_requests = 0
        self.compact_requests = 0
        self.compact_after_first = compact_after_first
        self.expected_target = expected_target
        self.expected_planner_requests = planner_requests
        self.managed_postgres = managed_postgres
        self.expected_model = expected_model
        self.python_generated = python_generated
        self.asgi_generated = asgi_generated
        self.wsgi_generated = wsgi_generated
        default_actions = ([
            ('read_project_files', {'paths': ['app.py', 'requirements.txt']}),
            ('configure_deployment', {'start_script': 'wsgi:app.py', 'build_script': None,
                                      'port': 4321, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ] if wsgi_generated else [
            ('read_project_files', {'paths': ['main.py', 'requirements.txt']}),
            ('configure_deployment', {'start_script': 'asgi:main.py', 'build_script': None,
                                      'port': 4321, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ] if asgi_generated else [
            ('read_project_files', {'paths': ['server.py']}),
            ('apply_project_patch', {'path': 'server.py',
                'old_text': 'HTTPServer(("127.0.0.1", 4321), Handler)',
                'new_text': 'HTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler)'}),
            ('configure_deployment', {'start_script': 'server.py', 'build_script': None,
                                      'port': 4321, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ] if python_generated else [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', {'path': 'package.json', 'old_text': '"scripts": {}',
                                     'new_text': '"scripts": {"start": "node server.js"}'}),
            ('apply_project_patch', {'path': 'server.js',
                                     'old_text': ").listen(4321, '127.0.0.1',",
                                     'new_text': ").listen(Number(process.env.PORT || 4321), '0.0.0.0',"}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None,
                                      'port': 4321, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ])
        self.actions = actions if actions is not None else default_actions

    def open(self, request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url.startswith('http://127.0.0.1:'):
            return self.local_opener.open(request, timeout=timeout)
        if url == 'https://api.openai.com/v1/responses/compact':
            assert self.compact_after_first and self.agent_requests == 1 and self.compact_requests == 0
            assert request.get_header('Authorization') == 'Bearer wire-fixture-key'
            payload = json.loads(request.data)
            assert payload['model'] == self.expected_model
            history = payload['input']
            assert history[-3]['type'] == 'reasoning'
            assert len(history[-3]['encrypted_content']) > COMPACT_AGENT_REQUEST_BYTES
            assert history[-2]['type'] == 'function_call'
            assert history[-1]['type'] == 'function_call_output'
            self.compact_requests += 1
            compacted = [
                {'type': 'compaction', 'id': 'cmp_1', 'encrypted_content': 'opaque-compact'},
                {**history[-3], 'encrypted_content': 'opaque-1'}, history[-2], history[-1],
            ]
            return io.BytesIO(json.dumps({'object': 'response.compaction', 'output': compacted}).encode())
        if url != 'https://api.openai.com/v1/responses':
            raise AssertionError('Unexpected network request: ' + url)
        assert request.get_header('Authorization') == 'Bearer wire-fixture-key'
        payload = json.loads(request.data)
        assert payload['model'] == self.expected_model and payload['store'] is False
        with self.lock:
            body = self._planner_response(payload) if 'text' in payload else self._agent_response(payload)
        return io.BytesIO(json.dumps(body).encode())

    def _planner_response(self, payload):
        assert self.planner_requests == 0 and self.expected_planner_requests == 1
        assert payload['text']['format']['type'] == 'json_schema'
        assert payload['text']['format']['strict'] is True
        context = json.loads(payload['input'])
        assert 'local-docker' in context['available_targets']
        if self.wsgi_generated:
            assert 'Flask' in context['files']['app.py']
            evidence = {'file': 'app.py', 'quote': 'Flask'}
        elif self.asgi_generated:
            assert 'FastAPI' in context['files']['main.py']
            evidence = {'file': 'main.py', 'quote': 'FastAPI'}
        elif self.python_generated:
            assert 'HTTPServer' in context['files']['server.py']
            evidence = {'file': 'server.py', 'quote': 'HTTPServer'}
        else:
            assert '"scripts": {}' in context['files']['package.json']
            evidence = {'file': 'package.json', 'quote': '"scripts": {}'}
        self.planner_requests += 1
        proposal = {'target': 'local-docker', 'workload': 'stateless-http',
                    'rationale': '로컬 Docker 배포 경로를 검증합니다.',
                    'evidence': [evidence]}
        return {'status': 'completed', 'output': [{'type': 'message', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': json.dumps(proposal)}]}]}

    def _agent_response(self, payload):
        index = self.agent_requests
        assert index < len(self.actions)
        assert payload['tool_choice'] == 'required' and payload['parallel_tool_calls'] is False
        assert payload['include'] == ['reasoning.encrypted_content']
        assert all(tool['strict'] is True for tool in payload['tools'])
        history = payload['input']
        if self.compact_after_first and index > 0:
            assert history[0]['type'] == 'compaction'
        if index == 0:
            assert len(history) == 1 and history[0]['role'] == 'user'
            initial = json.loads(history[0]['content'])
            assert initial['target'] == self.expected_target
            assert initial['managed_postgres_connection'] is self.managed_postgres
        else:
            reasoning, function_call, tool_result = history[-3:]
            assert reasoning['type'] == 'reasoning'
            assert reasoning['encrypted_content'] == f'opaque-{index}'
            assert function_call['type'] == 'function_call'
            assert function_call['call_id'] == f'call_{index}'
            assert tool_result['type'] == 'function_call_output'
            assert tool_result['call_id'] == function_call['call_id']
            result = json.loads(tool_result['output'])
            assert 'error' not in result, result
            previous_name, previous_args = self.actions[index - 1]
            if previous_name == 'read_project_files':
                assert set(previous_args['paths']) <= set(result['files'])
            if previous_name == 'apply_project_patch':
                assert result['changed'] == previous_args['path']
            if previous_name == 'configure_deployment':
                assert result['ready'] is True
        name, arguments = self.actions[index]
        self.agent_requests += 1
        reasoning = f'opaque-{index + 1}'
        if self.compact_after_first and index == 0:
            reasoning += 'x' * COMPACT_AGENT_REQUEST_BYTES
        return {'status': 'completed', 'output': [
            {'type': 'reasoning', 'id': f'rs_{index + 1}',
             'encrypted_content': reasoning, 'summary': []},
            {'type': 'function_call', 'id': f'fc_{index + 1}',
             'call_id': f'call_{index + 1}', 'name': name,
             'arguments': json.dumps(arguments), 'status': 'completed'},
        ]}

    def assert_complete(self):
        assert self.planner_requests == self.expected_planner_requests, self.planner_requests
        assert self.agent_requests == len(self.actions), self.agent_requests
        assert self.compact_requests == int(self.compact_after_first), self.compact_requests
