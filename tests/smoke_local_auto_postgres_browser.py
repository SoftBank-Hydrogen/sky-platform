"""Chrome upload-to-plan drill for an owned RDS, with no AWS or deployment execution."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.analysis import AISettings
from sky_platform.runtime.aws import AwsSettings
from sky_platform.runtime.server import App, handler_for
from tests.smoke_aws_postgres_api import archive


CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')
APPLICATION = 'dbdrill-1234abcd'


def run() -> dict:
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                           service_security_group='sg-33333333')
    database = {'database_id': 'sky-' + APPLICATION,
                'account': settings.expected_account, 'region': settings.region,
                'vpc_id': 'vpc-12345678',
                'subnet_ids': ['subnet-11111111', 'subnet-22222222'],
                'status': 'available'}
    def reject_aws(_adapter, args, **_kwargs):
        raise AssertionError('Unexpected AWS command in local drill: ' + repr(args))

    with tempfile.TemporaryDirectory(prefix='sky-auto-db-browser-') as directory, \
            patch('sky_platform.runtime.server.discover_existing_postgres', return_value=database) as discover, \
            patch('sky_platform.runtime.server.AwsPostgresProvisioner.inspect_current',
                  return_value=database) as inspect, \
            patch('sky_platform.runtime.aws.AwsExpressAdapter.aws', autospec=True,
                  side_effect=reject_aws), \
            patch.object(AwsSettings, 'unavailable_reason', return_value=None):
        root = Path(directory)
        zip_path = root / 'postgres-app.zip'
        zip_path.write_bytes(archive())
        app = App(root / 'app', AISettings('fixture-only', 'scripted'),
                  aws_settings=settings, monitor_interval=0,
                  agent_factory=lambda _: object())

        class QuietHandler(handler_for(app)):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = root / 'chrome-profile'
        with patch.object(app, 'run_agent', return_value=None) as runner, \
                (root / 'chrome.log').open('w') as log:
            chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
                '--no-default-browser-check', '--disable-gpu', '--disable-background-networking',
                '--no-proxy-server', '--remote-debugging-address=127.0.0.1',
                '--remote-debugging-port=0', '--remote-allow-origins=*',
                '--user-data-dir=' + str(profile), 'about:blank'],
                stdout=log, stderr=subprocess.STDOUT)
            try:
                port_file = profile / 'DevToolsActivePort'
                deadline = time.monotonic() + 30
                while not port_file.is_file():
                    if chrome.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Local Chrome debugging endpoint did not start')
                    time.sleep(.2)
                subprocess.run(['node', str(DRIVER),
                    f'http://127.0.0.1:{server.server_port}/',
                    port_file.read_text().splitlines()[0], APPLICATION,
                    'auto-existing-plan', str(zip_path)], check=True, timeout=120)
                jobs = list(app.jobs.values())
                if len(jobs) != 1:
                    raise AssertionError('Browser upload did not record exactly one job')
                job = jobs[0]
                if (job['requested_target'] != 'auto'
                        or job['target'] != 'aws-ecs-express'
                        or job['infrastructure_plan']['planner'] != 'policy'
                        or job['infrastructure_plan']['database']['database_id'] != database['database_id']
                        or job['postgres']['vpc_id'] != database['vpc_id']
                        or job['postgres']['subnet_ids'] != database['subnet_ids']):
                    raise AssertionError('Browser upload did not preserve the owned RDS plan')
                discover.assert_called_once_with(APPLICATION, settings)
                inspect.assert_called_once_with()
                runner.assert_called_once_with(job['id'])
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
    return {'application_id': APPLICATION, 'status': 'passed',
            'aws_mode': 'mocked', 'deployment_executed': False}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
