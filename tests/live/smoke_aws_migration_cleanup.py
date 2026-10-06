"""Opt-in live ECS/ECR migration-artifact cleanup drill without a database."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from deployment.aws_migrations import cleanup_interrupted_migration, inspect_migration_task
from database.postgres import PostgresRequest


def save(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.migration-cleanup-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(adapter: AwsExpressAdapter, args: list[str]) -> dict:
    return json.loads(adapter.aws(args, private=True, quiet=True))


def preflight(adapter: AwsExpressAdapter, request: PostgresRequest) -> dict:
    request.validate()
    if read(adapter, ['sts', 'get-caller-identity']).get('Account') != request.account:
        raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
    stacks = read(adapter, ['cloudformation', 'describe-stacks', '--stack-name', 'sky-core']).get('Stacks', [])
    if len(stacks) != 1 or stacks[0].get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise AwsConfigurationError('Sky 기반 스택이 준비되지 않았습니다.')
    outputs = {item.get('OutputKey'): item.get('OutputValue') for item in stacks[0].get('Outputs', [])}
    repository = f'{request.account}.dkr.ecr.{request.region}.amazonaws.com/sky-managed'
    role = outputs.get('ExecutionRoleArn', '')
    if (outputs.get('RepositoryUri') != repository
            or not re.fullmatch(f'arn:aws:iam::{request.account}:role/sky-core-ExecutionRole-[A-Za-z0-9]+', role)):
        raise AwsConfigurationError('기반 스택의 ECR 저장소·ECS 실행 역할이 예상과 다릅니다.')
    repositories = read(adapter, ['ecr', 'describe-repositories', '--repository-names', 'sky-managed']).get('repositories', [])
    if len(repositories) != 1 or repositories[0].get('repositoryUri') != repository:
        raise AwsConfigurationError('기존 ECR 저장소를 확인하지 못했습니다.')
    subnets = read(adapter, ['ec2', 'describe-subnets', '--subnet-ids', *request.subnet_ids]).get('Subnets', [])
    if (len(subnets) != len(request.subnet_ids)
            or {item.get('SubnetId') for item in subnets} != set(request.subnet_ids)
            or any(item.get('VpcId') != request.vpc_id or item.get('State') != 'available'
                   for item in subnets)):
        raise AwsConfigurationError('검증용 서브넷이 지정한 VPC와 일치하지 않습니다.')
    groups = read(adapter, ['ec2', 'describe-security-groups', '--group-ids',
                            request.service_security_group]).get('SecurityGroups', [])
    group_tags = {item.get('Key'): item.get('Value') for item in groups[0].get('Tags', [])} if len(groups) == 1 else {}
    if (len(groups) != 1 or groups[0].get('VpcId') != request.vpc_id
            or groups[0].get('GroupId') != request.service_security_group
            or group_tags.get('sky-managed') != 'true'
            or group_tags.get('sky-app') != request.application_id
            or groups[0].get('IpPermissions') != []):
        raise AwsConfigurationError('검증용 보안 그룹의 VPC·소유 태그·인바운드 규칙이 예상과 다릅니다.')
    return {'repository': repository, 'execution_role_arn': role}


def cleanup(adapter: AwsExpressAdapter, request: PostgresRequest,
            path: Path, record: dict) -> dict:
    required = ('task_arn', 'task_definition_arn', 'image', 'image_digest')
    if any(not record.get(key) for key in required):
        raise ValueError('태스크·정의·이미지 digest 기록이 부족해 자동 정리하지 않습니다.')
    def checkpoint():
        record['definition_inactive'] = True
        save(path, record)
    result = cleanup_interrupted_migration(adapter, request, record['attempt_id'],
        record['task_arn'], record['task_definition_arn'], record['image'],
        record['image_digest'], definition_inactive=record.get('definition_inactive', False),
        checkpoint=checkpoint)
    record['status'] = 'cleaned'
    record['cleanup'] = result
    save(path, record)
    return result


def launch(adapter: AwsExpressAdapter, request: PostgresRequest,
           plan: dict, path: Path, record: dict) -> None:
    attempt = record['attempt_id']
    image = plan['repository'] + ':' + attempt + '-db'
    record['image'] = image
    record['status'] = 'image_pushing'
    save(path, record)
    with tempfile.TemporaryDirectory(prefix='sky-cleanup-docker-') as directory:
        adapter.command(['docker', 'info', '--format', '{{.ServerVersion}}'], private=True, quiet=True)
        architecture = adapter.command(['docker', 'image', 'inspect', 'alpine:3.21',
            '--format', '{{.Architecture}}'], private=True, quiet=True)
        if architecture != 'arm64':
            raise AwsConfigurationError('로컬 alpine:3.21 ARM64 이미지가 필요합니다.')
        password = adapter.aws(['ecr', 'get-login-password'], private=True, quiet=True)
        if not password:
            raise AwsConfigurationError('ECR 로그인 암호가 비어 있습니다.')
        adapter.command(['docker', '--config', directory, 'login', '--username', 'AWS',
                         '--password-stdin', plan['repository'].split('/')[0]],
                        stdin=password, private=True, quiet=True)
        adapter.command(['docker', 'tag', 'alpine:3.21', image], private=True, quiet=True)
        adapter.command(['docker', '--config', directory, 'push', image], timeout=600,
                        private=True, quiet=True)
    details = read(adapter, ['ecr', 'describe-images', '--registry-id', request.account,
        '--repository-name', 'sky-managed', '--image-ids', 'imageTag=' + attempt + '-db']).get('imageDetails', [])
    if (len(details) != 1 or details[0].get('registryId') != request.account
            or details[0].get('repositoryName') != 'sky-managed'
            or attempt + '-db' not in details[0].get('imageTags', [])
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', details[0].get('imageDigest', ''))):
        raise AwsConfigurationError('검증용 ECR 이미지 digest를 확인하지 못했습니다.')
    record['image_digest'] = details[0]['imageDigest']
    record['status'] = 'image_verified'
    save(path, record)

    tags = [{'key': 'sky-managed', 'value': 'true'},
            {'key': 'sky-app', 'value': request.application_id},
            {'key': 'sky-attempt', 'value': attempt}]
    definition = {'family': 'sky-migrate-' + attempt, 'networkMode': 'awsvpc',
                  'requiresCompatibilities': ['FARGATE'], 'cpu': '256', 'memory': '512',
                  'runtimePlatform': {'cpuArchitecture': 'ARM64', 'operatingSystemFamily': 'LINUX'},
                  'executionRoleArn': plan['execution_role_arn'],
                  'containerDefinitions': [{'name': 'migration',
                      'image': plan['repository'] + '@' + record['image_digest'],
                      'essential': True, 'command': ['sh', '-c', 'echo sky-cleanup-probe']}],
                  'tags': tags}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-cleanup-task-',
                                     encoding='utf-8') as file:
        json.dump(definition, file)
        file.flush()
        registered = read(adapter, ['ecs', 'register-task-definition', '--cli-input-json', 'file://' + file.name])
    arn = registered.get('taskDefinition', {}).get('taskDefinitionArn', '')
    if not re.fullmatch(f'arn:aws:ecs:{request.region}:{request.account}:task-definition/sky-migrate-{attempt}:[0-9]+', arn):
        raise AwsConfigurationError('검증용 ECS 태스크 정의 ARN을 확인하지 못했습니다.')
    record['task_definition_arn'] = arn
    record['status'] = 'registered'
    save(path, record)

    payload = {'cluster': 'default', 'launchType': 'FARGATE', 'count': 1,
               'taskDefinition': arn,
               'networkConfiguration': {'awsvpcConfiguration': {
                   'subnets': [request.subnet_ids[0]],
                   'securityGroups': [request.service_security_group],
                   'assignPublicIp': 'ENABLED'}}, 'tags': tags}
    record['status'] = 'task_submitting'
    save(path, record)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-cleanup-run-',
                                     encoding='utf-8') as file:
        json.dump(payload, file)
        file.flush()
        launched = read(adapter, ['ecs', 'run-task', '--cli-input-json', 'file://' + file.name])
    tasks = launched.get('tasks', [])
    task_arn = tasks[0].get('taskArn', '') if len(tasks) == 1 else ''
    if (launched.get('failures') or not re.fullmatch(
            f'arn:aws:ecs:{request.region}:{request.account}:task/default/[a-f0-9]{{32}}', task_arn)):
        raise AwsConfigurationError('ECS 태스크 시작 응답을 확인하지 못했습니다. 로컬 기록과 AWS를 조사하세요.')
    record['task_arn'] = task_arn
    record['status'] = 'running'
    save(path, record)
    adapter.aws(['ecs', 'wait', 'tasks-stopped', '--cluster', 'default', '--tasks', task_arn],
                timeout=900, private=True, quiet=True)
    outcome = inspect_migration_task(adapter, request.application_id, request.account,
        request.region, attempt, task_arn, arn)
    record['outcome'] = outcome['status']
    record['status'] = 'stopped'
    save(path, record)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Exercise owned ECS/ECR migration cleanup without RDS')
    parser.add_argument('--apply', action='store_true', help='Create billable temporary ECS/ECR resources')
    parser.add_argument('--cleanup-record', type=Path, help='Finish cleanup from a saved drill record')
    parser.add_argument('--application', default='demo-app')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    args = parser.parse_args(argv)
    if args.cleanup_record and not args.apply:
        parser.error('--cleanup-record requires --apply')
    request = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    plan = preflight(adapter, request)
    print('Read-only cleanup drill preflight:', json.dumps(plan, ensure_ascii=False), flush=True)
    if not args.apply:
        print('읽기 전용 확인 완료. --apply 없이는 ECS 태스크나 ECR 이미지를 만들지 않습니다.', flush=True)
        return
    if args.cleanup_record:
        path = args.cleanup_record
        record = json.loads(path.read_text())
        if any(record.get(key) != value for key, value in {
                'application_id': request.application_id, 'account': request.account,
                'region': request.region, 'vpc_id': request.vpc_id,
                'service_security_group': request.service_security_group}.items()):
            raise ValueError('저장된 드릴 소유 정보가 요청과 다릅니다.')
        if record.get('status') == 'cleaned':
            print('이미 정리된 드릴 기록입니다:', path, flush=True)
            return
    else:
        attempt = secrets.token_hex(8) + '-a1'
        path = Path('.sky/migration-cleanup-drills') / (attempt + '.json')
        record = {'attempt_id': attempt, 'application_id': request.application_id,
                  'account': request.account, 'region': request.region,
                  'vpc_id': request.vpc_id,
                  'service_security_group': request.service_security_group,
                  'status': 'planned'}
        save(path, record)
    print('드릴 기록:', path, flush=True)
    if not args.cleanup_record:
        launch(adapter, request, plan, path, record)
    elif (record.get('outcome') not in {'succeeded', 'failed'}
          and record.get('task_arn') and record.get('task_definition_arn')):
        outcome = inspect_migration_task(adapter, request.application_id, request.account,
            request.region, record['attempt_id'], record['task_arn'], record['task_definition_arn'])
        record['outcome'] = outcome['status']
        save(path, record)
    result = cleanup(adapter, request, path, record)
    if record.get('outcome') != 'succeeded':
        raise AwsConfigurationError('검증용 태스크가 성공 종료하지 않았습니다. 자원은 정리했고 기록을 조사하세요.')
    if not args.cleanup_record:
        try:
            adapter.command(['docker', 'image', 'rm', record['image']], private=True, quiet=True)
        except AwsConfigurationError:
            print('AWS 정리는 완료됐지만 로컬 Docker 태그는 남아 있습니다:', record['image'], flush=True)
    print('PASS: owned ECS definition and ECR tag cleaned:',
          json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
