"""Opt-in live OpenAI -> source repair -> AWS ECS Express -> HTTP -> retirement smoke."""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from application.analysis import AISettings
from adapters.aws.ecs import AwsExpressAdapter, AwsSettings
from interfaces.http.server import App, handler_for


SAMPLE = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apps' / 'unready-node'


def preflight(account: str, region: str, ai: AISettings) -> AwsSettings:
    if not re.fullmatch(r'\d{12}', account):
        raise ValueError('--account에는 12자리 AWS 계정 ID가 필요합니다.')
    if not ai.available:
        raise ValueError('OPENAI_API_KEY를 설정하세요.')
    settings = AwsSettings(region, expected_account=account, account_pin_required=True)
    settings.validate()
    reason = settings.unavailable_reason()
    if reason:
        raise ValueError(reason)
    try:
        subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                       capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Docker 데몬이 준비되지 않았습니다. AWS 리소스를 생성하지 않았습니다.') from None
    try:
        identity = json.loads(subprocess.check_output(
            ['aws', 'sts', 'get-caller-identity', '--region', region,
             '--output', 'json', '--no-cli-pager'], text=True, timeout=20,
            stderr=subprocess.DEVNULL))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('AWS STS 계정을 확인하지 못했습니다. AWS 로그인과 권한을 확인하세요.') from None
    if identity.get('Account') != account:
        raise ValueError('현재 AWS 계정이 --account와 다릅니다. 리소스를 생성하지 않았습니다.')
    return settings


def sample_archive() -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        for path in sorted(SAMPLE.iterdir()):
            bundle.write(path, path.name)
    return archive.getvalue()


def verify_job(job: dict, job_id: str) -> None:
    if job.get('status') != 'succeeded':
        raise AssertionError('AI 배포가 성공 상태에 도달하지 않았습니다.')
    result = job.get('result') or {}
    if (job.get('target') != 'aws-ecs-express' or result.get('target') != 'aws-ecs-express'
            or result.get('service') != 'sky-' + result.get('owner_attempt', '')
            or not re.fullmatch(re.escape(job_id) + r'-a[1-3]', result.get('owner_attempt', ''))
            or not result.get('url', '').startswith('https://')):
        raise AssertionError('AWS 배포 결과와 소유권이 예상과 다릅니다.')
    changed = {change.get('path') for change in job.get('changes', [])
               if isinstance(change.get('diff'), str) and change['diff'].strip()}
    if not {'package.json', 'server.js'} <= changed:
        raise AssertionError('실제 AI가 start 스크립트와 서버 바인딩을 모두 수정하지 않았습니다.')
    project = Path(job['project'])
    if (json.loads((project / 'package.json').read_text())['scripts'] != {}
            or "'127.0.0.1'" not in (project / 'server.js').read_text()):
        raise AssertionError('업로드 원본이 변경됐습니다.')
    work = project.parent / 'work'
    repaired_server = (work / 'server.js').read_text()
    if (not json.loads((work / 'package.json').read_text()).get('scripts', {}).get('start')
            or '127.0.0.1' in repaired_server or 'process.env.PORT' not in repaired_server):
        raise AssertionError('작업용 복사본에서 배포 수정 결과를 확인하지 못했습니다.')


def run_live(settings: AwsSettings, ai: AISettings, timeout: int) -> None:
    state = Path(tempfile.mkdtemp(prefix='sky-aws-live-ai-'))
    app = App(state, ai, aws_settings=settings, monitor_interval=0)

    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{server.server_port}'

    def request(path, data=None, headers=None):
        with opener.open(urllib.request.Request(base + path, data=data, headers={
                'X-Sky-Token': app.token, **(headers or {})}), timeout=20) as response:
            return json.load(response)

    job_id = None
    job = None
    retired = False
    retired_via_api = False
    try:
        job_id = request('/api/deployments', sample_archive(), {
            'X-Deploy-Target': 'aws-ecs-express', 'X-Public-Access': 'true',
            'X-Application-Id': 'aws-live-ai-smoke'})['id']
        print('Live AI + AWS job:', job_id, flush=True)
        deadline = time.monotonic() + timeout
        last_stage = None
        while time.monotonic() < deadline:
            job = request('/api/jobs/' + job_id)
            stage = job['events'][-1]['stage'] if job.get('events') else 'starting'
            if stage != last_stage:
                print('Stage:', stage, flush=True)
                last_stage = stage
            if job['status'] != 'running':
                break
            time.sleep(2)
        else:
            raise TimeoutError('AI/AWS 작업 시간 제한을 넘었습니다. 작업 기록과 AWS 리소스를 확인하세요.')
        if job['status'] != 'succeeded':
            messages = [event.get('message', '') for event in job.get('events', [])[-8:]]
            raise RuntimeError('AI/AWS 배포 실패: ' + ' | '.join(messages))
        verify_job(job, job_id)
        health = request('/api/jobs/' + job_id + '/health')
        if not health.get('healthy'):
            raise AssertionError('AWS 서비스의 현재 상태 검사가 실패했습니다: ' + str(health.get('reason')))
        with opener.open(job['result']['url'], timeout=20) as response:
            body = json.load(response)
            if response.status != 200 or body.get('message') != 'Original application is running':
                raise AssertionError('공개 AWS URL의 앱 응답이 예상과 다릅니다.')
        print('PASS: live AI -> source edits -> AWS service -> public HTTP 200 -> health API', flush=True)
        request('/api/jobs/' + job_id + '/retire', b'')
        retire_deadline = time.monotonic() + 1200
        while time.monotonic() < retire_deadline:
            job = request('/api/jobs/' + job_id)
            if job.get('deployment_state') in {'deleted', 'delete_failed'}:
                break
            time.sleep(3)
        retired = job.get('deployment_state') == 'deleted'
        if not retired:
            raise RuntimeError('AWS 서비스 종료를 확인하지 못했습니다. 작업 기록에서 다시 시도하세요.')
        retired_via_api = True
        print('PASS: owned ECS service and ECR image retired; shared stack retained', flush=True)
    finally:
        server.shutdown()
        server.server_close()
        if job_id and not retired and job and job.get('status') == 'succeeded' and job.get('result'):
            try:
                result = job['result']
                AwsExpressAdapter(lambda stage, message: print(f'[{stage}] {message}', flush=True),
                                  settings).retire(result, result['owner_attempt'])
                retired = True
            except Exception as exc:
                print('AWS 종료 재시도 실패:', str(exc), flush=True)
        elif job_id and job and job.get('status') in {'failed', 'interrupted'}:
            print('실패한 배포 시도의 AWS 리소스 정리를 다시 요청합니다.', flush=True)
            for number in range(1, min(job.get('attempts', 0), 3) + 1):
                attempt = f'{job_id}-a{number}'
                adapter = AwsExpressAdapter(lambda stage, message: print(f'[{stage}] {message}', flush=True), settings)
                adapter.service_arn = (f'arn:aws:ecs:{settings.region}:{settings.expected_account}:'
                                       f'service/default/sky-{attempt}')
                adapter.service_created = True
                adapter.image = (f'{settings.expected_account}.dkr.ecr.{settings.region}.amazonaws.com/'
                                 f'sky-managed:{attempt}')
                adapter.image_pushed = True
                adapter.cleanup_failure(attempt)
        if retired_via_api and job and job.get('attempts') == 1:
            shutil.rmtree(state)
        else:
            print('작업 기록을 보존했습니다:', state, flush=True)
            print('추가 시도나 종료 실패로 남은 AWS 리소스가 있는지 확인하세요. 공유 스택은 유지됩니다.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Live AI repair and AWS ECS Express deployment smoke')
    parser.add_argument('--apply', action='store_true', help='Call OpenAI and create billable AWS resources')
    parser.add_argument('--account', required=True, help='Expected 12-digit AWS account ID')
    parser.add_argument('--region', required=True)
    parser.add_argument('--timeout', type=int, default=7200, help='Deployment wait limit in seconds')
    args = parser.parse_args(argv)
    if not 600 <= args.timeout <= 14400:
        parser.error('--timeout must be between 600 and 14400 seconds')
    ai = AISettings.from_environment()
    try:
        settings = preflight(args.account, args.region, ai)
    except ValueError as exc:
        parser.exit(2, f'사전 점검 실패: {exc}\n')
    if not args.apply:
        print('읽기 전용 사전 점검 완료. --apply를 지정해야 OpenAI 호출과 AWS 리소스 생성을 시작합니다.')
        return
    run_live(settings, ai, args.timeout)


if __name__ == '__main__':
    main()
