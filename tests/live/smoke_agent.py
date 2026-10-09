"""Real agent-tool HTTP/Docker smoke. Default AI decisions are a TEST FIXTURE."""
import argparse
import io
import json
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.support.agent_fixture import EnvironmentFixture, PauseAfterRepairFixture, PythonDockerfileFixture, PythonGeneratedFixture, RepairFixture
from tests.support.openai_wire_fixture import ResponsesWireFixture
from application.agent import OpenAIDeployAgent
from application.analysis import AISettings
from application.deployment_core import source_digest
from application.infrastructure import OpenAIInfrastructurePlanner
from interfaces.http.server import App, handler_for


class InfrastructureFixture:
    """Deterministic target choice for a real Docker integration test."""
    def __init__(self, _settings):
        pass

    def propose(self, files, available_targets, public_access):
        assert 'local-docker' in available_targets
        name, content = next(iter(files.items()))
        return {'target': 'local-docker', 'workload': 'stateless-http',
                'rationale': '로컬 실행 경로 검증',
                'evidence': [{'file': name, 'quote': content[:20]}]}


def verify_node_repair(job):
    """A successful HTTP response alone does not prove the model repaired the upload."""
    original = Path(job['project'])
    work = original.parent / 'work'
    changed = {item.get('path') for item in job.get('changes', [])
               if isinstance(item.get('diff'), str) and item['diff'].strip()}
    if not {'package.json', 'server.js'} <= changed:
        raise AssertionError('시작 스크립트와 서버 바인딩의 AI 수정 기록이 모두 필요합니다.')
    if (json.loads((original / 'package.json').read_text())['scripts'] != {}
            or "'127.0.0.1'" not in (original / 'server.js').read_text()
            or (original / 'Dockerfile').exists()):
        raise AssertionError('업로드 원본이 변경됐습니다.')
    scripts = json.loads((work / 'package.json').read_text()).get('scripts', {})
    server = (work / 'server.js').read_text()
    if (not isinstance(scripts.get('start'), str) or not scripts['start'].strip()
            or 'process.env.PORT' not in server or re.search(r'''["']127\.0\.0\.1["']''', server)
            or not re.search(r'''["']0\.0\.0\.0["']''', server)):
        raise AssertionError('작업용 복사본에 시작·PORT·바인딩 수정이 없습니다.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Use actual AI API (requires OPENAI_API_KEY)')
    parser.add_argument('--environment', action='store_true', help='Exercise missing environment and resume')
    parser.add_argument('--restart-before-resume', action='store_true',
                        help='Repair, pause for environment, restart server, then deploy')
    parser.add_argument('--python', action='store_true', help='Use a scripted fixture for a Python Dockerfile app')
    parser.add_argument('--python-generated', action='store_true',
                        help='Repair and deploy a Python app without an uploaded Dockerfile')
    parser.add_argument('--asgi-generated', action='store_true',
                        help='With --wire-fixture, deploy a root FastAPI ASGI app without a Dockerfile')
    parser.add_argument('--wsgi-generated', action='store_true',
                        help='With --wire-fixture, deploy a root Flask WSGI app without a Dockerfile')
    parser.add_argument('--folder', action='store_true', help='Upload a browser-style folder instead of a ZIP')
    parser.add_argument('--auto', action='store_true', help='Exercise infrastructure target planning')
    parser.add_argument('--interrupted-retire', action='store_true',
                        help='Restart a completed scripted local job as interrupted, then retire its owned Docker attempts')
    parser.add_argument('--wire-fixture', action='store_true',
                        help='Use mocked Responses HTTP with the real OpenAI planner and agent code')
    parser.add_argument('--compact-fixture', action='store_true',
                        help='With --wire-fixture, compact a long stateless agent history before Docker deployment')
    parser.add_argument('--resume-unstarted', action='store_true',
                        help='Interrupt worker startup, restart the server, then resume the same upload')
    args = parser.parse_args()
    if args.python and (args.live or args.environment or args.python_generated or args.asgi_generated or args.wsgi_generated):
        parser.error('--python cannot be combined with --live or --environment')
    if args.python_generated and (args.live or args.environment or args.folder or args.auto
                                  or args.interrupted_retire or args.resume_unstarted or args.asgi_generated
                                  or args.wsgi_generated):
        parser.error('--python-generated uses the scripted local Python deployment only')
    if args.asgi_generated and (not args.wire_fixture or args.compact_fixture or args.resume_unstarted
                                or args.wsgi_generated):
        parser.error('--asgi-generated requires --wire-fixture without resume or compaction')
    if args.wsgi_generated and (not args.wire_fixture or args.compact_fixture or args.resume_unstarted):
        parser.error('--wsgi-generated requires --wire-fixture without resume or compaction')
    if args.restart_before_resume and (not args.environment or args.live or args.python or args.folder
                                       or args.auto or args.interrupted_retire or args.wire_fixture):
        parser.error('--restart-before-resume requires --environment with the scripted Node fixture')
    if args.live and args.auto:
        parser.error('--live --auto may select a billable cloud target; use a dedicated cloud drill')
    if args.interrupted_retire and (args.live or args.environment or args.python or args.folder or args.auto):
        parser.error('--interrupted-retire uses the default scripted Node deployment only')
    if args.wire_fixture and (args.live or args.environment or args.python or args.folder
                              or args.auto or args.interrupted_retire):
        parser.error('--wire-fixture uses a Node or generated Python app with local automatic target only')
    if args.resume_unstarted and not args.wire_fixture:
        parser.error('--resume-unstarted requires --wire-fixture')
    if args.compact_fixture and (not args.wire_fixture or args.resume_unstarted):
        parser.error('--compact-fixture requires --wire-fixture without --resume-unstarted')
    settings = (AISettings.from_environment() if args.live else
                AISettings('wire-fixture-key', 'wire-fixture-model') if args.wire_fixture else
                AISettings('test-fixture', 'fixture-model'))
    if args.live and not settings.available:
        raise RuntimeError('Set OPENAI_API_KEY for live testing')
    wire = ResponsesWireFixture(compact_after_first=args.compact_fixture,
                               python_generated=args.python_generated,
                               asgi_generated=args.asgi_generated,
                               wsgi_generated=args.wsgi_generated) if args.wire_fixture else None
    with tempfile.TemporaryDirectory(prefix='sky-agent-smoke-') as directory, \
            (patch('urllib.request.build_opener', return_value=wire) if wire else nullcontext()):
        app = App(Path(directory), settings, OpenAIDeployAgent if args.live or wire else
                  (PythonGeneratedFixture if args.python_generated else
                   PythonDockerfileFixture if args.python else
                   PauseAfterRepairFixture if args.restart_before_resume else
                   EnvironmentFixture if args.environment else RepairFixture),
                  infrastructure_planner_factory=InfrastructureFixture if args.auto and not args.live else OpenAIInfrastructurePlanner)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        job_id = None
        def request(path, data=None, content_type=None):
            headers = {'X-Sky-Token': app.token}
            if content_type:
                headers['Content-Type'] = content_type
            if path == '/api/deployments' and (args.auto or wire):
                headers['X-Deploy-Target'] = 'auto'
            req = urllib.request.Request(base + path, data=data, headers=headers)
            with opener.open(req, timeout=15) as response:
                return json.load(response)
        try:
            files = []
            if args.wsgi_generated:
                for file in Path('tests/fixtures/apps/flask-wsgi').iterdir():
                    files.append((file.name, file.read_bytes()))
            elif args.asgi_generated:
                for file in Path('tests/fixtures/apps/fastapi-asgi').iterdir():
                    files.append((file.name, file.read_bytes()))
            elif args.python_generated:
                files.append(('server.py', Path('tests/fixtures/apps/unready-python/server.py').read_bytes()))
            elif args.python:
                files.append(('Dockerfile', b'FROM python:3.13-alpine\nWORKDIR /app\nCOPY server.py ./\nCMD ["python", "server.py"]\n'))
                files.append(('server.py', b"from http.server import BaseHTTPRequestHandler, HTTPServer\nimport os\nclass Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200)\n        self.send_header('Content-Type', 'application/json')\n        self.end_headers()\n        self.wfile.write(b'{\"message\":\"Python app is running\"}')\nHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()\n"))
            else:
                for file in Path('tests/fixtures/apps/unready-node').iterdir():
                    data = file.read_bytes()
                    if args.environment and file.name == 'server.js':
                        data = b"if (!process.env.DEMO_TOKEN) throw Error('DEMO_TOKEN is required');\n" + data
                    files.append((file.name, data))
            if args.folder:
                boundary = 'sky-smoke-boundary'
                body = bytearray()
                for name, data in files:
                    for field, value in [('path', ('sample-app/' + name).encode()), ('file', data)]:
                        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"'.encode())
                        if field == 'file':
                            body.extend(f'; filename="{name}"'.encode())
                        body.extend(b'\r\n\r\n' + value + b'\r\n')
                body.extend(f'--{boundary}--\r\n'.encode())
                upload, content_type = bytes(body), 'multipart/form-data; boundary=' + boundary
            else:
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, 'w') as bundle:
                    for name, data in files:
                        bundle.writestr(name, data)
                upload, content_type = archive.getvalue(), None
            # One upload request starts editing and deployment; there is no analyze/approve step.
            if args.resume_unstarted:
                start_worker = app.start_job_worker
                def fail_worker_start(job_id, worker, environment=None):
                    with patch('interfaces.http.server.threading.Thread.start',
                               side_effect=RuntimeError('simulated worker start failure')):
                        return start_worker(job_id, worker, environment)
                app.start_job_worker = fail_worker_start
            deployment_started = time.monotonic()
            uploaded = request('/api/deployments', upload, content_type)
            job_id = uploaded['id']
            if args.resume_unstarted:
                assert uploaded['status'] == 'interrupted'
                assert request('/api/jobs/' + job_id)['attempts'] == 0
                server.shutdown()
                server.server_close()
                app = App(Path(directory), settings, OpenAIDeployAgent)
                assert app.jobs[job_id]['status'] == 'interrupted'
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f'http://127.0.0.1:{server.server_port}'
                resumed = request('/api/deployments/' + job_id + '/resume-unstarted', b'')
                assert resumed['status'] == 'running'
            for _ in range(950):
                job = request('/api/jobs/' + job_id)
                if job['status'] == 'waiting_input' and args.environment:
                    assert job['missing_environment'] == ['DEMO_TOKEN']
                    if args.restart_before_resume:
                        verify_node_repair(job)
                        work = app.root / job_id / 'work'
                        assert job['work_digest'] == source_digest(work)
                        server.shutdown()
                        server.server_close()
                        app = App(Path(directory), settings, PauseAfterRepairFixture)
                        assert app.jobs[job_id]['status'] == 'waiting_input'
                        assert app.jobs[job_id]['work_digest'] == source_digest(work)
                        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
                        threading.Thread(target=server.serve_forever, daemon=True).start()
                        base = f'http://127.0.0.1:{server.server_port}'
                    request('/api/deployments/' + job_id + '/resume', json.dumps({
                        'environment': {'DEMO_TOKEN': 'synthetic-agent-runtime-value'}}).encode())
                    time.sleep(1)
                    continue
                if job['status'] != 'running':
                    break
                time.sleep(1)
            if job['status'] != 'succeeded':
                raise AssertionError(json.dumps(job, indent=2, ensure_ascii=False))
            if args.auto or wire:
                assert job['target'] == 'local-docker'
                assert job['infrastructure_plan']['planner'] == 'openai'
            with opener.open(job['result']['url'], timeout=5) as response:
                expected_message = ('Flask application is running' if args.wsgi_generated else
                                    'FastAPI application is running' if args.asgi_generated else
                                    'Original Python application is running' if args.python_generated else
                                    'Python app is running' if args.python else 'Original application is running')
                assert json.load(response)['message'] == expected_message
            original = Path(job['project'])
            if args.wsgi_generated:
                assert job['plan']['runtime'] == 'python-wsgi'
                assert job['plan']['start_command'].startswith('python -m gunicorn --bind 0.0.0.0:4321')
                assert not (original / 'Dockerfile').exists()
                assert (original / 'app.py').is_file()
                assert job['attempts'] == 1
            elif args.asgi_generated:
                assert job['plan']['runtime'] == 'python-asgi'
                assert job['plan']['start_command'].startswith('python -m uvicorn main:app')
                assert not (original / 'Dockerfile').exists()
                assert (original / 'main.py').is_file()
                assert job['attempts'] == 1
            elif args.python_generated:
                assert job['plan']['runtime'] == 'python'
                assert not (original / 'Dockerfile').exists()
                assert '127.0.0.1' in (original / 'server.py').read_text()
                assert '0.0.0.0' in (app.root / job_id / 'work' / 'server.py').read_text()
                assert any(change['path'] == 'server.py' for change in job['changes'])
            elif args.python:
                assert not (original / 'package.json').exists()
                assert "'127.0.0.1'" in (original / 'server.py').read_text()
                assert (original / 'Dockerfile').exists()
                assert job['plan']['runtime'] == 'custom-dockerfile'
            else:
                verify_node_repair(job)
            assert 'synthetic-agent-runtime-value' not in json.dumps(job)
            if not args.live and not args.python and not wire:
                assert job['attempts'] == (1 if args.restart_before_resume or args.python_generated else 2)
                if not args.restart_before_resume and not args.python_generated:
                    assert any(e['stage'] == 'attempt_failed' for e in job['events'])
                    containers = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True)
                    assert f'sky-{job_id}-a1' not in containers.splitlines()
            print('PASS: one request -> source edits -> real Docker -> HTTP URL', flush=True)
            print(f'DEPLOY_TOTAL_SECONDS {time.monotonic() - deployment_started:.1f}', flush=True)
            print('AI mode: ' + ('LIVE' if args.live else 'RESPONSES WIRE FIXTURE (not live AI)'
                                  if wire else 'SCRIPTED TEST FIXTURE (not live AI)'), flush=True)
            if wire:
                wire.assert_complete()
                print('PASS: Responses planner and agent requests replayed tool and reasoning items', flush=True)
                if args.compact_fixture:
                    print('PASS: compacted agent history continued through one Docker deployment', flush=True)
            if args.restart_before_resume:
                print('PASS: repaired source survived server restart and environment resume', flush=True)
            if args.resume_unstarted:
                print('PASS: unstarted upload resumed after worker failure and server restart', flush=True)
            print('Attempts: ' + str(job['attempts']), flush=True)
            print(json.dumps(job['result']), flush=True)
            if args.python:
                app.monitor_once()
                observed = request('/api/jobs/' + job_id)
                assert observed['last_health']['healthy']
                assert observed['last_health']['source'] == 'automatic'
                assert (app.root / job_id / 'health.json').is_file()
                print('PASS: automatic health check recorded Docker ownership and HTTP 200', flush=True)
            if args.interrupted_retire:
                # Reproduce the crash window after Docker succeeded but before its result was durable.
                with app.lock:
                    app.jobs[job_id]['status'] = 'running'
                    app.jobs[job_id].pop('result')
                    app.save(job_id)
                server.shutdown()
                server.server_close()
                app = App(Path(directory), settings, RepairFixture)
                assert app.jobs[job_id]['status'] == 'interrupted'
                assert app.jobs[job_id]['attempts'] == job['attempts']
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f'http://127.0.0.1:{server.server_port}'
                assert request('/api/jobs/' + job_id)['status'] == 'interrupted'
            request('/api/jobs/' + job_id + '/retire', b'')
            for _ in range(30):
                retired = request('/api/jobs/' + job_id)
                if retired['deployment_state'] != 'deleting':
                    break
                time.sleep(1)
            assert retired['deployment_state'] == 'deleted', json.dumps(retired, ensure_ascii=False)
            assert subprocess.run(['docker', 'container', 'inspect', job['result']['container']],
                                  capture_output=True).returncode != 0
            assert subprocess.run(['docker', 'image', 'inspect', job['result']['image']],
                                  capture_output=True).returncode != 0
            if args.interrupted_retire:
                for number in range(1, job['attempts'] + 1):
                    attempt = f'{job_id}-a{number}'
                    assert subprocess.run(['docker', 'container', 'inspect', f'sky-{attempt}'],
                                          capture_output=True).returncode != 0
                    assert subprocess.run(['docker', 'image', 'inspect', f'sky/{attempt}:latest'],
                                          capture_output=True).returncode != 0
                print('PASS: restart -> interrupted -> retirement API removed every owned attempt', flush=True)
            else:
                print('PASS: retirement API removed the owned container and image tag', flush=True)
        finally:
            server.shutdown()
            server.server_close()
            if job_id:
                for attempt in range(1, 4):
                    subprocess.run(['docker', 'rm', '-f', f'sky-{job_id}-a{attempt}'], capture_output=True)
                    subprocess.run(['docker', 'image', 'rm', f'sky/{job_id}-a{attempt}:latest'], capture_output=True)


if __name__ == '__main__':
    main()
