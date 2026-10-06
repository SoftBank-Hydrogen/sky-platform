"""Real Chrome and HTTP rehearsal of the combined RDS-create/deploy UI without AWS."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.analysis import AISettings
from sky_platform.runtime.aws import AwsSettings
from sky_platform.runtime.postgres import PostgresRequest
from sky_platform.runtime.server import App, handler_for
from tests.smoke_aws_postgres_api import archive


CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')
APPLICATION = 'dbdrill-1234abcd'


def run(*, fail_create: bool = False, wait_input: bool = False,
        reject_plan: bool = False, manual_resume: bool = False,
        reconnect: bool = False) -> dict:
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                           service_security_group='sg-33333333')
    request = PostgresRequest(APPLICATION, settings.expected_account, settings.region,
                              'vpc-12345678', ('subnet-11111111', 'subnet-22222222'),
                              settings.service_security_group)
    quote = {'account': request.account, 'region': request.region,
             'vpc_id': request.vpc_id, 'subnet_ids': list(request.subnet_ids),
             'stack_name': request.stack_name, 'database_id': request.database_id,
             'engine_version': '18.3', 'instance_class': 'db.t4g.micro',
             'storage_type': 'gp3', 'storage_gib': 20,
             'pricing': {'baseline_730h_usd': '20.87'}}
    database = {'database_id': request.database_id}
    if sum((fail_create, wait_input, reject_plan, manual_resume)) > 1:
        raise ValueError('Choose one local drill scenario')
    if reconnect and not wait_input:
        raise ValueError('Browser reconnect drill requires environment input')
    changed_quote = {**quote, 'pricing': {'baseline_730h_usd': '22.00'}}
    probe_key = 'local-browser-probe-key'
    network = {'account': request.account, 'region': request.region,
               'vpc_id': request.vpc_id, 'subnet_ids': list(request.subnet_ids),
               'availability_zones': ['ap-northeast-2a', 'ap-northeast-2c']}

    def reject_aws(_adapter, args, **_kwargs):
        raise AssertionError('Unexpected AWS command in local drill: ' + repr(args))

    with tempfile.TemporaryDirectory(prefix='sky-one-action-browser-') as directory, \
            patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', autospec=True,
                  side_effect=reject_aws), \
            patch('sky_platform.runtime.postgres_operations.AwsPostgresProvisioner.preflight',
                  side_effect=[quote, changed_quote] if reject_plan else None,
                  return_value=quote) as preflight, \
            patch('sky_platform.runtime.postgres_operations.AwsPostgresProvisioner.assert_stack_available'), \
            patch('sky_platform.runtime.postgres_operations.AwsPostgresProvisioner.create',
                  side_effect=ValueError('simulated RDS creation failure') if fail_create else None,
                  return_value=database) as create, \
            patch('sky_platform.runtime.server.AwsPostgresProvisioner.inspect_current',
                  return_value=database) as inspect, \
            patch('sky_platform.runtime.server.discover_default_network',
                  return_value=network), \
            patch.object(AwsSettings, 'unavailable_reason', return_value=None):
        root = Path(directory)
        zip_path = root / 'postgres-app.zip'
        zip_path.write_bytes(archive())
        app = App(root / 'app', AISettings('fixture-only', 'scripted'),
                  aws_settings=settings, monitor_interval=0,
                  agent_factory=lambda _: object())
        deployments = []
        agent_calls = []

        def finish_deployment(job_id, environment=None):
            agent_calls.append(job_id)
            if manual_resume and len(agent_calls) == 1:
                with app.lock:
                    app.jobs[job_id]['status'] = 'interrupted'
                    app.save(job_id)
                return
            if wait_input and environment is None:
                shutil.copytree(Path(app.jobs[job_id]['project']),
                                app.root / job_id / 'work')
                with app.lock:
                    app.jobs[job_id]['status'] = 'waiting_input'
                    app.jobs[job_id]['missing_environment'] = ['PROBE_KEY']
                    app.save(job_id)
                return
            if wait_input and environment != {'PROBE_KEY': probe_key}:
                raise AssertionError('Browser did not resume with the requested environment value')
            deployments.append(job_id)
            with app.lock:
                app.jobs[job_id]['status'] = 'succeeded'
                app.jobs[job_id]['result'] = {
                    'url': 'https://example.test', 'target': 'aws-ecs-express'}
                app.save(job_id)

        class QuietHandler(handler_for(app)):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = root / 'chrome-profile'
        try:
            with patch.object(app, 'run_agent', side_effect=finish_deployment), \
                    (root / 'chrome.log').open('w') as log:
                chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
                    '--no-default-browser-check', '--disable-gpu',
                    '--disable-background-networking', '--no-proxy-server',
                    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                    '--remote-allow-origins=*', '--user-data-dir=' + str(profile),
                    'about:blank'], stdout=log, stderr=subprocess.STDOUT)
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
                        'one-action-failed-local' if fail_create else
                        'one-action-plan-rejected-local' if reject_plan else
                        'one-action-manual-resume-local' if manual_resume else
                        'one-action-submit-only-local' if reconnect else
                        'one-action-deploy' if wait_input else 'one-action-local',
                        str(zip_path)], check=True, timeout=60,
                        env={**os.environ, 'SKY_BROWSER_PROBE_KEY': probe_key})
                    if reconnect:
                        subprocess.run(['node', str(DRIVER),
                            f'http://127.0.0.1:{server.server_port}/',
                            port_file.read_text().splitlines()[0], APPLICATION,
                            'one-action-resume-existing'], check=True, timeout=60,
                            env={**os.environ, 'SKY_BROWSER_PROBE_KEY': probe_key})
                    if wait_input:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline and not deployments:
                            time.sleep(.1)
                finally:
                    chrome.terminate()
                    try:
                        chrome.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        chrome.kill()
                        chrome.wait(timeout=10)
            jobs = list(app.jobs.values())
            expected_status = 'interrupted' if fail_create else 'failed' if reject_plan else 'succeeded'
            operation = (app.postgres_operations.get(APPLICATION)
                         if not reject_plan else None)
            operation_status = 'needs_attention' if fail_create else 'succeeded'
            if (len(jobs) != 1 or deployments != ([] if fail_create or reject_plan else [jobs[0]['id']])
                    or jobs[0]['status'] != expected_status
                    or jobs[0]['infrastructure_plan']['database'] != {
                        'binding': 'create', 'database_id': request.database_id}
                    or (operation is not None and operation['status'] != operation_status)
                    or (operation is not None and jobs[0]['postgres_creation_id'] !=
                        operation['creation_id'])
                    or (reject_plan and (APPLICATION in app.postgres_operations.operations
                        or 'postgres_creation_id' in jobs[0]))):
                raise AssertionError('Browser one-action job did not preserve the RDS binding: '
                                     + repr({'job_count': len(jobs), 'deployments': deployments,
                                             'status': jobs[0]['status'] if jobs else None,
                                             'operation': operation['status'] if operation else None,
                                             'creation_id': jobs[0].get('postgres_creation_id') if jobs else None}))
            if (preflight.call_count != 2 or create.call_count != (0 if reject_plan else 1)
                    or inspect.call_count != (0 if fail_create or reject_plan else
                                              2 if manual_resume else 1)
                    or agent_calls != ([jobs[0]['id']] * 2 if manual_resume or wait_input else
                                       [] if fail_create or reject_plan else [jobs[0]['id']])):
                raise AssertionError('Browser one-action plan, creation, or ownership check was skipped')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
    return {'application_id': APPLICATION, 'status': 'passed',
            'scenario': 'creation_failure' if fail_create else
                        'plan_rejected' if reject_plan else
                        'manual_resume' if manual_resume else
                        'browser_reconnect' if reconnect else
                        'environment_resume' if wait_input else 'creation_success',
            'aws_mode': 'mocked', 'deployment_executed': False}


if __name__ == '__main__':
    print(json.dumps([run(), run(fail_create=True), run(wait_input=True),
                      run(wait_input=True, reconnect=True),
                      run(reject_plan=True), run(manual_resume=True)],
                     ensure_ascii=False))
