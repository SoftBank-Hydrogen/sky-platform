"""Opt-in cleanup of a disposable dbdrill-* PostgreSQL stack created by UI smoke."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from sky_platform.runtime.aws import AwsConfigurationError, AwsSettings
from sky_platform.runtime.postgres import AwsPostgresProvisioner, PostgresRequest
from tests.smoke_aws_restore_marker_source import save, save_new


def _response(provisioner: AwsPostgresProvisioner, args: list[str]) -> dict:
    return json.loads(provisioner.adapter.aws(args, private=True, quiet=True))


def _definition_uses_secret(provisioner: AwsPostgresProvisioner,
                            definition_arn: str, secret_arn: str) -> bool:
    definition = _response(provisioner, ['ecs', 'describe-task-definition',
        '--task-definition', definition_arn]).get('taskDefinition', {})
    if definition.get('taskDefinitionArn') != definition_arn:
        raise AwsConfigurationError('ECS 태스크 정의 소유 식별자를 확인하지 못했습니다.')
    containers = definition.get('containerDefinitions')
    if not isinstance(containers, list):
        raise AwsConfigurationError('ECS 컨테이너 비밀 목록을 확인하지 못했습니다.')
    for container in containers:
        secrets = container.get('secrets', [])
        if not isinstance(secrets, list):
            raise AwsConfigurationError('ECS 컨테이너 비밀 목록을 확인하지 못했습니다.')
        for item in secrets:
            if not isinstance(item, dict) or not isinstance(item.get('valueFrom'), str):
                raise AwsConfigurationError('ECS 컨테이너 비밀 참조를 확인하지 못했습니다.')
            if item['valueFrom'].startswith(secret_arn):
                return True
    return False


def _db_users(provisioner: AwsPostgresProvisioner, secret_arn: str) -> list[str]:
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
            raise AwsConfigurationError('ECS 서비스 소유 식별자를 확인하지 못했습니다.')
        if service.get('status', {}).get('statusCode') == 'INACTIVE':
            continue
        if service.get('currentDeployment'):
            raise AwsConfigurationError('진행 중인 ECS 서비스 배포가 있어 DB 정리를 중단합니다.')
        configs = service.get('activeConfigurations')
        if not isinstance(configs, list) or not configs:
            raise AwsConfigurationError('ECS 서비스 구성 목록을 확인하지 못했습니다.')
        for config in configs:
            definition_arn = config.get('taskDefinitionArn')
            if not isinstance(definition_arn, str):
                raise AwsConfigurationError('ECS 서비스 태스크 정의를 확인하지 못했습니다.')
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
    request.validate()
    if not re.fullmatch(r'dbdrill-[a-f0-9]{8}', request.application_id):
        raise ValueError('이 정리 도구는 dbdrill-<8 hex> 임시 앱에만 사용할 수 있습니다.')
    provisioner = AwsPostgresProvisioner(request)
    database = provisioner.inspect_current()
    tags = _response(provisioner, ['rds', 'list-tags-for-resource',
                                   '--resource-name', database['database_arn']]).get('TagList')
    owned = {item.get('Key'): item.get('Value') for item in tags or []}
    if (not isinstance(tags, list) or owned.get('sky-managed') != 'true'
            or owned.get('sky-app') != request.application_id):
        raise AwsConfigurationError('임시 DB 소유 태그가 예상과 다릅니다.')
    snapshots = _response(provisioner, ['rds', 'describe-db-snapshots',
        '--db-instance-identifier', request.database_id, '--snapshot-type', 'manual'])
    if snapshots.get('Marker') or snapshots.get('DBSnapshots') != []:
        raise AwsConfigurationError('수동 스냅샷이 있어 임시 DB를 정리하지 않습니다.')
    users = _db_users(provisioner, database['secret_arn'])
    if users:
        raise AwsConfigurationError('DB 비밀을 사용하는 ECS 서비스가 남아 있습니다.')
    stacks = _response(provisioner, ['cloudformation', 'describe-stacks',
                                     '--stack-name', database['stack_id']]).get('Stacks')
    stack = stacks[0] if isinstance(stacks, list) and len(stacks) == 1 else {}
    if (stack.get('StackId') != database['stack_id']
            or stack.get('StackStatus') != 'CREATE_COMPLETE'
            or stack.get('EnableTerminationProtection') is not True):
        raise AwsConfigurationError('임시 DB 스택의 종료 보호·상태가 예상과 다릅니다.')
    return {'application_id': request.application_id,
            'account': request.account, 'region': request.region,
            'database_id': database['database_id'],
            'database_arn': database['database_arn'],
            'stack_id': database['stack_id'],
            'snapshot_count': 0, 'active_db_services': 0,
            'deletion_protection': True}


def apply(request: PostgresRequest, state_file: Path) -> dict:
    expected = plan(request)
    state = {**expected, 'status': 'running', 'stage': 'planned'}
    save_new(state_file, state)
    provisioner = AwsPostgresProvisioner(request)
    adapter = provisioner.adapter
    try:
        state['stage'] = 'removing_db_protection'
        save(state_file, state)
        modified = _response(provisioner, ['rds', 'modify-db-instance',
            '--db-instance-identifier', request.database_id,
            '--no-deletion-protection', '--apply-immediately']).get('DBInstance', {})
        if (modified.get('DBInstanceIdentifier') != request.database_id
                or modified.get('DBInstanceArn') != expected['database_arn']
                or modified.get('DeletionProtection') is not False):
            raise AwsConfigurationError('임시 DB 삭제 보호 변경 응답이 불확실합니다.')
        adapter.aws(['rds', 'wait', 'db-instance-available',
                     '--db-instance-identifier', request.database_id],
                    timeout=3600, private=True, quiet=True)
        current = _response(provisioner, ['rds', 'describe-db-instances',
            '--db-instance-identifier', request.database_id]).get('DBInstances', [])
        if (len(current) != 1 or current[0].get('DBInstanceArn') != expected['database_arn']
                or current[0].get('DBInstanceStatus') != 'available'
                or current[0].get('DeletionProtection') is not False):
            raise AwsConfigurationError('임시 DB 삭제 보호 해제를 재확인하지 못했습니다.')
        state['stage'] = 'db_protection_removed'
        save(state_file, state)
        state['stage'] = 'deleting_db'
        save(state_file, state)
        deleted = _response(provisioner, ['rds', 'delete-db-instance',
            '--db-instance-identifier', request.database_id, '--skip-final-snapshot',
            '--delete-automated-backups']).get('DBInstance', {})
        if (deleted.get('DBInstanceArn') != expected['database_arn']
                or deleted.get('DBInstanceStatus') != 'deleting'):
            raise AwsConfigurationError('임시 DB 삭제 요청 응답이 불확실합니다.')
        adapter.aws(['rds', 'wait', 'db-instance-deleted',
                     '--db-instance-identifier', request.database_id],
                    timeout=3600, private=True, quiet=True)
        state['stage'] = 'db_deleted'
        save(state_file, state)
        state['stage'] = 'deleting_stack'
        save(state_file, state)
        adapter.aws(['cloudformation', 'update-termination-protection',
                     '--no-enable-termination-protection', '--stack-name', expected['stack_id']],
                    private=True, quiet=True)
        stacks = _response(provisioner, ['cloudformation', 'describe-stacks',
                                         '--stack-name', expected['stack_id']]).get('Stacks', [])
        if (len(stacks) != 1 or stacks[0].get('StackId') != expected['stack_id']
                or stacks[0].get('EnableTerminationProtection') is not False):
            raise AwsConfigurationError('임시 DB 스택 종료 보호 해제를 확인하지 못했습니다.')
        adapter.aws(['cloudformation', 'delete-stack', '--stack-name', expected['stack_id']],
                    private=True, quiet=True)
        adapter.aws(['cloudformation', 'wait', 'stack-delete-complete',
                     '--stack-name', expected['stack_id']], timeout=3600,
                    private=True, quiet=True)
        state['stage'] = 'stack_deleted'
        state['status'] = 'succeeded'
        save(state_file, state)
        return {key: state[key] for key in ('application_id', 'database_id',
                                             'stack_id', 'stage', 'status')}
    except Exception:
        state['status'] = 'needs_attention'
        save(state_file, state)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Dispose only dbdrill-* RDS smoke stacks')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    args = parser.parse_args(argv)
    request = PostgresRequest(args.application, args.account, args.region,
                              args.vpc_id, tuple(args.subnet_id), args.service_security_group)
    result = apply(request, args.state_file) if args.apply else plan(request)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
