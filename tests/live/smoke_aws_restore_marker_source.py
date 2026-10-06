"""Opt-in live source marker and owned snapshot for a restore data drill."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from application.deployment_core import analyze
from adapters.database.migrations import collect_sql_migrations
from adapters.aws.postgres import AwsPostgresProvisioner, PostgresRequest
from adapters.aws.postgres_snapshot import create_snapshot, inspect_snapshot, plan_snapshot
from tests.live.smoke_aws_postgres import probe


def save_new(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    sync_dir(path.parent)


def sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path: Path, state: dict) -> None:
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.marker-',
                                     delete=False, encoding='utf-8') as file:
        temp = Path(file.name)
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def run(args) -> dict:
    request = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
    request.validate()
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    database = AwsPostgresProvisioner(request).inspect_current()
    snapshot_plan = plan_snapshot(args.application, args.snapshot_id, settings)
    if snapshot_plan['database_id'] != database['database_id']:
        raise AwsConfigurationError('표식 스냅샷의 원본 DB가 다릅니다.')
    if not args.apply:
        return {'database_id': database['database_id'],
                'snapshot_id': args.snapshot_id, 'mode': 'read_only'}
    marker_id = uuid.uuid4().hex
    attempt_id = uuid.uuid4().hex[:16] + '-a1'
    key = secrets.token_urlsafe(32)
    state = {'application_id': args.application, 'account': args.account,
             'region': args.region, 'snapshot_id': args.snapshot_id,
             'marker_id': marker_id, 'attempt_id': attempt_id,
             'service_arn': (f'arn:aws:ecs:{args.region}:{args.account}:service/default/'
                             f'sky-{attempt_id}'),
             'image_tag': attempt_id,
             'probe_key': key, 'stage': 'planned', 'status': 'running'}
    save_new(args.state_file, state)
    adapter = AwsExpressAdapter(lambda stage, message: print(f'[{stage}] {message}', flush=True),
                                settings)
    source = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apps' / 'postgres-probe-node'
    try:
        with tempfile.TemporaryDirectory(prefix='sky-restore-marker-') as directory:
            project = Path(directory) / 'app'
            shutil.copytree(source, project)
            deployment = replace(analyze(project), target='aws-ecs-express',
                                 health_path='/health', required_env=['PROBE_KEY'])
            state['stage'] = 'deploying'
            save(args.state_file, state)
            result = adapter.deploy(project, deployment, attempt_id,
                                    {'PROBE_KEY': key}, postgres=request,
                                    migrations=collect_sql_migrations(project))
        state['service'] = {name: result[name] for name in (
            'account', 'region', 'target', 'service', 'service_arn', 'image',
            'owner_attempt', 'url')}
        state['stage'] = 'deployed'
        save(args.state_file, state)
        probe(result['url'], key, marker_id, 'POST')
        probe(result['url'], key, marker_id, 'GET')
        state['stage'] = 'marker_written'
        save(args.state_file, state)
        snapshot = create_snapshot(args.application, args.snapshot_id, settings)
        state['stage'] = 'snapshot_requested'
        state['snapshot_arn'] = snapshot['snapshot_arn']
        save(args.state_file, state)
        for _ in range(120):
            observed = inspect_snapshot(args.application, args.snapshot_id, settings)
            if observed['status'] == 'available':
                break
            if observed['status'] != 'creating':
                raise AwsConfigurationError('표식 스냅샷 상태가 예상과 다릅니다.')
            time.sleep(15)
        else:
            raise AwsConfigurationError('표식 스냅샷 완료 대기 시간이 초과됐습니다.')
        state['stage'] = 'snapshot_available'
        save(args.state_file, state)
        probe(result['url'], key, marker_id, 'DELETE')
        state['stage'] = 'source_marker_removed'
        save(args.state_file, state)
        adapter.retire(result, attempt_id)
        state['stage'] = 'source_service_retired'
        state['status'] = 'succeeded'
        state.pop('probe_key', None)
        save(args.state_file, state)
        return {name: state[name] for name in ('snapshot_id', 'snapshot_arn',
                                               'marker_id', 'status', 'stage')}
    except Exception:
        state['status'] = 'needs_attention'
        save(args.state_file, state)
        raise


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Write one source marker and snapshot it')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args), ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
