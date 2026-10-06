"""Opt-in one-shot ECS Fargate runner and read-only migration recovery check."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from adapters.database.migrations import MigrationBundle, stage_migrator_context
from adapters.aws.postgres import PostgresRequest


def inspect_migration_task(adapter: AwsExpressAdapter, application_id: str, account: str,
                           region: str, attempt_id: str, task_arn: str,
                           definition_arn: str) -> dict:
    """Read a single owned task; an expired or missing ECS result remains unknown."""
    if (not re.fullmatch(r'[a-z][a-z0-9]*(-[a-z0-9]+)*', application_id)
            or not re.fullmatch(r'\d{12}', account)
            or not re.fullmatch(r'[a-z]{2}-[a-z]+-\d', region)
            or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id)):
        raise ValueError('마이그레이션 소유자 정보가 올바르지 않습니다.')
    task_prefix = f'arn:aws:ecs:{region}:{account}:task/default/'
    definition_prefix = f'arn:aws:ecs:{region}:{account}:task-definition/sky-migrate-{attempt_id}:'
    if (not isinstance(task_arn, str) or not task_arn.startswith(task_prefix)
            or not re.fullmatch(r'[a-f0-9]{32}', task_arn.removeprefix(task_prefix))
            or not isinstance(definition_arn, str) or not definition_arn.startswith(definition_prefix)
            or not definition_arn.removeprefix(definition_prefix).isdigit()):
        raise ValueError('마이그레이션 태스크 ARN 또는 정의 ARN이 예상과 다릅니다.')
    if adapter.settings.region != region or adapter.settings.expected_account != account:
        raise AwsConfigurationError('마이그레이션 조회 계정·리전 설정이 소유자 정보와 다릅니다.')
    response = json.loads(adapter.aws(['ecs', 'describe-tasks', '--cluster', 'default',
        '--tasks', task_arn, '--include', 'TAGS'], private=True, quiet=True))
    tasks = response.get('tasks', [])
    if response.get('failures') or len(tasks) != 1:
        return {'status': 'unknown', 'task_arn': task_arn, 'task_definition_arn': definition_arn}
    task = tasks[0]
    tags = {item.get('key'): item.get('value') for item in task.get('tags', [])}
    containers = task.get('containers', [])
    if (task.get('taskArn') != task_arn or task.get('taskDefinitionArn') != definition_arn
            or task.get('clusterArn') != f'arn:aws:ecs:{region}:{account}:cluster/default'
            or task.get('launchType') != 'FARGATE'
            or tags.get('sky-managed') != 'true'
            or tags.get('sky-app') != application_id
            or tags.get('sky-attempt') != attempt_id
            or len(containers) > 1
            or (containers and containers[0].get('name') != 'migration')):
        raise AwsConfigurationError('마이그레이션 태스크의 소유권·실행 구성이 예상과 다릅니다.')
    last_status = task.get('lastStatus')
    if last_status == 'STOPPED':
        if containers:
            exit_code = containers[0].get('exitCode')
            status = ('succeeded' if type(exit_code) is int and exit_code == 0 else
                      'failed' if type(exit_code) is int else 'unknown')
        else:
            status = 'failed' if task.get('stopCode') == 'TaskFailedToStart' else 'unknown'
    elif last_status in {'PROVISIONING', 'PENDING', 'ACTIVATING', 'RUNNING',
                         'DEACTIVATING', 'STOPPING', 'DEPROVISIONING'}:
        status = 'running'
    else:
        status = 'unknown'
    return {'status': status, 'task_arn': task_arn, 'task_definition_arn': definition_arn,
            'last_status': last_status, 'exit_code': containers[0].get('exitCode') if containers else None}


def cleanup_interrupted_migration(adapter: AwsExpressAdapter, request: PostgresRequest,
                                  attempt_id: str, task_arn: str, definition_arn: str,
                                  image: str, image_digest: str, *,
                                  definition_inactive: bool = False,
                                  verified_outcome: bool = False,
                                  checkpoint=None) -> dict:
    """Remove only a stopped owned migration's definition and unique ECR tag."""
    request.validate()
    expected_repository = (f'{request.account}.dkr.ecr.{request.region}.amazonaws.com/'
                           'sky-managed')
    if (adapter.settings.region != request.region
            or adapter.settings.expected_account != request.account
            or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id)
            or not isinstance(task_arn, str)
            or not re.fullmatch(f'arn:aws:ecs:{re.escape(request.region)}:{request.account}:task/default/[a-f0-9]{{32}}', task_arn)
            or not isinstance(definition_arn, str)
            or not re.fullmatch(f'arn:aws:ecs:{re.escape(request.region)}:{request.account}:task-definition/sky-migrate-{attempt_id}:[0-9]+', definition_arn)
            or image != f'{expected_repository}:{attempt_id}-db'
            or not isinstance(image_digest, str)
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest)):
        raise AwsConfigurationError('정리할 마이그레이션 이미지 소유 정보가 올바르지 않습니다.')
    # Require a fresh task result unless success was already journaled or the
    # definition was previously deregistered; ECS can expire either record.
    if not definition_inactive and not verified_outcome:
        outcome = inspect_migration_task(adapter, request.application_id,
            request.account, request.region, attempt_id, task_arn, definition_arn)
        if outcome['status'] not in {'succeeded', 'failed'}:
            raise AwsConfigurationError('마이그레이션 태스크 종료 결과가 확정되지 않아 정리하지 않습니다.')
    family = f'sky-migrate-{attempt_id}'
    running = json.loads(adapter.aws(['ecs', 'list-tasks', '--cluster', 'default',
        '--family', family, '--desired-status', 'RUNNING'], private=True, quiet=True))
    if (running.get('nextToken') or not isinstance(running.get('taskArns'), list)
            or running['taskArns']):
        raise AwsConfigurationError('같은 마이그레이션 계열에 실행 중인 태스크가 있어 정리하지 않습니다.')
    if not definition_inactive:
        described = json.loads(adapter.aws(['ecs', 'describe-task-definition',
            '--task-definition', definition_arn, '--include', 'TAGS'], private=True, quiet=True))
        definition = described.get('taskDefinition', {})
        tags = {item.get('key'): item.get('value') for item in described.get('tags', [])}
        containers = definition.get('containerDefinitions', [])
        if (definition.get('taskDefinitionArn') != definition_arn
                or definition.get('family') != family
                or definition.get('status') not in {'ACTIVE', 'INACTIVE'}
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != request.application_id
                or tags.get('sky-attempt') != attempt_id
                or len(containers) != 1 or containers[0].get('name') != 'migration'
                or containers[0].get('image') != expected_repository + '@' + image_digest):
            raise AwsConfigurationError('마이그레이션 태스크 정의의 소유권·이미지가 예상과 다릅니다.')

    tag = attempt_id + '-db'
    def image_state():
        response = json.loads(adapter.aws(['ecr', 'batch-get-image',
            '--registry-id', request.account, '--repository-name', 'sky-managed',
            '--image-ids', 'imageTag=' + tag], private=True, quiet=True))
        images, failures = response.get('images'), response.get('failures')
        if (images == [] and isinstance(failures, list) and len(failures) == 1
                and failures[0].get('imageId', {}).get('imageTag') == tag
                and failures[0].get('failureCode') == 'ImageNotFound'):
            return False
        if (not isinstance(images, list) or len(images) != 1 or failures != []
                or images[0].get('registryId') != request.account
                or images[0].get('repositoryName') != 'sky-managed'
                or images[0].get('imageId', {}).get('imageTag') != tag
                or images[0]['imageId'].get('imageDigest') != image_digest):
            raise AwsConfigurationError('마이그레이션 ECR 태그의 소유권·digest를 확인하지 못했습니다.')
        return True

    image_present = image_state()
    if not definition_inactive:
        if definition['status'] == 'ACTIVE':
            response = json.loads(adapter.aws(['ecs', 'deregister-task-definition',
                '--task-definition', definition_arn], private=True, quiet=True))
            deregistered = response.get('taskDefinition', {})
            if (deregistered.get('taskDefinitionArn') != definition_arn
                    or deregistered.get('status') != 'INACTIVE'):
                raise AwsConfigurationError('마이그레이션 태스크 정의 비활성화를 확인하지 못했습니다.')
        definition_inactive = True
        if checkpoint is not None:
            checkpoint()
    if image_present:
        deleted = json.loads(adapter.aws(['ecr', 'batch-delete-image',
            '--registry-id', request.account, '--repository-name', 'sky-managed',
            '--image-ids', 'imageTag=' + tag], private=True, quiet=True))
        ids = deleted.get('imageIds', [])
        if (deleted.get('failures') or len(ids) != 1
                or ids[0].get('imageTag') != tag
                or ids[0].get('imageDigest') != image_digest):
            raise AwsConfigurationError('마이그레이션 ECR 태그 삭제 응답이 예상과 다릅니다.')
        if image_state():
            raise AwsConfigurationError('마이그레이션 ECR 태그가 삭제 후에도 남아 있습니다.')
    return {'state': 'done', 'task_definition_arn': definition_arn,
            'image': image, 'image_deleted': image_present}


class AwsMigrationRunner:
    def __init__(self, adapter: AwsExpressAdapter, request: PostgresRequest,
                 database: dict, bundle: MigrationBundle, repository: str, attempt_id: str):
        request.validate()
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise ValueError('마이그레이션 배포 시도 ID가 올바르지 않습니다.')
        if database.get('service_security_group') != request.service_security_group:
            raise AwsConfigurationError('마이그레이션 서비스 보안 그룹이 DB 기록과 다릅니다.')
        if repository != f'{request.account}.dkr.ecr.{request.region}.amazonaws.com/sky-managed':
            raise AwsConfigurationError('마이그레이션 ECR 저장소가 예상과 다릅니다.')
        if database.get('migration_log_group') != f'/sky/migrations/{request.application_id}':
            raise AwsConfigurationError('마이그레이션 로그 그룹이 DB 기록과 다릅니다.')
        self.adapter, self.request, self.database = adapter, request, database
        self.bundle, self.repository, self.attempt_id = bundle, repository, attempt_id
        self.image = f'{repository}:{attempt_id}-db'
        self.image_digest = None
        self.family = f'sky-migrate-{attempt_id}'
        self.subnet = None
        self.registered_arn = None
        self.task_arn = None
        self.completed = False

    def preflight(self) -> str:
        """Require a default-VPC public subnet for image pull and Secrets Manager access."""
        req = self.request
        response = json.loads(self.adapter.aws(['ec2', 'describe-route-tables', '--filters',
            f'Name=vpc-id,Values={req.vpc_id}'], private=True, quiet=True))
        tables = response.get('RouteTables', [])
        main = [table for table in tables if any(association.get('Main') is True
                for association in table.get('Associations', []))]
        if len(main) != 1:
            raise AwsConfigurationError('마이그레이션용 기본 VPC 라우팅 테이블을 확인하지 못했습니다.')
        for subnet in req.subnet_ids:
            associated = [table for table in tables if any(association.get('SubnetId') == subnet
                          for association in table.get('Associations', []))]
            if len(associated) > 1:
                raise AwsConfigurationError('마이그레이션 서브넷의 라우팅 테이블이 중복됩니다.')
            table = associated[0] if associated else main[0]
            if any(route.get('DestinationCidrBlock') == '0.0.0.0/0'
                   and re.fullmatch(r'igw-[a-f0-9]{8,17}', route.get('GatewayId', ''))
                   and route.get('State') == 'active' for route in table.get('Routes', [])):
                self.subnet = subnet
                break
        if not self.subnet:
            raise AwsConfigurationError('일회성 ECS 태스크가 이미지를 받을 수 있는 공개 서브넷이 필요합니다.')
        logs = json.loads(self.adapter.aws(['logs', 'describe-log-groups', '--log-group-name-prefix',
            self.database['migration_log_group']], private=True, quiet=True)).get('logGroups', [])
        if (len(logs) != 1 or logs[0].get('logGroupName') != self.database['migration_log_group']
                or logs[0].get('retentionInDays') != 14):
            raise AwsConfigurationError('마이그레이션 로그 그룹의 이름·보존 기간이 예상과 다릅니다.')
        return self.subnet

    def task_definition(self) -> dict:
        if not isinstance(self.image_digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', self.image_digest):
            raise AwsConfigurationError('마이그레이션 이미지 digest 확인이 필요합니다.')
        req, db = self.request, self.database
        return {'family': self.family, 'networkMode': 'awsvpc',
                'requiresCompatibilities': ['FARGATE'], 'cpu': '256', 'memory': '512',
                'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
                'executionRoleArn': db['execution_role_arn'],
                'containerDefinitions': [{'name': 'migration',
                    'image': f'{self.repository}@{self.image_digest}', 'essential': True,
                    'environment': [
                        {'name': 'PGHOST', 'value': db['endpoint']},
                        {'name': 'PGPORT', 'value': str(db['port'])},
                        {'name': 'PGDATABASE', 'value': 'appdb'},
                        {'name': 'PGSSLMODE', 'value': 'require'}],
                    'secrets': [
                        {'name': 'PGUSER', 'valueFrom': db['secret_arn'] + ':username::'},
                        {'name': 'PGPASSWORD', 'valueFrom': db['secret_arn'] + ':password::'}],
                    'logConfiguration': {'logDriver': 'awslogs', 'options': {
                        'awslogs-group': db['migration_log_group'], 'awslogs-region': req.region,
                        'awslogs-stream-prefix': 'migration'}}}],
                'tags': [{'key': 'sky-managed', 'value': 'true'},
                         {'key': 'sky-app', 'value': req.application_id},
                         {'key': 'sky-attempt', 'value': self.attempt_id}]}

    def run_task(self) -> dict:
        if not self.subnet:
            raise AwsConfigurationError('마이그레이션 네트워크 사전 점검이 필요합니다.')
        self.completed = False
        req = self.request
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-migrate-task-',
                                         encoding='utf-8') as file:
            json.dump(self.task_definition(), file)
            file.flush()
            registered = json.loads(self.adapter.aws(['ecs', 'register-task-definition',
                '--cli-input-json', 'file://' + file.name], private=True))
        definition = registered.get('taskDefinition', {})
        prefix = f'arn:aws:ecs:{req.region}:{req.account}:task-definition/{self.family}:'
        arn = definition.get('taskDefinitionArn', '')
        if (not isinstance(arn, str) or not arn.startswith(prefix)
                or not arn.removeprefix(prefix).isdigit()):
            raise AwsConfigurationError('마이그레이션 태스크 정의 ARN을 확인하지 못했습니다.')
        self.registered_arn = arn
        if self.adapter.checkpoint:
            self.adapter.checkpoint(aws_migration_status='registered',
                                    aws_migration_task_definition_arn=arn,
                                    aws_migration_image=self.image,
                                    aws_migration_image_digest=self.image_digest,
                                    aws_migration_bundle_digest=self.bundle.digest)
        payload = {'cluster': 'default', 'launchType': 'FARGATE', 'count': 1,
                   'taskDefinition': arn,
                   'networkConfiguration': {'awsvpcConfiguration': {
                       'subnets': [self.subnet],
                       'securityGroups': [req.service_security_group],
                       'assignPublicIp': 'ENABLED'}},
                   'tags': [{'key': 'sky-managed', 'value': 'true'},
                            {'key': 'sky-app', 'value': req.application_id},
                            {'key': 'sky-attempt', 'value': self.attempt_id}]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-migrate-run-',
                                         encoding='utf-8') as file:
            json.dump(payload, file)
            file.flush()
            launched = json.loads(self.adapter.aws(['ecs', 'run-task', '--cli-input-json',
                'file://' + file.name], private=True))
        tasks = launched.get('tasks', [])
        task_arn = tasks[0].get('taskArn', '') if len(tasks) == 1 else ''
        expected = f'arn:aws:ecs:{req.region}:{req.account}:task/default/'
        if (launched.get('failures') or not isinstance(task_arn, str)
                or not task_arn.startswith(expected) or len(tasks) != 1):
            raise AwsConfigurationError(f'마이그레이션 태스크 시작 상태를 확인하지 못했습니다. {arn}을 확인하세요.')
        self.task_arn = task_arn
        if self.adapter.checkpoint:
            self.adapter.checkpoint(aws_migration_status='running', aws_migration_task_arn=task_arn)
        try:
            self.adapter.aws(['ecs', 'wait', 'tasks-stopped', '--cluster', 'default', '--tasks', task_arn],
                             timeout=900, private=True, quiet=True)
            outcome = inspect_migration_task(self.adapter, req.application_id, req.account,
                                             req.region, self.attempt_id, task_arn, arn)
        except Exception as exc:
            raise AwsConfigurationError(f'마이그레이션 종료를 확인하지 못했습니다. 태스크 {task_arn}과 정의 {arn}을 확인하세요: {exc}') from None
        if outcome['status'] == 'failed':
            raise AwsConfigurationError(f'마이그레이션이 실패했습니다. 태스크 {task_arn}와 CloudWatch 로그 '
                                        f'{self.database["migration_log_group"]}을 확인하세요.')
        if outcome['status'] != 'succeeded':
            raise AwsConfigurationError(f'마이그레이션 종료를 확인하지 못했습니다: {task_arn}')
        self.completed = True
        return {'task_arn': task_arn, 'task_definition_arn': arn,
                'image': self.image, 'image_digest': self.image_digest,
                'bundle_digest': self.bundle.digest,
                'log_group': self.database['migration_log_group']}

    def verify_pushed_image(self) -> str:
        self.image_digest = None
        tag = self.attempt_id + '-db'
        details = json.loads(self.adapter.aws(['ecr', 'describe-images',
            '--registry-id', self.request.account, '--repository-name', 'sky-managed',
            '--image-ids', f'imageTag={tag}'], private=True, quiet=True)).get('imageDetails', [])
        image = details[0] if len(details) == 1 else {}
        digest = image.get('imageDigest', '')
        if (image.get('registryId') != self.request.account
                or image.get('repositoryName') != 'sky-managed'
                or tag not in image.get('imageTags', [])
                or not isinstance(digest, str)
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest)):
            raise AwsConfigurationError('마이그레이션 ECR 이미지의 소유권·digest를 확인하지 못했습니다.')
        self.image_digest = digest
        return digest

    def cleanup_completed(self) -> bool:
        """Delete only the known stopped task's definition and migration image tag."""
        if not self.completed or not self.registered_arn or not self.task_arn:
            return False
        def checkpoint():
            if self.adapter.checkpoint:
                self.adapter.checkpoint(aws_migration_cleanup_definition_inactive=True)
        result = cleanup_interrupted_migration(self.adapter, self.request, self.attempt_id,
            self.task_arn, self.registered_arn, self.image, self.image_digest,
            checkpoint=checkpoint)
        if self.adapter.checkpoint:
            self.adapter.checkpoint(aws_migration_cleanup_state='done',
                                    aws_migration_cleanup_image_deleted=result['image_deleted'])
        self.completed = False
        return True

    def build_and_run(self) -> dict:
        if not self.subnet:
            self.preflight()
        with tempfile.TemporaryDirectory(prefix='sky-migrate-build-') as directory:
            context = stage_migrator_context(self.bundle, Path(directory) / 'context')
            self.adapter.command(['docker', 'build', '--platform', 'linux/amd64', '-t', self.image,
                                  str(context)], timeout=900)
        password = self.adapter.aws(['ecr', 'get-login-password'], private=True)
        if not password:
            raise AwsConfigurationError('마이그레이션 이미지용 ECR 로그인 암호를 받지 못했습니다.')
        self.adapter.sensitive.append(password)
        endpoint = os.getenv('DOCKER_HOST') or self.adapter.command(
            ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], private=True)
        with tempfile.TemporaryDirectory(prefix='sky-migrate-docker-auth-') as config:
            docker = ['docker', '--config', config, '--host', endpoint]
            self.adapter.command(docker + ['login', '--username', 'AWS', '--password-stdin',
                                           self.repository.split('/')[0]], stdin=password, private=True)
            self.adapter.command(docker + ['push', self.image], timeout=600)
        try:
            if self.adapter.checkpoint:
                self.adapter.checkpoint(aws_migration_status='image_pushed',
                                        aws_migration_image=self.image,
                                        aws_migration_bundle_digest=self.bundle.digest)
            self.verify_pushed_image()
            if self.adapter.checkpoint:
                self.adapter.checkpoint(aws_migration_status='image_verified',
                                        aws_migration_image=self.image,
                                        aws_migration_image_digest=self.image_digest,
                                        aws_migration_bundle_digest=self.bundle.digest)
        except Exception as exc:
            self.adapter.event('cleanup', '마이그레이션 이미지 업로드 상태를 확인하지 못했습니다. '
                               + self.image + '를 직접 확인하세요.')
            raise AwsConfigurationError(f'마이그레이션 이미지 digest 조회 실패: {exc}') from None
        self.adapter.event('migrating', '검증된 SQL 마이그레이션을 일회성 ECS 태스크로 실행합니다.')
        try:
            result = self.run_task()
            if self.adapter.checkpoint:
                self.adapter.checkpoint(aws_migration_status='succeeded',
                                        aws_migration_result=result)
        except Exception as exc:
            if self.adapter.checkpoint:
                try:
                    self.adapter.checkpoint(aws_migration_status='needs_attention')
                except Exception:
                    pass
            self.adapter.event('cleanup', '마이그레이션이 실패했거나 결과가 불확실합니다. ECS 태스크·정의와 ECR 이미지 '
                               + self.image + '를 직접 확인하세요.')
            raise AwsConfigurationError(f'마이그레이션 결과를 확인하지 못했습니다. '
                                        f'태스크 {self.task_arn or "미시작"}, 정의 '
                                        f'{self.registered_arn or "미등록"}: {exc}') from None
        try:
            result['cleanup_complete'] = self.cleanup_completed()
        except Exception as exc:
            result['cleanup_complete'] = False
            self.adapter.event('cleanup', str(exc) + ' 리소스: ' + self.image)
        return result


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Read-only check of one owned ECS migration task')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--attempt', required=True)
    parser.add_argument('--task-arn', required=True)
    parser.add_argument('--task-definition-arn', required=True)
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account, account_pin_required=True)
    settings.validate()
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    caller = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
    if caller.get('Account') != args.account:
        raise AwsConfigurationError('현재 AWS 계정이 지정한 마이그레이션 계정과 다릅니다.')
    outcome = inspect_migration_task(adapter, args.application, args.account, args.region,
                                     args.attempt, args.task_arn, args.task_definition_arn)
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == '__main__':
    main()
