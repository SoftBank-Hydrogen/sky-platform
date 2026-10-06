"""Opt-in HTTP upload -> existing RDS -> ECS data-path smoke with scripted tool calls."""
from __future__ import annotations

import argparse
import io
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.support.agent_fixture import call
from tests.live.smoke_aws_postgres import probe, probe_version
from sky_platform.planning.analysis import AISettings
from sky_platform.deployment.aws import AwsSettings
from sky_platform.database.postgres import AwsPostgresProvisioner, PostgresRequest
from sky_platform.api.server import App, handler_for


SOURCES = {runtime: Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apps' / f'postgres-probe-{runtime}'
           for runtime in ('node', 'python')}
START_SCRIPTS = {'node': 'dockerfile', 'python': 'wsgi:app.py'}


class PostgresFixture:
    """Exercise the real deployment engine without claiming live AI judgment."""
    def __init__(self, _settings, runtime='node'):
        self.index = 0
        self.runtime = runtime

    def next(self, _history):
        actions = [
            ('configure_deployment', {'start_script': START_SCRIPTS[self.runtime], 'build_script': None,
                                      'port': 3000, 'health_path': '/health',
                                      'required_env': ['PROBE_KEY', 'PGHOST', 'PGPORT',
                                                       'PGDATABASE', 'PGUSER', 'PGPASSWORD']}),
            ('deploy_application', {}),
        ]
        name, arguments = actions[self.index]
        self.index += 1
        return call(name, arguments, self.index)


def archive(runtime='node', version=None) -> bytes:
    source = SOURCES[runtime]
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as bundle:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                name = path.relative_to(source).as_posix()
                entry = 'app.py' if runtime == 'python' else 'server.js'
                if version is not None and name == entry:
                    original = path.read_text()
                    old = ('return jsonify(ok=True)' if runtime == 'python'
                           else 'reply(response, 200, {ok: true});')
                    new = (f"return jsonify(ok=True, version='{version}')" if runtime == 'python'
                           else f"reply(response, 200, {{ok: true, version: '{version}'}});")
                    if original.count(old) != 1:
                        raise AssertionError(f'Cannot stamp {runtime} {version} health response')
                    bundle.writestr(name, original.replace(old, new, 1))
                else:
                    bundle.write(path, name)
    return content.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify API upload against an existing Sky RDS')
    parser.add_argument('--apply', action='store_true', help='Create temporary billable ECS resources')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--probe-runtime', choices=('node', 'python'), default='node')
    parser.add_argument('--verify-update', action='store_true',
                        help='Upload a changed v2 release and verify URL/data preservation')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'\d{12}', args.account):
        parser.error('--account must be a 12-digit AWS account ID')
    request = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
    request.validate()
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True,
                           service_security_group=args.service_security_group)
    settings.validate()
    identity = json.loads(subprocess.check_output(['aws', 'sts', 'get-caller-identity',
        '--region', args.region, '--output', 'json', '--no-cli-pager'], text=True, timeout=20))
    if identity.get('Account') != args.account:
        raise RuntimeError('현재 AWS 계정이 지정한 계정과 다릅니다.')
    database = AwsPostgresProvisioner(request).inspect_current()
    print('Existing DB verified:', database['database_id'], database['status'], flush=True)
    if not args.apply:
        print('읽기 전용 확인 완료. --apply 없이는 ECS 리소스를 생성하지 않습니다.', flush=True)
        return
    subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                   check=True, capture_output=True, text=True, timeout=20)

    state = Path(tempfile.mkdtemp(prefix='sky-postgres-api-smoke-'))
    app = App(state, AISettings('fixture-only', 'scripted'),
              lambda settings: PostgresFixture(settings, args.probe_runtime),
              aws_settings=settings, monitor_interval=0)
    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{server.server_port}'
    key = secrets.token_urlsafe(32)
    record_id = uuid.uuid4().hex
    job_id = None
    job = None
    retired = False

    def api(path, data=None, headers=None):
        values = {'X-Sky-Token': app.token, **(headers or {})}
        with opener.open(urllib.request.Request(base + path, data=data, headers=values),
                         timeout=30) as response:
            return json.load(response)

    def deploy(version=None):
        nonlocal job_id, job
        job_id = api('/api/deployments', archive(args.probe_runtime, version), {
            'X-Deploy-Target': 'aws-ecs-express', 'X-Public-Access': 'true',
            'X-Application-Id': args.application, 'X-Postgres-Existing': 'true',
            'X-Postgres-Vpc-Id': args.vpc_id,
            'X-Postgres-Subnet-Ids': ','.join(args.subnet_id)})['id']
        print('Started API DB job:', version or 'single', job_id, flush=True)
        deadline = time.monotonic() + 1800
        resumed = False
        last_stage = None
        while time.monotonic() < deadline:
            job = api('/api/jobs/' + job_id)
            stage = job['events'][-1]['stage'] if job.get('events') else 'starting'
            if stage != last_stage:
                print('Stage:', stage, flush=True)
                last_stage = stage
            if job['status'] == 'waiting_input':
                if resumed:
                    raise AssertionError('API job requested environment again after resume')
                if job.get('missing_environment') != ['PROBE_KEY']:
                    raise AssertionError('Unexpected environment request: '
                                         + repr(job.get('missing_environment')))
                api('/api/deployments/' + job_id + '/resume',
                    json.dumps({'environment': {'PROBE_KEY': key}}).encode())
                resumed = True
            elif job['status'] != 'running':
                break
            time.sleep(2)
        else:
            raise TimeoutError('API PostgreSQL deployment exceeded 30 minutes')
        if job['status'] != 'succeeded' or not resumed or job.get('attempts') != 1:
            raise AssertionError('API PostgreSQL deployment failed: '
                                 + json.dumps(job.get('events', [])[-8:], ensure_ascii=False))
        result = job['result']
        if (result.get('database', {}).get('database_id') != database['database_id']
                or result.get('migration', {}).get('cleanup_complete') is not True
                or job.get('postgres', {}).get('application_id') != args.application):
            raise AssertionError('API job did not bind and migrate the owned database')
        if not api('/api/jobs/' + job_id + '/health').get('healthy'):
            raise AssertionError('API health check failed')
        return job

    try:
        first = deploy('v1' if args.verify_update else None)
        first_result = first['result']
        if args.verify_update:
            probe_version(first_result['url'], 'v1')
        probe(first_result['url'], key, record_id, 'POST')
        probe(first_result['url'], key, record_id, 'GET')
        if args.verify_update:
            second = deploy('v2')
            result = second['result']
            if (second.get('replaces_job_id') != first['id']
                    or result['url'] != first_result['url']
                    or result['service_arn'] != first_result['service_arn']
                    or result['image'] == first_result['image']):
                raise AssertionError('API update did not replace the prior ECS release')
            previous = api('/api/jobs/' + first['id'])
            if previous.get('deployment_state') != 'superseded':
                raise AssertionError('API did not mark the previous release superseded')
            probe_version(result['url'], 'v2')
            probe(result['url'], key, record_id, 'GET')
        else:
            result = first_result
        probe(result['url'], key, record_id, 'DELETE')
        print('PASS: upload -> environment resume -> ECS + RDS -> HTTP data read/write', flush=True)
        api('/api/jobs/' + job_id + '/retire', b'')
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            job = api('/api/jobs/' + job_id)
            if job.get('deployment_state') in {'deleted', 'delete_failed'}:
                break
            time.sleep(3)
        retired = job.get('deployment_state') == 'deleted'
        if not retired:
            raise RuntimeError('API ECS retirement did not complete')
        print('PASS: temporary ECS service and image retired through API; RDS retained', flush=True)
    finally:
        server.shutdown()
        server.server_close()
        if retired:
            shutil.rmtree(state)
        else:
            print('Smoke state retained for diagnosis:', state, flush=True)
            if job_id:
                print('Job:', job_id, 'Status:', job.get('status') if job else 'unknown', flush=True)


if __name__ == '__main__':
    main()
