"""Real upload -> analyze -> deploy -> HTTP smoke; needs Docker."""
import io
import argparse
import json
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext

from sky_platform.runtime.analysis import AISettings, OpenAIAnalyzer
from sky_platform.runtime.server import App, handler_for


def smoke(mode):
    with tempfile.TemporaryDirectory(prefix="sky-smoke-") as temporary:
        settings = AISettings.from_environment() if mode == 'live-ai' else AISettings()
        if mode == 'fixture-ai':
            settings = AISettings('fixture-not-a-real-key', 'fixture-model')
        if mode == 'live-ai' and not settings.available:
            raise RuntimeError('Set OPENAI_API_KEY for live AI validation')
        app = App(Path(temporary), settings)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path, data=None):
            req = urllib.request.Request(base + path, data=data,
                headers={"X-Sky-Token": app.token,
                         "X-Analysis-Mode": 'static' if mode == 'static' else 'ai'})
            with opener.open(req, timeout=90) as response:
                return json.load(response)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            for path in Path("tests/fixtures/apps/hello-node" if mode == 'static' else 'tests/fixtures/apps/serve-node').iterdir():
                z.write(path, path.name)
        job_id = None
        try:
            with opener.open(base) as response:
                assert b"Sky" in response.read()
            assert request('/api/config')['ai_available'] == settings.available
            job = request("/api/analyze", archive.getvalue())
            job_id = job['id']
            assert job['plan']['analyzer'] == ('static' if mode == 'static' else 'openai')
            assert '+++ Dockerfile' in job['diff']
            if mode != 'static':
                assert job['plan']['start_command'] == 'npm run serve'
                assert job['plan']['port'] == 8087
                assert job['plan']['health_path'] == '/health'
            runtime_value = 'synthetic-smoke-environment-value'
            if mode != 'static':
                assert 'DEMO_TOKEN' in job['plan']['required_env']
                try:
                    request('/api/deploy/' + job_id, b'{}')
                    raise AssertionError('Missing required env was accepted')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 400
                    exc.close()
            request("/api/deploy/" + job_id, json.dumps({'environment': {'DEMO_TOKEN': runtime_value}}).encode())
            for _ in range(360):
                job = request("/api/jobs/" + job_id)
                if job['status'] != 'running':
                    break
                time.sleep(1)
            if job['status'] != 'succeeded':
                raise AssertionError(json.dumps(job, indent=2))
            with opener.open(job['result']['health_url'] if mode != 'static' else job['result']['url']) as response:
                assert json.load(response)['message'] == ('Hello from Sky!' if mode == 'static' else 'AI plan deployed!')
            assert runtime_value not in (app.root / job_id / 'job.json').read_text()
            assert runtime_value not in json.dumps(request('/api/jobs/' + job_id))
            assert request('/api/jobs')[0]['id'] == job_id
            # Replace the server and App instance, using only persisted job records.
            server.shutdown()
            server.server_close()
            app = App(Path(temporary), settings)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}'
            recovered = request('/api/jobs/' + job_id)
            assert recovered['status'] == 'succeeded'
            assert recovered['result'] == job['result']
            assert request('/api/jobs')[0]['id'] == job_id
            print(f"PASS ({mode}): ZIP upload -> plan -> Docker build -> deploy -> HTTP response", flush=True)
            print(json.dumps(job['result']), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            if job_id:
                subprocess.run(['docker', 'rm', '-f', 'sky-' + job_id], capture_output=True)
                subprocess.run(['docker', 'image', 'rm', 'sky/' + job_id + ':latest'], capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['static', 'fixture-ai', 'live-ai'], default='static')
    mode = parser.parse_args().mode
    proposal = {
        'framework': 'node:http', 'start_script': 'serve', 'build_script': None,
        'port': 8087, 'health_path': '/health', 'required_env': ['DEMO_TOKEN'],
        'rationale': 'Deterministic test proposal, not a live AI result.', 'warnings': [],
        'evidence': [{'file': 'server.js', 'quote': ").listen(8087, '0.0.0.0');"}],
    }
    context = patch.object(OpenAIAnalyzer, 'propose', return_value=proposal) if mode == 'fixture-ai' else nullcontext()
    with context:
        smoke(mode)


if __name__ == '__main__':
    main()
