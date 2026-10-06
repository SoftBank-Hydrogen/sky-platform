"""Explicit, journaled retirement of an owned PostgreSQL database.

This path never removes a database without a verified, available final snapshot.
An interrupted operation requires manual inspection; it is never retried on startup.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.postgres import AwsPostgresProvisioner, PostgresRequest
from adapters.aws.postgres_snapshot import create_snapshot, inspect_snapshot


def _response(provisioner: AwsPostgresProvisioner, args: list[str]) -> dict:
    return json.loads(provisioner.adapter.aws(args, private=True, quiet=True))


def _definition_uses_secret(provisioner: AwsPostgresProvisioner,
                            definition_arn: str, secret_arn: str) -> bool:
    definition = _response(provisioner, ['ecs', 'describe-task-definition',
        '--task-definition', definition_arn]).get('taskDefinition', {})
    if definition.get('taskDefinitionArn') != definition_arn:
        raise AwsConfigurationError('ECS 태스크 정의 식별자를 확인하지 못했습니다.')
    containers = definition.get('containerDefinitions')
    if not isinstance(containers, list):
        raise AwsConfigurationError('ECS 컨테이너 목록을 확인하지 못했습니다.')
    for container in containers:
        refs = container.get('secrets', []) if isinstance(container, dict) else None
        if not isinstance(refs, list):
            raise AwsConfigurationError('ECS 비밀 참조를 확인하지 못했습니다.')
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get('valueFrom'), str):
                raise AwsConfigurationError('ECS 비밀 참조를 확인하지 못했습니다.')
            if ref['valueFrom'].startswith(secret_arn + ':') or ref['valueFrom'] == secret_arn:
                return True
    return False


def active_database_users(provisioner: AwsPostgresProvisioner, secret_arn: str) -> list[str]:
    """Fail closed on pagination, deployments, or an uninspectable ECS service/task."""
    services = _response(provisioner, ['ecs', 'list-services', '--cluster', 'default'])
    arns = services.get('serviceArns')
    if services.get('nextToken') or not isinstance(arns, list):
        raise AwsConfigurationError('ECS 서비스 목록을 완전히 확인하지 못했습니다.')
    users = []
    for arn in arns:
        if not isinstance(arn, str):
            raise AwsConfigurationError('ECS 서비스 ARN이 올바르지 않습니다.')
        service = _response(provisioner, ['ecs', 'describe-express-gateway-service',
                                          '--service-arn', arn]).get('service', {})
        if service.get('serviceArn') != arn:
            raise AwsConfigurationError('ECS 서비스 식별자를 확인하지 못했습니다.')
        if service.get('status', {}).get('statusCode') == 'INACTIVE':
            continue
        if service.get('currentDeployment'):
            raise AwsConfigurationError('진행 중인 ECS 배포가 있어 DB 폐기를 중단합니다.')
        configs = service.get('activeConfigurations')
        if not isinstance(configs, list) or not configs:
            raise AwsConfigurationError('ECS 서비스 구성을 확인하지 못했습니다.')
        for config in configs:
            definition_arn = config.get('taskDefinitionArn') if isinstance(config, dict) else None
            if not isinstance(definition_arn, str):
                raise AwsConfigurationError('ECS 태스크 정의를 확인하지 못했습니다.')
            if _definition_uses_secret(provisioner, definition_arn, secret_arn):
                users.append(arn)
    tasks = _response(provisioner, ['ecs', 'list-tasks', '--cluster', 'default',
                                    '--desired-status', 'RUNNING'])
    task_arns = tasks.get('taskArns')
    if tasks.get('nextToken') or not isinstance(task_arns, list):
        raise AwsConfigurationError('실행 중인 ECS 작업 목록을 완전히 확인하지 못했습니다.')
    if task_arns:
        described = _response(provisioner, ['ecs', 'describe-tasks', '--cluster', 'default',
                                            '--tasks', *task_arns])
        if described.get('failures') or len(described.get('tasks', [])) != len(task_arns):
            raise AwsConfigurationError('실행 중인 ECS 작업을 완전히 확인하지 못했습니다.')
        for task in described['tasks']:
            if task.get('taskArn') not in task_arns:
                raise AwsConfigurationError('실행 중인 ECS 작업 식별자를 확인하지 못했습니다.')
            definition_arn = task.get('taskDefinitionArn')
            if not isinstance(definition_arn, str):
                raise AwsConfigurationError('실행 중인 ECS 태스크 정의를 확인하지 못했습니다.')
            if _definition_uses_secret(provisioner, definition_arn, secret_arn):
                users.append(task['taskArn'])
    return users


def plan(request: PostgresRequest) -> dict:
    """Read-only ownership, workload, and protection check."""
    provisioner = AwsPostgresProvisioner(request)
    database = provisioner.inspect_current()
    tags = _response(provisioner, ['rds', 'list-tags-for-resource',
                                   '--resource-name', database['database_arn']]).get('TagList')
    if not isinstance(tags, list) or any(not isinstance(item, dict) for item in tags):
        raise AwsConfigurationError('DB 소유 태그를 확인하지 못했습니다.')
    owned = {item.get('Key'): item.get('Value') for item in tags}
    if owned.get('sky-managed') != 'true' or owned.get('sky-app') != request.application_id:
        raise AwsConfigurationError('DB 소유 태그가 예상과 다릅니다.')
    users = active_database_users(provisioner, database['secret_arn'])
    if users:
        raise AwsConfigurationError('DB 비밀을 사용하는 ECS 서비스·작업이 남아 있습니다: ' + ', '.join(users))
    stacks = _response(provisioner, ['cloudformation', 'describe-stacks',
                                     '--stack-name', database['stack_id']]).get('Stacks')
    stack = stacks[0] if isinstance(stacks, list) and len(stacks) == 1 else {}
    if (stack.get('StackId') != database['stack_id']
            or stack.get('StackStatus') != 'CREATE_COMPLETE'
            or stack.get('EnableTerminationProtection') is not True
            or database['deletion_protection'] is not True
            or database['retained_on_stack_delete'] is not True):
        raise AwsConfigurationError('DB/스택의 생성 완료·보호·보존 상태가 예상과 다릅니다.')
    return {'application_id': request.application_id, 'account': request.account,
            'region': request.region, 'database_id': database['database_id'],
            'database_arn': database['database_arn'], 'stack_id': database['stack_id'],
            'active_db_users': 0, 'deletion_protection': True,
            'final_snapshot_required': True,
            'retained_resources': ['all manual snapshots (including final)', 'app service network']}


def _save(path: Path, state: dict, *, new: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if new:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(state, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.retire-',
                                     delete=False, encoding='utf-8') as file:
        temporary = Path(file.name)
        os.chmod(temporary, 0o600)
        json.dump(state, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def execute_recorded(request: PostgresRequest, state_file: Path, state: dict) -> dict:
    """Run one already-journaled retirement; caller must never invoke it twice."""
    expected = {key: state[key] for key in ('application_id', 'account', 'region',
        'database_id', 'database_arn', 'stack_id', 'active_db_users',
        'deletion_protection', 'final_snapshot_required', 'retained_resources')}
    snapshot_id = state['snapshot_id']
    provisioner = AwsPostgresProvisioner(request)
    adapter = provisioner.adapter

    def stage(value: str) -> None:
        state['stage'] = value
        _save(state_file, state)

    try:
        stage('creating_final_snapshot')
        created = create_snapshot(request.application_id, snapshot_id, adapter.settings)
        if created['snapshot_id'] != snapshot_id:
            raise AwsConfigurationError('최종 스냅샷 생성 응답이 예상과 다릅니다.')
        adapter.aws(['rds', 'wait', 'db-snapshot-available', '--db-snapshot-identifier', snapshot_id],
                    timeout=7200, private=True, quiet=True)
        snapshot = inspect_snapshot(request.application_id, snapshot_id, adapter.settings)
        if snapshot['status'] != 'available' or snapshot['encrypted'] is not True:
            raise AwsConfigurationError('암호화된 최종 스냅샷이 사용 가능 상태가 아닙니다.')
        stage('final_snapshot_verified')
        if plan(request) != expected:
            raise AwsConfigurationError('스냅샷 생성 중 DB 상태가 바뀌었습니다. 폐기를 중단합니다.')
        secret_arn = provisioner.inspect_current()['secret_arn']
        stage('removing_db_protection')
        modified = _response(provisioner, ['rds', 'modify-db-instance',
            '--db-instance-identifier', request.database_id,
            '--no-deletion-protection', '--apply-immediately']).get('DBInstance', {})
        if (modified.get('DBInstanceIdentifier') != request.database_id
                or modified.get('DBInstanceArn') != expected['database_arn']
                or modified.get('DeletionProtection') is not False):
            raise AwsConfigurationError('DB 삭제 보호 변경 응답이 불확실합니다.')
        adapter.aws(['rds', 'wait', 'db-instance-available',
                     '--db-instance-identifier', request.database_id],
                    timeout=3600, private=True, quiet=True)
        current = _response(provisioner, ['rds', 'describe-db-instances',
            '--db-instance-identifier', request.database_id]).get('DBInstances', [])
        if (len(current) != 1 or current[0].get('DBInstanceArn') != expected['database_arn']
                or current[0].get('DBInstanceStatus') != 'available'
                or current[0].get('DeletionProtection') is not False):
            raise AwsConfigurationError('DB 삭제 보호 해제를 확인하지 못했습니다.')
        if active_database_users(provisioner, secret_arn):
            raise AwsConfigurationError('DB를 사용하는 ECS 작업이 다시 나타났습니다.')
        if inspect_snapshot(request.application_id, snapshot_id, adapter.settings)['status'] != 'available':
            raise AwsConfigurationError('최종 스냅샷 상태가 변경됐습니다.')
        stage('deleting_database')
        deleted = _response(provisioner, ['rds', 'delete-db-instance',
            '--db-instance-identifier', request.database_id, '--skip-final-snapshot',
            '--delete-automated-backups']).get('DBInstance', {})
        if (deleted.get('DBInstanceArn') != expected['database_arn']
                or deleted.get('DBInstanceStatus') != 'deleting'):
            raise AwsConfigurationError('DB 삭제 요청 응답이 불확실합니다.')
        adapter.aws(['rds', 'wait', 'db-instance-deleted',
                     '--db-instance-identifier', request.database_id],
                    timeout=3600, private=True, quiet=True)
        stage('database_deleted')
        if inspect_snapshot(request.application_id, snapshot_id, adapter.settings)['status'] != 'available':
            raise AwsConfigurationError('DB 삭제 후 최종 스냅샷을 확인하지 못했습니다.')
        stage('removing_stack_protection')
        adapter.aws(['cloudformation', 'update-termination-protection',
                     '--no-enable-termination-protection', '--stack-name', expected['stack_id']],
                    private=True, quiet=True)
        stacks = _response(provisioner, ['cloudformation', 'describe-stacks',
                                         '--stack-name', expected['stack_id']]).get('Stacks', [])
        if (len(stacks) != 1 or stacks[0].get('StackId') != expected['stack_id']
                or stacks[0].get('EnableTerminationProtection') is not False):
            raise AwsConfigurationError('스택 종료 보호 해제를 확인하지 못했습니다.')
        stage('deleting_stack')
        adapter.aws(['cloudformation', 'delete-stack', '--stack-name', expected['stack_id']],
                    private=True, quiet=True)
        adapter.aws(['cloudformation', 'wait', 'stack-delete-complete',
                     '--stack-name', expected['stack_id']], timeout=3600, private=True, quiet=True)
        if inspect_snapshot(request.application_id, snapshot_id, adapter.settings)['status'] != 'available':
            raise AwsConfigurationError('스택 삭제 후 최종 스냅샷을 확인하지 못했습니다.')
        stage('stack_deleted')
        state['status'] = 'succeeded'
        _save(state_file, state)
        return state
    except Exception as exc:
        if state['stage'] == 'removing_db_protection':
            try:
                restored = _response(provisioner, ['rds', 'modify-db-instance',
                    '--db-instance-identifier', request.database_id,
                    '--deletion-protection', '--apply-immediately']).get('DBInstance', {})
                if (restored.get('DBInstanceArn') != expected['database_arn']
                        or restored.get('DeletionProtection') is not True):
                    raise AwsConfigurationError('DB 삭제 보호 복구 응답이 불확실합니다.')
                adapter.aws(['rds', 'wait', 'db-instance-available',
                             '--db-instance-identifier', request.database_id],
                            timeout=3600, private=True, quiet=True)
                if provisioner.inspect_current()['deletion_protection'] is not True:
                    raise AwsConfigurationError('DB 삭제 보호 복구 상태가 불확실합니다.')
                stage('db_protection_restored')
            except Exception as restore_exc:
                state['protection_restore_error'] = str(restore_exc)
        state['status'] = 'needs_attention'
        state['message'] = str(exc)
        _save(state_file, state)
        raise


def apply(request: PostgresRequest, state_file: Path, *, confirm_database_id: str) -> dict:
    if confirm_database_id != request.database_id:
        raise ValueError('삭제 대상 DB ID를 정확히 확인해야 합니다.')
    expected = plan(request)
    state = {**expected,
             'snapshot_id': f'sky-{request.application_id}-final-{secrets.token_hex(6)}',
             'status': 'running', 'stage': 'planned',
             'created_at': datetime.now(timezone.utc).isoformat()}
    _save(state_file, state, new=True)
    return execute_recorded(request, state_file, state)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Retire owned RDS after an available final snapshot')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--state-file', type=Path)
    parser.add_argument('--confirm-database-id')
    args = parser.parse_args(argv)
    request = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
    try:
        if args.apply:
            if not args.state_file or not args.confirm_database_id:
                raise ValueError('--apply에는 --state-file과 --confirm-database-id가 필요합니다.')
            result = apply(request, args.state_file, confirm_database_id=args.confirm_database_id)
        else:
            result = plan(request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, AwsConfigurationError) as exc:
        parser.exit(2, f'PostgreSQL 폐기 점검 실패: {exc}\n')


if __name__ == '__main__':
    main()
