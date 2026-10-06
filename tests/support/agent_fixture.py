"""Deterministic tool calls for tests only, never a product AI replacement."""
import json


def call(name, arguments, number=0):
    return [{"type": "function_call", "call_id": f"fixture_{number}",
             "name": name, "arguments": json.dumps(arguments)}]


class RepairFixture:
    def __init__(self, settings=None):
        self.index = 0

    def next(self, history):
        script_patch = {'path': 'package.json', 'old_text': '"scripts": {}',
                        'new_text': '"scripts": {"start": "node server.js"}'}
        binding_patch = {'path': 'server.js', "old_text": ").listen(4321, '127.0.0.1',",
                         "new_text": ").listen(Number(process.env.PORT || 4321), '0.0.0.0',"}
        config = {'start_script': 'start', 'build_script': None, 'port': 4321,
                  'health_path': '/', 'required_env': []}
        actions = [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', script_patch),
            ('configure_deployment', config),
            ('deploy_application', {}),
            ('read_runtime_logs', {}),
            ('apply_project_patch', binding_patch),
            ('configure_deployment', {**config, 'port': 3000}),
            ('deploy_application', {}),
        ]
        if self.index == 4:
            result = json.loads(history[-1]['output'])
            assert result.get('verified') is False, 'Fixture requires a real failed deployment first'
        name, arguments = actions[self.index]
        self.index += 1
        return call(name, arguments, self.index)


class EnvironmentFixture(RepairFixture):
    def next(self, history):
        initial = json.loads(history[0]['content'])
        if 'DEMO_TOKEN' not in initial['available_environment_names']:
            return call('request_environment', {'names': ['DEMO_TOKEN'], 'reason': '앱 실행에 DEMO_TOKEN이 필요합니다.'})
        # Only names, never runtime values, may reach the provider.
        assert 'synthetic-agent-runtime-value' not in json.dumps(history)
        return super().next(history)


class PauseAfterRepairFixture:
    """Repair before requesting a secret, then re-read the preserved work on resume."""
    def __init__(self, settings=None):
        self.index = 0

    def next(self, history):
        initial = json.loads(history[0]['content'])
        resumed = 'DEMO_TOKEN' in initial['available_environment_names']
        assert 'synthetic-agent-runtime-value' not in json.dumps(history)
        config = {'start_script': 'start', 'build_script': None, 'port': 4321,
                  'health_path': '/', 'required_env': ['DEMO_TOKEN']}
        actions = ([
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', {'path': 'package.json', 'old_text': '"scripts": {}',
                                     'new_text': '"scripts": {"start": "node server.js"}'}),
            ('apply_project_patch', {'path': 'server.js',
                                     'old_text': ").listen(4321, '127.0.0.1',",
                                     'new_text': ").listen(Number(process.env.PORT || 4321), '0.0.0.0',"}),
            ('configure_deployment', config),
            ('deploy_application', {}),
        ] if not resumed else [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('configure_deployment', config),
            ('deploy_application', {}),
        ])
        name, arguments = actions[self.index]
        self.index += 1
        return call(name, arguments, self.index)


class PythonDockerfileFixture:
    def __init__(self, settings=None):
        self.index = 0

    def next(self, history):
        actions = [
            ('read_project_files', {'paths': ['Dockerfile', 'server.py']}),
            ('apply_project_patch', {'path': 'server.py', 'old_text': "'127.0.0.1'",
                                     'new_text': "'0.0.0.0'"}),
            ('configure_deployment', {'start_script': 'dockerfile', 'build_script': None,
                                      'port': 3000, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ]
        name, arguments = actions[self.index]
        self.index += 1
        return call(name, arguments, self.index)


class PythonGeneratedFixture:
    """Repair a Dockerfile-less executable Python HTTP app."""
    def __init__(self, settings=None):
        self.index = 0

    def next(self, history):
        actions = [
            ('read_project_files', {'paths': ['server.py']}),
            ('apply_project_patch', {'path': 'server.py',
                'old_text': 'HTTPServer(("127.0.0.1", 4321), Handler)',
                'new_text': 'HTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler)'}),
            ('configure_deployment', {'start_script': 'server.py', 'build_script': None,
                                      'port': 4321, 'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ]
        name, arguments = actions[self.index]
        self.index += 1
        return call(name, arguments, self.index)
