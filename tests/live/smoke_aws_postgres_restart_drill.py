"""Opt-in live crash/restart drill for a disposable PostgreSQL create operation."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from sky_platform.deployment.aws import AwsSettings
from sky_platform.deployment.aws_network import (AwsServiceNetworkProvisioner,
                                   ServiceNetworkRequest, discover_default_network)
from sky_platform.database.postgres import AwsPostgresProvisioner, PostgresRequest
from sky_platform.database.postgres_operations import PostgresOperations
from tests.live.smoke_aws_network import retire_probe
from tests.live.smoke_aws_postgres_cleanup import apply as cleanup_database
from tests.live.smoke_aws_restore_marker_source import save, save_new


def worker(request: PostgresRequest, root: Path) -> None:
    settings = AwsSettings(request.region, expected_account=request.account,
                           account_pin_required=True,
                           service_security_group=request.service_security_group)
    operations = PostgresOperations(root, settings)
    planned = operations.plan(request)
    started = operations.start(request.application_id, planned['plan_id'])
    print(json.dumps({'status': started['status'],
                      'application_id': started['application_id']}), flush=True)
    # The parent kills this process after AWS accepts the stack, before create completes.
    while True:
        time.sleep(1)


def stop_worker(child: subprocess.Popen) -> None:
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=20)


def run(args) -> dict:
    if not re.fullmatch(r'dbdrill-[a-f0-9]{8}', args.application):
        raise ValueError('앱 ID는 새 dbdrill-<8 hex>여야 합니다.')
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    network = discover_default_network(settings)
    network_request = ServiceNetworkRequest(args.application, args.account,
                                            args.region, network['vpc_id'])
    network_provisioner = AwsServiceNetworkProvisioner(network_request)
    network_provisioner.preflight()
    state_dir = Path('.sky') / 'postgres-restart-drills' / args.application
    if not args.apply:
        return {'application_id': args.application, 'mode': 'read_only',
                'network_stack': network_request.stack_name}
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    journal = state_dir / 'journal.json'
    state = {'application_id': args.application, 'account': args.account,
             'region': args.region, 'vpc_id': network['vpc_id'],
             'subnet_ids': network['subnet_ids'],
             'database_id': 'sky-' + args.application,
             'database_stack_name': 'sky-db-' + args.application,
             'stage': 'planned', 'status': 'running'}
    save_new(journal, state)
    child = None
    created_network = None
    database_cleaned = False
    network_cleaned = False
    request = None
    try:
        created_network = network_provisioner.create()
        state.update(stage='network_created', network_stack_id=created_network['stack_id'],
                     service_security_group=created_network['service_security_group'])
        save(journal, state)
        request = PostgresRequest(args.application, args.account, args.region,
            network['vpc_id'], tuple(network['subnet_ids']),
            created_network['service_security_group'])
        provisioner = AwsPostgresProvisioner(request)
        stderr = (state_dir / 'worker.log').open('w')
        try:
            child = subprocess.Popen([sys.executable, '-m',
                'tests.live.smoke_aws_postgres_restart_drill', '--worker', '--apply',
                '--application', args.application, '--account', args.account,
                '--region', args.region, '--vpc-id', network['vpc_id'],
                *[part for subnet in network['subnet_ids']
                  for part in ('--subnet-id', subnet)],
                '--service-security-group', request.service_security_group,
                '--state-dir', str(state_dir)],
                stdout=subprocess.PIPE, stderr=stderr, text=True,
                start_new_session=True)
            ready, _, _ = select.select([child.stdout], [], [], 180)
            if not ready:
                raise RuntimeError('RDS 생성 작업 접수 응답을 받지 못했습니다.')
            line = child.stdout.readline()
            started = json.loads(line)
            if (started.get('status') != 'running'
                    or started.get('application_id') != args.application):
                raise RuntimeError('RDS 생성 작업이 실행 중 상태로 기록되지 않았습니다.')
        finally:
            stderr.close()
        state['stage'] = 'create_recorded'
        save(journal, state)
        print('PASS: create operation persisted before stack submission', flush=True)
        deadline = time.monotonic() + 180
        accepted = None
        while time.monotonic() < deadline:
            try:
                stacks = json.loads(provisioner.adapter.aws(['cloudformation',
                    'describe-stacks', '--stack-name', request.stack_name],
                    private=True, quiet=True)).get('Stacks', [])
                stack = stacks[0] if len(stacks) == 1 else {}
                tags = {item.get('Key'): item.get('Value') for item in stack.get('Tags', [])}
                prefix = f'arn:aws:cloudformation:{args.region}:{args.account}:stack/{request.stack_name}/'
                if (stack.get('StackId', '').startswith(prefix)
                        and tags.get('sky-managed') == 'true'
                        and tags.get('sky-app') == args.application
                        and stack.get('StackStatus') == 'CREATE_IN_PROGRESS'):
                    accepted = stack['StackId']
                    break
            except Exception:
                pass
            if child.poll() is not None:
                raise RuntimeError('AWS 접수 전에 생성 작업 프로세스가 종료됐습니다.')
            time.sleep(3)
        if not accepted:
            raise RuntimeError('AWS 스택 생성 요청 접수를 확인하지 못했습니다.')
        state.update(stage='stack_accepted', stack_id=accepted)
        save(journal, state)
        stop_worker(child)
        child = None
        print('PASS: creator process stopped after AWS accepted stack', flush=True)
        manager = PostgresOperations(state_dir / 'database-operations',
            AwsSettings(args.region, expected_account=args.account,
                        account_pin_required=True,
                        service_security_group=request.service_security_group))
        if manager.get(args.application)['status'] != 'needs_attention':
            raise AssertionError('재시작한 작업 기록이 자동 재시도 없이 확인 필요 상태가 아닙니다.')
        state['stage'] = 'recovered_needs_attention'
        save(journal, state)
        print('PASS: restarted manager marked uncertain create for reconciliation', flush=True)
        provisioner.adapter.aws(['cloudformation', 'wait', 'stack-create-complete',
                                 '--stack-name', accepted], timeout=3600,
                                private=True, quiet=True)
        completed = None
        for _ in range(12):
            completed = manager.reconcile(args.application)
            if completed['status'] == 'succeeded':
                break
            time.sleep(5)
        if (not completed or completed['status'] != 'succeeded'
                or completed['database_id'] != request.database_id):
            raise AssertionError('재시작 후 AWS 생성 완료 재확인이 실패했습니다.')
        provisioner.inspect_current()
        state['stage'] = 'reconciled'
        save(journal, state)
        print('PASS: restarted manager reconciled the owned AWS database', flush=True)
    finally:
        if child is not None:
            stop_worker(child)
        if request is not None:
            try:
                provisioner.inspect_current()
                result = cleanup_database(request, state_dir / 'database-cleanup.json')
                database_cleaned = result['status'] == 'succeeded'
                print('Temporary RDS cleanup:', result['status'], flush=True)
            except Exception as exc:
                print('임시 DB 정리 확인 필요:', type(exc).__name__, str(exc), flush=True)
        if created_network is not None and database_cleaned:
            try:
                retire_probe(network_provisioner, created_network['stack_id'],
                             created_network['service_security_group'])
                network_cleaned = True
                print('PASS: temporary network retired', flush=True)
            except Exception as exc:
                print('임시 네트워크 정리 확인 필요:', type(exc).__name__, str(exc), flush=True)
        state.update(status='succeeded' if database_cleaned and network_cleaned
                     else 'needs_attention', stage='cleaned' if database_cleaned and network_cleaned
                     else state['stage'])
        save(journal, state)
        print('Local restart drill:', state_dir.resolve(), flush=True)
    if not database_cleaned or not network_cleaned:
        raise RuntimeError('임시 RDS 또는 네트워크 정리가 완료되지 않았습니다.')
    return {'application_id': args.application, 'status': state['status'],
            'stage': state['stage']}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Crash and reconcile a disposable AWS RDS creation')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--application', default='dbdrill-' + secrets.token_hex(4))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id')
    parser.add_argument('--subnet-id', action='append')
    parser.add_argument('--service-security-group')
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args(argv)
    if args.worker:
        if not args.apply:
            parser.error('--worker requires --apply')
        request = PostgresRequest(args.application, args.account, args.region,
                                  args.vpc_id, tuple(args.subnet_id), args.service_security_group)
        worker(request, args.state_dir / 'database-operations')
    else:
        print(json.dumps(run(args), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
