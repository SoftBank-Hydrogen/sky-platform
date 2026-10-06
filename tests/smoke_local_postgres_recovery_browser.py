"""Real Chrome UI recovery drill backed only by deterministic local AWS responses."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.analysis import AISettings
from sky_platform.runtime.aws import AwsSettings
from sky_platform.runtime.postgres import PostgresRequest
from sky_platform.runtime.server import App, handler_for


CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')
ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
APPLICATION = 'dbdrill-1234abcd'
STACK_ID = (f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:'
            f'stack/sky-db-{APPLICATION}/stack-id')


def run() -> dict:
    """Use a real browser and HTTP server; reject every unexpected AWS command."""
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    settings = AwsSettings(REGION, expected_account=ACCOUNT,
                           service_security_group='sg-33333333')
    request = PostgresRequest(APPLICATION, ACCOUNT, REGION, 'vpc-12345678',
                              ('subnet-11111111', 'subnet-22222222'), 'sg-33333333')
    quote = {'account': ACCOUNT, 'region': REGION, 'vpc_id': request.vpc_id,
             'subnet_ids': list(request.subnet_ids), 'stack_name': request.stack_name,
             'database_id': request.database_id, 'engine_version': '18.3',
             'instance_class': 'db.t4g.micro', 'storage_type': 'gp3',
             'storage_gib': 20, 'pricing': {'baseline_730h_usd': '20.87'}}
    state = {'stack_status': 'ROLLBACK_COMPLETE', 'deleted': 0, 'unprotected': 0}
    def aws(_adapter, args, **_kwargs):
        if args[:2] == ['sts', 'get-caller-identity']:
            return json.dumps({'Account': ACCOUNT})
        if args[:2] == ['cloudformation', 'describe-stacks']:
            return json.dumps({'Stacks': [{'StackId': STACK_ID,
                'StackStatus': state['stack_status'], 'Tags': [
                    {'Key': 'sky-managed', 'Value': 'true'},
                    {'Key': 'sky-app', 'Value': APPLICATION}]}]})
        if args[:2] == ['cloudformation', 'list-stack-resources']:
            return json.dumps({'StackResourceSummaries': [
                {'LogicalResourceId': 'WaitHandle', 'ResourceStatus': 'DELETE_COMPLETE'},
                {'LogicalResourceId': 'UnsignaledWait', 'ResourceStatus': 'CREATE_FAILED'}]})
        if args[:2] == ['cloudformation', 'list-stacks']:
            return json.dumps({'StackSummaries': [{'StackName': request.stack_name,
                'StackStatus': state['stack_status']}]})
        if args[:2] == ['rds', 'describe-db-instances']:
            return json.dumps({'DBInstances': []})
        if args[:2] == ['rds', 'describe-db-snapshots']:
            return json.dumps({'DBSnapshots': []})
        if args[:2] == ['cloudformation', 'update-termination-protection']:
            state['unprotected'] += 1
            return '{}'
        if args[:2] == ['cloudformation', 'delete-stack']:
            state['deleted'] += 1
            return '{}'
        if args[:3] == ['cloudformation', 'wait', 'stack-delete-complete']:
            state['stack_status'] = 'DELETE_COMPLETE'
            return '{}'
        raise AssertionError('Unexpected AWS command in local drill: ' + repr(args))

    with tempfile.TemporaryDirectory(prefix='sky-recovery-browser-') as directory, \
            patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', autospec=True, side_effect=aws), \
            patch('sky_platform.runtime.postgres_operations.AwsPostgresProvisioner.preflight',
                  return_value=quote):
        root = Path(directory)
        app = App(root / 'app', AISettings('fixture-only', 'scripted'),
                  aws_settings=settings, monitor_interval=0, agent_factory=lambda _: object())
        operation = {'application_id': APPLICATION, 'request': asdict(request),
                     'expected_plan': quote, 'status': 'needs_attention',
                     'created_at': datetime.now(timezone.utc).isoformat(),
                     'message': 'CloudFormation 상태: ROLLBACK_COMPLETE.'}
        app.postgres_operations._save(operation)
        app.postgres_operations.operations[APPLICATION] = operation

        class QuietHandler(handler_for(app)):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = root / 'chrome-profile'
        with (root / 'chrome.log').open('w') as log:
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
                    port_file.read_text().splitlines()[0], APPLICATION, 'recover'],
                    check=True, timeout=120)
                if (state['stack_status'] != 'DELETE_COMPLETE'
                        or state['unprotected'] != 1 or state['deleted'] != 1
                        or app.postgres_operations.get(APPLICATION)['status'] != 'failed_cleaned'
                        or not list(app.postgres_operations.archive.glob(APPLICATION + '-*.json'))):
                    raise AssertionError('Browser recovery did not preserve the expected local/AWS state')
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
            'stack_delete_calls': state['deleted'], 'aws_mode': 'mocked'}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
