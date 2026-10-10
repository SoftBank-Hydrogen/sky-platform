"""Real Chrome presentation drill: ZIP -> live AI repair -> Local Docker -> HTTP."""
from __future__ import annotations

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

from application.agent import OpenAIDeployAgent
from application.analysis import AISettings
from interfaces.cli import load_local_env
from interfaces.http.server import App, handler_for


CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_demo_cdp.mjs')
SAMPLE = Path(__file__).resolve().parents[1] / 'fixtures' / 'apps' / 'unready-node'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path, help='Write a local screenshot for visual review')
    args = parser.parse_args()
    load_local_env(Path.cwd() / '.env')
    settings = AISettings.from_environment()
    if not settings.available:
        parser.error('OPENAI_API_KEY is required')
    if not CHROME.is_file():
        parser.error('Google Chrome is required')
    with tempfile.TemporaryDirectory(prefix='sky-browser-demo-') as directory:
        root = Path(directory)
        archive = root / 'unready-node.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for path in SAMPLE.iterdir():
                if path.is_file():
                    bundle.write(path, path.name)
        app = App(root / 'state', settings, OpenAIDeployAgent, monitor_interval=0)

        class QuietHandler(handler_for(app)):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = root / 'chrome-profile'
        with (root / 'chrome.log').open('w') as log:
            chrome = subprocess.Popen([
                str(CHROME), '--headless=new', '--no-first-run', '--no-default-browser-check',
                '--disable-gpu', '--disable-background-networking', '--no-proxy-server',
                '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--remote-allow-origins=*', '--user-data-dir=' + str(profile), 'about:blank',
            ], stdout=log, stderr=subprocess.STDOUT)
            job_id = None
            try:
                port_file = profile / 'DevToolsActivePort'
                deadline = time.monotonic() + 30
                while not port_file.is_file():
                    if chrome.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Chrome debugging endpoint did not start: ' + str(root / 'chrome.log'))
                    time.sleep(.2)
                command = ['node', str(DRIVER), f'http://127.0.0.1:{server.server_port}/',
                           port_file.read_text().splitlines()[0], str(archive)]
                if args.screenshot:
                    command.append(str(args.screenshot.resolve()))
                result = subprocess.run(command, text=True, capture_output=True, timeout=240)
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                browser = json.loads(result.stdout.strip())
                job_id = browser['job_id']
                job = app.jobs[job_id]
                if job['status'] != 'succeeded' or job['target'] != 'local-docker' or not job['changes']:
                    raise AssertionError('Browser result does not match the actual deployment job')
                original = Path(job['project'])
                if (json.loads((original / 'package.json').read_text())['scripts'] != {}
                        or "'127.0.0.1'" not in (original / 'server.js').read_text()
                        or (original / 'Dockerfile').exists()):
                    raise AssertionError('The uploaded source was modified')
                print(json.dumps(browser, ensure_ascii=False), flush=True)
            finally:
                try:
                    if job_id is None and app.jobs:
                        job_id = next(iter(app.jobs))
                    if job_id:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                        def request(path: str, data: bytes | None = None) -> dict:
                            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                                data=data, headers={'X-Sky-Token': app.token})
                            with opener.open(req, timeout=20) as response:
                                return json.load(response)

                        try:
                            request('/api/jobs/' + job_id + '/retire', b'')
                            for _ in range(60):
                                retired = request('/api/jobs/' + job_id)
                                if retired['deployment_state'] != 'deleting':
                                    break
                                time.sleep(1)
                            if retired['deployment_state'] != 'deleted':
                                raise AssertionError('Retirement API did not confirm deletion')
                        finally:
                            for attempt in range(1, 4):
                                suffix = f'{job_id}-a{attempt}'
                                subprocess.run(['docker', 'rm', '-f', f'sky-{suffix}'], capture_output=True)
                                subprocess.run(['docker', 'image', 'rm', f'sky/{suffix}:latest'], capture_output=True)
                finally:
                    chrome.terminate()
                    try:
                        chrome.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        chrome.kill()
                        chrome.wait(timeout=10)
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=10)


if __name__ == '__main__':
    main()
