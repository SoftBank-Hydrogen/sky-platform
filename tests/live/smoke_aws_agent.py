"""Explicit end-to-end AWS smoke with scripted AI tool calls, not a live AI test."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.support.agent_fixture import call
from sky_platform.planning.analysis import AISettings
from sky_platform.deployment.aws import AwsExpressAdapter, AwsSettings
from sky_platform.api.server import App, handler_for


class AwsRepairFixture:
    """Patch the real sample's missing start script and loopback binding."""
    def __init__(self, _settings):
        self.index = 0

    def next(self, _history):
        actions = [
            ('read_project_files', {'paths': ['package.json', 'server.js']}),
            ('apply_project_patch', {'path': 'package.json', 'old_text': '"scripts": {}',
                                     'new_text': '"scripts": {"start": "node server.js"}'}),
            ('apply_project_patch', {'path': 'server.js', 'old_text': ").listen(4321, '127.0.0.1',",
                                     'new_text': ").listen(Number(process.env.PORT || 4321), '0.0.0.0',"}),
            ('configure_deployment', {'start_script': 'start', 'build_script': None, 'port': 3000,
                                      'health_path': '/', 'required_env': []}),
            ('deploy_application', {}),
        ]
        name, arguments = actions[self.index]
        self.index += 1
        return call(name, arguments, self.index)


class AwsPythonFixture:
    """Use an uploaded Python Dockerfile and repair loopback binding."""
    def __init__(self, _settings):
        self.index = 0

    def next(self, _history):
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


def main():
    parser = argparse.ArgumentParser(description='Upload, repair, and deploy the sample through the real AWS path')
    parser.add_argument('--apply', action='store_true', help='Create and then clean up a real ECS service')
    parser.add_argument('--account', required=True, help='Expected AWS account ID')
    parser.add_argument('--region', required=True)
    parser.add_argument('--python', action='store_true', help='Deploy a Dockerfile-based Python app without package.json')
    args = parser.parse_args()
    settings = AwsSettings(args.region)
    settings.validate()
    identity = json.loads(subprocess.check_output(['aws', 'sts', 'get-caller-identity', '--region', args.region,
                                                    '--output', 'json', '--no-cli-pager'], text=True))
    if identity.get('Account') != args.account:
        raise SystemExit('AWS 계정이 요청한 계정과 다릅니다. 리소스를 생성하지 않았습니다.')
    if not args.apply:
        print('읽기 전용 계정 점검 완료. --apply를 지정해야 AWS 리소스를 생성합니다.')
        return

    job_id = None
    job = None
    with tempfile.TemporaryDirectory(prefix='sky-aws-agent-smoke-') as directory:
        app = App(Path(directory), AISettings('test-fixture', 'fixture-model'),
                  AwsPythonFixture if args.python else AwsRepairFixture,
                  aws_settings=settings)
        class QuietHandler(handler_for(app)):
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f'http://127.0.0.1:{server.server_port}'

        def request(path, data=None, headers=None):
            request_headers = {'X-Sky-Token': app.token, **(headers or {})}
            with opener.open(urllib.request.Request(base + path, data=data, headers=request_headers), timeout=15) as response:
                return json.load(response)

        try:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as bundle:
                if args.python:
                    bundle.writestr('Dockerfile', 'FROM python:3.13-alpine\nWORKDIR /app\nCOPY server.py ./\nCMD ["python", "server.py"]\n')
                    bundle.writestr('server.py', "from http.server import BaseHTTPRequestHandler, HTTPServer\nimport os\nclass Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200)\n        self.send_header('Content-Type', 'application/json')\n        self.end_headers()\n        self.wfile.write(b'{\"message\":\"Python on AWS\"}')\nHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()\n")
                else:
                    for file in (Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apps' / 'unready-node').iterdir():
                        bundle.write(file, file.name)
            job_id = request('/api/deployments', archive.getvalue(), {
                'X-Deploy-Target': 'aws-ecs-express', 'X-Public-Access': 'true',
                'X-Application-Id': 'aws-python-smoke' if args.python else 'aws-agent-smoke'})['id']
            print('Started AWS agent job:', job_id, flush=True)
            for _ in range(600):
                job = request('/api/jobs/' + job_id)
                if job['status'] != 'running':
                    break
                if _ % 15 == 0:
                    print('Status:', job['status'], 'stage:', job['events'][-1]['stage'] if job['events'] else '-', flush=True)
                time.sleep(2)
            else:
                raise RuntimeError('Agent job exceeded the smoke time limit')
            if job['status'] != 'succeeded':
                raise AssertionError(json.dumps(job, ensure_ascii=False, indent=2))
            assert job['attempts'] == 1
            assert job['target'] == 'aws-ecs-express'
            assert job['result']['service'] == 'sky-' + job_id + '-a1'
            if args.python:
                assert {change['path'] for change in job['changes']} == {'server.py'}
                assert not (Path(job['project']) / 'package.json').exists()
                assert "'127.0.0.1'" in (Path(job['project']) / 'server.py').read_text()
                assert job['plan']['runtime'] == 'custom-dockerfile'
            else:
                assert {change['path'] for change in job['changes']} == {'package.json', 'server.js'}
                assert json.loads((Path(job['project']) / 'package.json').read_text())['scripts'] == {}
                assert "'127.0.0.1'" in (Path(job['project']) / 'server.js').read_text()
            health = request('/api/jobs/' + job_id + '/health')
            assert health['healthy'], health
            print('PASS: HTTP upload -> scripted source edits -> actual AWS deploy -> public HTTP 200 -> health API')
            print('AI mode: SCRIPTED TEST FIXTURE (not live AI)')
            print(json.dumps(job['result'], ensure_ascii=False, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            if job_id:
                attempt = job_id + '-a1'
                adapter = AwsExpressAdapter(lambda stage, message: print(f'[{stage}] {message}', flush=True), settings)
                if job and job.get('status') == 'succeeded' and job.get('result'):
                    adapter.retire(job['result'], attempt)
                    print('Smoke ECS service and image deleted; shared stack and demo service retained.', flush=True)
                else:
                    adapter.service_arn = f'arn:aws:ecs:{args.region}:{args.account}:service/default/sky-{attempt}'
                    adapter.service_created = True
                    adapter.image = f'{args.account}.dkr.ecr.{args.region}.amazonaws.com/sky-managed:{attempt}'
                    adapter.image_pushed = True
                    adapter.image_built = True
                    adapter.cleanup_failure(attempt)
                    print('Smoke failure cleanup requested; inspect ECS and ECR before concluding cleanup.', flush=True)


if __name__ == '__main__':
    main()
