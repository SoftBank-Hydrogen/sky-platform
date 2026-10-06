"""Opt-in real Chrome UI -> existing RDS -> ECS smoke with scripted AI tools."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from application.analysis import AISettings
from adapters.aws.ecs import AwsSettings
from adapters.aws.postgres import discover_existing_postgres
from adapters.aws.postgres_snapshot import inspect_snapshot, plan_snapshot
from interfaces.http.server import App, handler_for
from tests.live.smoke_aws_postgres_api import PostgresFixture, archive
from tests.live.smoke_aws_postgres import probe, probe_version

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_cdp.mjs')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify the browser PostgreSQL deployment path')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Deploy temporary billable ECS resources')
    mode.add_argument('--browser-read-only', action='store_true',
                      help='Verify browser network and existing RDS lookup without deploying')
    mode.add_argument('--snapshot-apply', action='store_true',
                      help='Create and retain a billable manual RDS snapshot through real Chrome')
    parser.add_argument('--application', default='demo-app')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--snapshot-name', default='backup-' + datetime.now(timezone.utc).strftime('%Y%m%d'))
    parser.add_argument('--probe-runtime', choices=('node', 'python'), default='node')
    parser.add_argument('--verify-update', action='store_true',
                        help='Deploy a changed v2 ZIP through Chrome and verify URL/data preservation')
    args = parser.parse_args(argv)
    if args.verify_update and not args.apply:
        parser.error('--verify-update requires --apply')
    if args.application != 'demo-app':
        parser.error('The browser driver currently uses the demo-app fixture only')
    if not re.fullmatch(r'\d{12}', args.account):
        parser.error('--account must be a 12-digit AWS account ID')
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True,
                           service_security_group=args.service_security_group)
    settings.validate()
    database = discover_existing_postgres(args.application, settings)
    print('Existing DB verified:', database['database_id'], database['status'], flush=True)
    snapshot_id = 'sky-' + args.application + '-' + args.snapshot_name
    if args.snapshot_apply:
        preview = plan_snapshot(args.application, snapshot_id, settings)
        print('Snapshot plan verified:', preview['snapshot_id'], flush=True)
    if not args.apply and not args.browser_read_only and not args.snapshot_apply:
        print('읽기 전용 확인 완료. --apply 없이는 브라우저 배포나 ECS 생성을 시작하지 않습니다.', flush=True)
        return
    if not CHROME.is_file():
        raise RuntimeError('Chrome executable not found')
    if args.apply:
        subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                       check=True, capture_output=True, text=True, timeout=20)
    state = Path(tempfile.mkdtemp(prefix='sky-postgres-browser-smoke-'))
    app = App(state, AISettings('fixture-only', 'scripted'),
              lambda settings: PostgresFixture(settings, args.probe_runtime),
              aws_settings=settings, monitor_interval=0)
    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    archive_path = state / 'probe.zip'
    archive_path.write_bytes(archive(args.probe_runtime, 'v1' if args.verify_update else None))
    update_path = state / 'probe-v2.zip'
    if args.verify_update:
        update_path.write_bytes(archive(args.probe_runtime, 'v2'))
    profile = state / 'chrome-profile'
    chrome_log = (state / 'chrome.log').open('w')
    chrome = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
        '--no-default-browser-check', '--disable-gpu', '--disable-background-networking',
        '--no-proxy-server', '--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0', '--remote-allow-origins=*',
        '--user-data-dir=' + str(profile), 'about:blank'],
        stdout=chrome_log, stderr=subprocess.STDOUT)
    key = secrets.token_urlsafe(32)
    record_id = uuid.uuid4().hex
    probe_state = state / 'probe-state.json'
    probe_state.write_text(json.dumps({'key': key, 'record_id': record_id}))
    probe_state.chmod(0o600)
    job_id = None
    retired = False
    def api(path, data=None):
        request = urllib.request.Request(url.rstrip('/') + path, data=data,
                                         headers={'X-Sky-Token': app.token})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    def browser_upload(path, previous_id=None):
        subprocess.run(['node', str(DRIVER), url, port, str(path), 'apply',
                        'browser-read-only', previous_id or ''],
                       check=True, timeout=40 * 60, env=environment)
    def validate_job(job):
        if (job['status'] != 'succeeded' or job.get('attempts') != 1
                or job.get('result', {}).get('database', {}).get('database_id') != database['database_id']
                or job.get('result', {}).get('migration', {}).get('cleanup_complete') is not True):
            raise AssertionError('Browser job did not complete an owned DB deployment')
        if not api('/api/jobs/' + job['id'] + '/health').get('healthy'):
            raise AssertionError('Browser deployment health check failed')
    try:
        port_file = profile / 'DevToolsActivePort'
        deadline = time.monotonic() + 30
        while not port_file.is_file():
            if chrome.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Chrome debugging endpoint did not start; see ' + str(state / 'chrome.log'))
            time.sleep(.2)
        port = port_file.read_text().splitlines()[0]
        environment = {**os.environ, 'SKY_BROWSER_PROBE_KEY': key}
        if args.apply:
            browser_upload(archive_path)
        else:
            subprocess.run(['node', str(DRIVER), url, port, str(archive_path),
                            'snapshot-apply' if args.snapshot_apply else 'read-only',
                            args.snapshot_name if args.snapshot_apply else 'browser-read-only'],
                           check=True, timeout=40 * 60, env=environment)
        if args.browser_read_only:
            if app.jobs:
                raise AssertionError('Read-only browser check created a deployment job')
            if app.snapshot_operations.operations:
                raise AssertionError('Read-only browser check recorded a snapshot create operation')
            if app.postgres_retirement_operations.operations:
                raise AssertionError('Read-only browser check recorded a database retirement operation')
            retired = True
            print('PASS: real browser verified RDS, backup, snapshot and retirement plans without mutation', flush=True)
            return
        if args.snapshot_apply:
            operation = app.snapshot_operations.get(args.application, snapshot_id)
            inspected = inspect_snapshot(args.application, snapshot_id, settings)
            if operation['status'] != 'succeeded' or inspected['status'] != 'available':
                raise AssertionError('브라우저 작업 기록과 AWS 수동 스냅샷 완료 상태가 다릅니다.')
            retired = True
            print('PASS: browser-created encrypted snapshot available:', inspected['snapshot_arn'], flush=True)
            print('Snapshot retained; backup storage may incur charges.', flush=True)
            return
        jobs = list(app.jobs.values())
        if len(jobs) != 1:
            raise AssertionError('Expected one browser deployment job')
        first = jobs[0]
        job_id = first['id']
        validate_job(first)
        endpoint = first['result']['url']
        if args.verify_update:
            probe_version(endpoint, 'v1')
        probe(endpoint, key, record_id, 'POST')
        probe(endpoint, key, record_id, 'GET')
        if args.verify_update:
            browser_upload(update_path, first['id'])
            jobs = list(app.jobs.values())
            if len(jobs) != 2:
                raise AssertionError('Expected two browser deployment jobs')
            second = next(item for item in jobs if item['id'] != first['id'])
            job_id = second['id']
            validate_job(second)
            current = second['result']
            if (second.get('replaces_job_id') != first['id']
                    or first.get('deployment_state') != 'superseded'
                    or current['url'] != endpoint
                    or current['service_arn'] != first['result']['service_arn']
                    or current['image'] == first['result']['image']):
                raise AssertionError('Browser update did not replace the prior ECS release')
            probe_version(endpoint, 'v2')
            probe(endpoint, key, record_id, 'GET')
        probe(endpoint, key, record_id, 'DELETE')
        print('PASS: real browser UI -> ECS + RDS -> HTTP write/read/delete', flush=True)
    finally:
        try:
            jobs = list(app.jobs.values())
            pending_update = any(item.get('aws_update_submitted') and item.get('status') in {
                'running', 'failed', 'interrupted'} and not item.get('aws_reconciled')
                for item in jobs)
            active = [item for item in jobs if item.get('status') == 'succeeded'
                      and item.get('deployment_state', 'active') in {'active', 'delete_failed'}]
            if pending_update:
                print('AWS update result is uncertain; state retained for reconciliation:', state, flush=True)
            elif len(active) == 1:
                job_id = active[0]['id']
                api('/api/jobs/' + job_id + '/retire', b'')
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    job = api('/api/jobs/' + job_id)
                    if job.get('deployment_state') in {'deleted', 'delete_failed'}:
                        break
                    time.sleep(3)
                retired = job.get('deployment_state') == 'deleted'
                print('Temporary ECS retirement:', 'confirmed' if retired else 'needs attention', flush=True)
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=10)
            chrome_log.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if retired:
                shutil.rmtree(state)
            else:
                print('Smoke state retained for diagnosis:', state, flush=True)
                if job_id:
                    print('Job:', job_id, flush=True)
    if not retired:
        raise RuntimeError('Temporary ECS resources were not confirmed retired')


if __name__ == '__main__':
    main()
