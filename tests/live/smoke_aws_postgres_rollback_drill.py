"""Opt-in live CloudFormation rollback drill with no RDS resource in the injected template.

Only a disposable dbdrill-* app is accepted. The test template contains a
WaitCondition that never receives a signal; production templates are untouched.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import sky_platform.database.postgres as postgres
from sky_platform.planning.analysis import AISettings
from sky_platform.deployment.aws import AwsConfigurationError, AwsSettings
from sky_platform.deployment.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, discover_default_network)
from sky_platform.database.postgres import AwsPostgresProvisioner, PostgresRequest
from sky_platform.database.postgres_operations import PostgresOperations
from sky_platform.api.server import App, handler_for
from tests.live.smoke_aws_network import retire_probe
from tests.live.smoke_aws_restore_marker_source import save, save_new

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
DRIVER = Path(__file__).with_name('browser_postgres_create_cdp.mjs')


def _aws(provisioner: AwsPostgresProvisioner, args: list[str]) -> dict:
    return json.loads(provisioner.adapter.aws(args, private=True, quiet=True))


def rollback_template() -> dict:
    """Keep the production create-stack parameters, but create no DB or secret."""
    return {'AWSTemplateFormatVersion': '2010-09-09',
            'Description': 'Disposable Sky rollback drill; creates no RDS resources',
            'Parameters': {name: {'Type': 'String'} for name in (
                'ApplicationId', 'EngineVersion', 'VpcId', 'SubnetIds',
                'ServiceSecurityGroupId')},
            'Resources': {
                'WaitHandle': {'Type': 'AWS::CloudFormation::WaitConditionHandle'},
                'UnsignaledWait': {'Type': 'AWS::CloudFormation::WaitCondition',
                                   'Properties': {'Handle': {'Ref': 'WaitHandle'},
                                                  'Timeout': '1', 'Count': 1}}}}


def inspect_rolled_back(provisioner: AwsPostgresProvisioner, stack_id: str) -> dict:
    """Require owned terminal rollback and absence of app DB before stack cleanup."""
    request = provisioner.request
    prefix = f'arn:aws:cloudformation:{request.region}:{request.account}:stack/{request.stack_name}/'
    if not stack_id.startswith(prefix):
        raise AwsConfigurationError('시험용 스택 ARN이 예상 앱·계정·리전과 다릅니다.')
    identity = _aws(provisioner, ['sts', 'get-caller-identity'])
    if identity.get('Account') != request.account:
        raise AwsConfigurationError('AWS 계정이 시험 요청과 다릅니다.')
    stacks = _aws(provisioner, ['cloudformation', 'describe-stacks',
                                '--stack-name', stack_id]).get('Stacks', [])
    stack = stacks[0] if len(stacks) == 1 else {}
    tags = {item.get('Key'): item.get('Value') for item in stack.get('Tags', [])}
    if (stack.get('StackId') != stack_id
            or stack.get('StackStatus') != 'ROLLBACK_COMPLETE'
            or tags.get('sky-managed') != 'true'
            or tags.get('sky-app') != request.application_id
            or stack.get('EnableTerminationProtection') is not True):
        raise AwsConfigurationError('앱 소유 ROLLBACK_COMPLETE 스택을 확인하지 못했습니다.')
    resources = _aws(provisioner, ['cloudformation', 'list-stack-resources',
                                   '--stack-name', stack_id])
    summaries = resources.get('StackResourceSummaries')
    if (resources.get('NextToken') or not isinstance(summaries, list)
            or {item.get('LogicalResourceId') for item in summaries}
            != {'WaitHandle', 'UnsignaledWait'}
            or {item.get('ResourceType') for item in summaries}
            != {'AWS::CloudFormation::WaitConditionHandle',
                'AWS::CloudFormation::WaitCondition'}
            or any(item.get('ResourceStatus') not in {'DELETE_COMPLETE', 'CREATE_FAILED'}
                   for item in summaries)):
        raise AwsConfigurationError('롤백 스택에 예상 밖의 리소스가 있어 정리하지 않습니다.')
    databases = _aws(provisioner, ['rds', 'describe-db-instances'])
    if (databases.get('Marker') or not isinstance(databases.get('DBInstances'), list)
            or any(item.get('DBInstanceIdentifier') == request.database_id
                   for item in databases['DBInstances'])):
        raise AwsConfigurationError('앱 DB 부재를 확인하지 못했습니다.')
    snapshots = _aws(provisioner, ['rds', 'describe-db-snapshots',
                                   '--snapshot-type', 'manual'])
    if (snapshots.get('Marker') or not isinstance(snapshots.get('DBSnapshots'), list)
            or any(item.get('DBInstanceIdentifier') == request.database_id
                   for item in snapshots['DBSnapshots'])):
        raise AwsConfigurationError('앱 수동 스냅샷 부재를 확인하지 못했습니다.')
    return {'stack_id': stack_id, 'status': stack['StackStatus'],
            'resource_count': len(summaries)}


def retire_before_create(network_provisioner: AwsServiceNetworkProvisioner,
                         created_network: dict, provisioner: AwsPostgresProvisioner,
                         manager: PostgresOperations | None) -> None:
    """Reclaim the drill network only when no DB creation was ever recorded."""
    application = provisioner.request.application_id
    if (manager is not None and (application in manager.operations
                                 or manager.untrusted_unknown
                                 or application in manager.untrusted_applications)):
        raise AwsConfigurationError('DB 생성 기록이 있거나 불확실해 네트워크를 자동 정리하지 않습니다.')
    provisioner.assert_database_absent()
    provisioner.assert_stack_available()
    retire_probe(network_provisioner, created_network['stack_id'],
                 created_network['service_security_group'])


def browser_cleanup(state_dir: Path, application: str, settings: AwsSettings,
                    network: dict) -> PostgresOperations:
    """Run the real product HTTP and Chrome controls against the owned rollback."""
    app = App(state_dir, AISettings('fixture-only', 'scripted'),
              agent_factory=lambda _: object(), aws_settings=settings, monitor_interval=0)
    class QuietHandler(handler_for(app)):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = state_dir / 'chrome-profile'
    with (state_dir / 'chrome.log').open('w') as log:
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
                    raise RuntimeError('Chrome debugging endpoint did not start: '
                                       + str(state_dir / 'chrome.log'))
                time.sleep(.2)
            subnet_ids = network['subnet_ids']
            subprocess.run(['node', str(DRIVER),
                f'http://127.0.0.1:{server.server_port}/',
                port_file.read_text().splitlines()[0], application, 'recover',
                json.dumps({'vpc_id': network['vpc_id'], 'subnet_ids': subnet_ids})],
                check=True, timeout=8 * 60)
            return app.postgres_operations
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=10)
            shutil.rmtree(profile, ignore_errors=True)
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)


def start_after_stable_preflight(manager: PostgresOperations,
                                 request: PostgresRequest) -> dict:
    """Retry only read-only checks; never repeat an accepted creation operation."""
    for attempt in range(4):
        try:
            planned = manager.plan(request)
            return manager.start(request.application_id, planned['plan_id'])
        except AwsConfigurationError as exc:
            if ('기본 PostgreSQL 버전의 암호화된' not in str(exc)
                    or request.application_id in manager.operations or attempt == 3):
                raise
            print('RDS 읽기 전용 구성 조회가 불안정해 생성 기록 전 재확인합니다.', flush=True)
            time.sleep(10)
    raise AssertionError('Unreachable preflight retry state')


def run(args) -> dict:
    if not re.fullmatch(r'dbdrill-[a-f0-9]{8}', args.application):
        raise ValueError('새 dbdrill-<8 hex> 임시 앱만 허용합니다.')
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True)
    network = discover_default_network(settings)
    network_request = ServiceNetworkRequest(args.application, args.account,
                                            args.region, network['vpc_id'])
    network_provisioner = AwsServiceNetworkProvisioner(network_request)
    network_provisioner.preflight()
    if not args.apply:
        return {'application_id': args.application, 'mode': 'read_only',
                'network_stack': network_request.stack_name}
    if args.browser_cleanup:
        if not CHROME.is_file():
            raise RuntimeError('Chrome executable not found')
        subprocess.run(['node', '--version'], check=True, capture_output=True,
                       text=True, timeout=10)

    state_dir = Path('.sky') / 'postgres-rollback-drills' / args.application
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    journal = state_dir / 'journal.json'
    state = {'application_id': args.application, 'account': args.account,
             'region': args.region, 'stage': 'planned', 'status': 'running'}
    save_new(journal, state)
    created_network = None
    stack_id = None
    stack_cleaned = False
    network_cleaned = False
    product_verified = False
    manager = None
    provisioner = None
    precreate_cleaned = False
    try:
        created_network = network_provisioner.create()
        state.update(stage='network_created',
                     network_stack_id=created_network['stack_id'],
                     service_security_group=created_network['service_security_group'])
        save(journal, state)
        request = PostgresRequest(args.application, args.account, args.region,
            network['vpc_id'], tuple(network['subnet_ids']),
            created_network['service_security_group'])
        provisioner = AwsPostgresProvisioner(request)
        manager = PostgresOperations(state_dir / 'database-operations',
            AwsSettings(args.region, expected_account=args.account,
                        account_pin_required=True,
                        service_security_group=request.service_security_group))
        template_file = state_dir / 'intentional-rollback-template.json'
        template_file.write_text(json.dumps(rollback_template()))
        with patch.object(postgres, 'TEMPLATE', template_file):
            started = start_after_stable_preflight(manager, request)
            if started['status'] != 'running':
                raise AssertionError('실패 시험용 RDS 생성 요청이 기록되지 않았습니다.')
            state['stage'] = 'create_recorded'
            save(journal, state)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                operation = manager.get(args.application)
                if operation['status'] != 'running':
                    break
                time.sleep(3)
        operation = manager.get(args.application)
        if operation['status'] != 'needs_attention':
            raise AssertionError('AWS 실패 후 생성 작업이 확인 필요로 전환되지 않았습니다.')
        state['stage'] = 'create_failed'
        save(journal, state)
        print('PASS: accepted create ended in needs_attention without auto retry', flush=True)

        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            stacks = _aws(provisioner, ['cloudformation', 'describe-stacks',
                                        '--stack-name', request.stack_name]).get('Stacks', [])
            stack = stacks[0] if len(stacks) == 1 else {}
            stack_id = stack.get('StackId')
            if stack.get('StackStatus') == 'ROLLBACK_COMPLETE':
                break
            time.sleep(3)
        if not stack_id:
            raise AssertionError('실패 시험용 스택 ARN을 확인하지 못했습니다.')
        state.update(stage='rollback_complete', stack_id=stack_id)
        save(journal, state)
        inspect_rolled_back(provisioner, stack_id)
        result = manager.reconcile(args.application)
        if (result['status'] != 'needs_attention'
                or 'ROLLBACK_COMPLETE' not in result['message']):
            raise AssertionError('롤백 상태를 사용자 작업 기록에서 확인하지 못했습니다.')
        print('PASS: owned rollback and read-only user reconciliation confirmed', flush=True)
        if args.browser_cleanup:
            state['stage'] = 'browser_cleanup_started'
            save(journal, state)
            manager = browser_cleanup(state_dir, args.application, settings, network)
        else:
            cleanup = manager.cleanup_plan(args.application)
            if (cleanup['stack_id'] != stack_id
                    or cleanup['stack_status'] != 'ROLLBACK_COMPLETE'):
                raise AssertionError('제품 정리 계획의 실패 스택이 예상과 다릅니다.')
            state['stage'] = 'product_cleanup_planned'
            save(journal, state)
            started = manager.cleanup_start(args.application, cleanup['plan_id'], stack_id)
            if started['status'] != 'recovering':
                raise AssertionError('제품 실패 스택 정리 요청이 기록되지 않았습니다.')
            state['stage'] = 'product_cleanup_recorded'
            save(journal, state)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            operation = manager.get(args.application)
            if operation['status'] != 'recovering':
                break
            time.sleep(3)
        operation = manager.get(args.application)
        if operation['status'] != 'failed_cleaned':
            raise AssertionError('제품 실패 스택 정리가 완료되지 않았습니다: '
                                 + operation['message'])
        stack_cleaned = True
        state['stage'] = 'product_cleanup_verified'
        save(journal, state)
        if not list((manager.archive).glob(args.application + '-*.json')):
            raise AssertionError('실패한 생성 시도의 로컬 보존 기록이 없습니다.')
        if args.browser_cleanup:
            planned = manager.plans.get(args.application)
            if not planned or planned['result']['stack_name'] != request.stack_name:
                raise AssertionError('브라우저의 같은 앱 ID 재계획을 확인하지 못했습니다.')
        else:
            retry = manager.plan(request)
            if retry['stack_name'] != request.stack_name:
                raise AssertionError('같은 앱 ID의 새 생성 계획을 확인하지 못했습니다.')
        product_verified = True
        state['stage'] = 'retry_plan_verified'
        save(journal, state)
        print('PASS: product cleanup archived the failed attempt and reopened same-ID plan', flush=True)
    finally:
        if stack_id and created_network and not stack_cleaned:
            try:
                inspect_rolled_back(provisioner, stack_id)
                provisioner.adapter.aws(['cloudformation', 'update-termination-protection',
                    '--no-enable-termination-protection', '--stack-name', stack_id],
                    private=True, quiet=True)
                provisioner.adapter.aws(['cloudformation', 'delete-stack',
                    '--stack-name', stack_id], private=True, quiet=True)
                provisioner.adapter.aws(['cloudformation', 'wait', 'stack-delete-complete',
                    '--stack-name', stack_id], timeout=900, private=True, quiet=True)
                stack_cleaned = True
                print('PASS: rollback stack deleted', flush=True)
            except Exception as exc:
                print('롤백 스택 정리 확인 필요:', type(exc).__name__, str(exc), flush=True)
        if created_network and not stack_id and state['stage'] == 'network_created':
            try:
                if provisioner is None:
                    raise AwsConfigurationError('DB 생성 전 상태를 재확인할 수 없습니다.')
                retire_before_create(network_provisioner, created_network,
                                     provisioner, manager)
                network_cleaned = True
                precreate_cleaned = True
                print('PASS: no DB creation recorded; temporary network retired', flush=True)
            except Exception as exc:
                print('생성 전 시험용 네트워크 정리 확인 필요:', type(exc).__name__, str(exc), flush=True)
        if created_network and stack_cleaned:
            try:
                retire_probe(network_provisioner, created_network['stack_id'],
                             created_network['service_security_group'])
                network_cleaned = True
                print('PASS: temporary network retired', flush=True)
            except Exception as exc:
                print('시험용 네트워크 정리 확인 필요:', type(exc).__name__, str(exc), flush=True)
        state.update(status='succeeded' if stack_cleaned and network_cleaned and product_verified
                     else 'failed_preflight' if precreate_cleaned else 'needs_attention',
                     stage='cleaned' if stack_cleaned and network_cleaned and product_verified
                     else 'preflight_failed_network_retired' if precreate_cleaned else state['stage'])
        save(journal, state)
        print('Local rollback drill:', state_dir.resolve(), flush=True)
    if not stack_cleaned or not network_cleaned or not product_verified:
        raise RuntimeError('제품 실패 스택 정리·재계획 또는 임시 네트워크 정리가 완료되지 않았습니다.')
    return {'application_id': args.application, 'status': state['status'],
            'stage': state['stage']}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Fail and reconcile a disposable AWS RDS create operation')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--browser-cleanup', action='store_true',
                        help='Use real Chrome and product HTTP controls for failed stack cleanup')
    parser.add_argument('--application', default='dbdrill-' + secrets.token_hex(4))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args(argv)
    if args.browser_cleanup and not args.apply:
        parser.error('--browser-cleanup requires --apply')
    print(json.dumps(run(args), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
