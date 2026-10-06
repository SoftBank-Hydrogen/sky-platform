"""Read-only ECS Fargate task plan for checking a restored PostgreSQL database."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from adapters.database.migrations import MigrationBundle, collect_sql_migrations
from adapters.aws.postgres import AwsPostgresProvisioner, PostgresRequest, discover_existing_postgres
from adapters.aws.postgres_restore_credentials import plan_restore_credentials
from adapters.aws.postgres_restore_instance import RestoreInstance
from adapters.aws.postgres_restore_verifier import stage_restore_verifier_context


def _public_subnet(adapter: AwsExpressAdapter, vpc_id: str,
                   subnet_ids: list[str]) -> str:
    response = json.loads(adapter.aws(['ec2', 'describe-route-tables', '--filters',
        'Name=vpc-id,Values=' + vpc_id], private=True, quiet=True))
    tables = response.get('RouteTables')
    if response.get('NextToken') or not isinstance(tables, list):
        raise AwsConfigurationError('검사 작업의 VPC 라우팅 테이블을 확인하지 못했습니다.')
    main = [table for table in tables if any(item.get('Main') is True
            for item in table.get('Associations', []))]
    if len(main) != 1:
        raise AwsConfigurationError('검사 작업의 기본 라우팅 테이블을 확인하지 못했습니다.')
    for subnet in subnet_ids:
        specific = [table for table in tables if any(item.get('SubnetId') == subnet
                    for item in table.get('Associations', []))]
        if len(specific) > 1:
            raise AwsConfigurationError('검사 작업의 서브넷 라우팅이 중복됩니다.')
        table = specific[0] if specific else main[0]
        if any(route.get('DestinationCidrBlock') == '0.0.0.0/0'
               and re.fullmatch(r'igw-[a-f0-9]{8,17}', route.get('GatewayId', ''))
               and route.get('State') == 'active' for route in table.get('Routes', [])):
            return subnet
    raise AwsConfigurationError('이미지·비밀을 받을 공개 ECS 서브넷이 없습니다.')


def plan_restore_verifier_task(application_id: str, snapshot_id: str, target_id: str,
                               settings: AwsSettings, vpc_id: str, db_group_id: str,
                               probe_group_id: str, project: Path) -> dict:
    bundle = collect_sql_migrations(project)
    credentials = plan_restore_credentials(application_id, snapshot_id, target_id, settings)
    restore = RestoreInstance(application_id, snapshot_id, target_id,
                              settings, vpc_id, db_group_id)
    target = restore.inspect_for_probe(probe_group_id)
    if target['status'] != 'available' or target.get('endpoint') is None:
        raise AwsConfigurationError('검사 작업에는 사용 가능한 복원 DB 엔드포인트가 필요합니다.')
    source = discover_existing_postgres(application_id, settings)
    request = PostgresRequest(application_id, settings.expected_account, settings.region,
                              source['vpc_id'], tuple(source['subnet_ids']),
                              settings.service_security_group)
    request.validate()
    database = AwsPostgresProvisioner(request).inspect_current()
    if (source['vpc_id'] != vpc_id
            or database['database_id'] != credentials['source_database_id']
            or database['secret_arn'] != credentials['secret_arn']
            or target['target_database_id'] != target_id
            or target['group_id'] != db_group_id):
        raise AwsConfigurationError('복원 검사 계획의 원본·대상 소유권이 다릅니다.')
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    subnet = _public_subnet(adapter, vpc_id, list(request.subnet_ids))
    logs = json.loads(adapter.aws(['logs', 'describe-log-groups',
        '--log-group-name-prefix', database['migration_log_group']],
        private=True, quiet=True))
    groups = logs.get('logGroups')
    if (logs.get('nextToken') or not isinstance(groups, list) or len(groups) != 1
            or groups[0].get('logGroupName') != database['migration_log_group']
            or groups[0].get('retentionInDays') != 14):
        raise AwsConfigurationError('검사 결과 로그 그룹의 소유권·보존 기간이 다릅니다.')
    repository = f'{settings.expected_account}.dkr.ecr.{settings.region}.amazonaws.com/sky-managed'
    return {'application_id': application_id, 'snapshot_id': snapshot_id,
            'target_database_id': target_id, 'account': settings.expected_account,
            'region': settings.region, 'vpc_id': vpc_id,
            'db_group_id': db_group_id, 'probe_group_id': probe_group_id,
            'endpoint': target['endpoint'], 'port': target['port'],
            'subnet_id': subnet, 'execution_role_arn': database['execution_role_arn'],
            'log_group': database['migration_log_group'], 'repository': repository,
            'bundle_digest': bundle.digest, 'migration_count': len(bundle.migrations),
            'secret_version_id': credentials['secret_version_id'],
            'username_value_from': credentials['ecs_username_value_from'],
            'password_value_from': credentials['ecs_password_value_from'],
            'password_match_unverified': True}


def verifier_task_definition(plan: dict, image_digest: str, attempt_id: str,
                             marker_id: str | None = None) -> dict:
    if (not re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest)
            or not re.fullmatch(r'[a-f0-9]{16}', attempt_id)
            or (marker_id is not None and not re.fullmatch(r'[a-f0-9]{32}', marker_id))):
        raise ValueError('검사 이미지 digest·시도 ID·데이터 표식 형식이 올바르지 않습니다.')
    family = 'sky-restore-verify-' + attempt_id
    environment = [{'name': 'PGHOST', 'value': plan['endpoint']},
                   {'name': 'PGPORT', 'value': '5432'},
                   {'name': 'PGDATABASE', 'value': 'appdb'},
                   {'name': 'PGSSLMODE', 'value': 'require'}]
    if marker_id:
        environment.append({'name': 'SKY_RESTORE_MARKER_ID', 'value': marker_id})
    return {'family': family, 'networkMode': 'awsvpc',
            'requiresCompatibilities': ['FARGATE'], 'cpu': '256', 'memory': '512',
            'runtimePlatform': {'cpuArchitecture': 'X86_64',
                                'operatingSystemFamily': 'LINUX'},
            'executionRoleArn': plan['execution_role_arn'],
            'containerDefinitions': [{'name': 'verify',
                'image': plan['repository'] + '@' + image_digest,
                'essential': True, 'environment': environment,
                'secrets': [{'name': 'PGUSER', 'valueFrom': plan['username_value_from']},
                            {'name': 'PGPASSWORD', 'valueFrom': plan['password_value_from']}],
                'logConfiguration': {'logDriver': 'awslogs', 'options': {
                    'awslogs-group': plan['log_group'], 'awslogs-region': plan['region'],
                    'awslogs-stream-prefix': 'restore-verify'}}}],
            'tags': [{'key': 'sky-managed', 'value': 'true'},
                     {'key': 'sky-app', 'value': plan['application_id']},
                     {'key': 'sky-restore-target', 'value': plan['target_database_id']},
                     {'key': 'sky-attempt', 'value': attempt_id}]}


def verifier_run_request(plan: dict, definition_arn: str, attempt_id: str) -> dict:
    prefix = (f'arn:aws:ecs:{plan["region"]}:{plan["account"]}:task-definition/'
              f'sky-restore-verify-{attempt_id}:')
    if not isinstance(definition_arn, str) or not definition_arn.startswith(prefix) \
            or not definition_arn.removeprefix(prefix).isdigit():
        raise ValueError('검사 작업 정의 ARN이 예상과 다릅니다.')
    return {'cluster': 'default', 'launchType': 'FARGATE', 'count': 1,
            'clientToken': 'sky-restore-verify-' + attempt_id,
            'taskDefinition': definition_arn,
            'networkConfiguration': {'awsvpcConfiguration': {
                'subnets': [plan['subnet_id']],
                'securityGroups': [plan['probe_group_id']],
                'assignPublicIp': 'ENABLED'}},
            'tags': [{'key': 'sky-managed', 'value': 'true'},
                     {'key': 'sky-app', 'value': plan['application_id']},
                     {'key': 'sky-restore-target', 'value': plan['target_database_id']},
                     {'key': 'sky-attempt', 'value': attempt_id}]}


class RestoreVerifierRunner:
    def __init__(self, adapter: AwsExpressAdapter, plan: dict,
                 attempt_id: str, image_digest: str | None = None,
                 marker_id: str | None = None, checkpoint=None):
        if (adapter.settings.region != plan['region']
                or adapter.settings.expected_account != plan['account']):
            raise AwsConfigurationError('검사 작업의 AWS 계정·리전이 계획과 다릅니다.')
        if (not re.fullmatch(r'[a-f0-9]{16}', attempt_id)
                or (marker_id is not None and not re.fullmatch(r'[a-f0-9]{32}', marker_id))):
            raise ValueError('검사 시도 ID·데이터 표식 형식이 올바르지 않습니다.')
        self.adapter, self.plan, self.attempt_id = adapter, plan, attempt_id
        self.definition = (verifier_task_definition(plan, image_digest, attempt_id, marker_id)
                           if image_digest else None)
        self.image_digest = image_digest
        self.marker_id = marker_id
        self.checkpoint = checkpoint or (lambda _stage, **_fields: None)
        self.tag = 'restore-verify-' + attempt_id
        self.definition_arn: str | None = None
        self.task_arn: str | None = None

    def build_and_push(self, bundle: MigrationBundle) -> str:
        if bundle.digest != self.plan['bundle_digest']:
            raise ValueError('검사 SQL 번들이 사전 계획 뒤 변경됐습니다.')
        existing = json.loads(self.adapter.aws(['ecr', 'list-images', '--registry-id',
            self.plan['account'], '--repository-name', 'sky-managed',
            '--filter', 'tagStatus=TAGGED'], private=True, quiet=True))
        images = existing.get('imageIds')
        if (existing.get('nextToken') or not isinstance(images, list)
                or any(item.get('imageTag') == self.tag for item in images)):
            raise AwsConfigurationError('검사 이미지 태그가 이미 있거나 목록이 불완전합니다.')
        image = self.plan['repository'] + ':' + self.tag
        self.checkpoint('building', image_tag=self.tag)
        with tempfile.TemporaryDirectory(prefix='sky-restore-verify-build-') as directory:
            context = stage_restore_verifier_context(bundle, Path(directory) / 'context')
            self.adapter.command(['docker', 'build', '--platform', 'linux/amd64',
                                  '-t', image, str(context)], timeout=900)
        password = self.adapter.aws(['ecr', 'get-login-password'], private=True)
        if not password:
            raise AwsConfigurationError('검사 이미지용 ECR 로그인 암호를 받지 못했습니다.')
        self.adapter.sensitive.append(password)
        endpoint = os.getenv('DOCKER_HOST') or self.adapter.command(
            ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
            private=True)
        with tempfile.TemporaryDirectory(prefix='sky-restore-verify-auth-') as config:
            docker = ['docker', '--config', config, '--host', endpoint]
            self.adapter.command(docker + ['login', '--username', 'AWS', '--password-stdin',
                                           self.plan['repository'].split('/')[0]],
                                 stdin=password, private=True)
            self.adapter.command(docker + ['push', image], timeout=600)
        self.checkpoint('image_pushed', image_tag=self.tag)
        self.verify_image()
        self.checkpoint('image_verified', image_digest=self.image_digest)
        return image

    def verify_image(self) -> None:
        result = json.loads(self.adapter.aws(['ecr', 'describe-images',
            '--registry-id', self.plan['account'], '--repository-name', 'sky-managed',
            '--image-ids', 'imageTag=' + self.tag], private=True, quiet=True))
        images = result.get('imageDetails')
        image = images[0] if isinstance(images, list) and len(images) == 1 else {}
        digest = image.get('imageDigest')
        if (image.get('registryId') != self.plan['account']
                or image.get('repositoryName') != 'sky-managed'
                or self.tag not in image.get('imageTags', [])
                or not isinstance(digest, str)
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest)
                or (self.image_digest is not None and digest != self.image_digest)):
            raise AwsConfigurationError('검사 이미지 태그·digest 소유권이 다릅니다.')
        self.image_digest = digest
        self.definition = verifier_task_definition(
            self.plan, digest, self.attempt_id, self.marker_id)

    def register(self) -> str:
        self.verify_image()
        self.checkpoint('registering', image_digest=self.image_digest,
                        task_family=self.definition['family'])
        with tempfile.NamedTemporaryFile('w', prefix='sky-restore-verify-',
                                         suffix='.json', encoding='utf-8') as file:
            json.dump(self.definition, file)
            file.flush()
            response = json.loads(self.adapter.aws(['ecs', 'register-task-definition',
                '--cli-input-json', 'file://' + file.name], private=True, quiet=True))
        definition = response.get('taskDefinition', {})
        arn = definition.get('taskDefinitionArn')
        prefix = (f'arn:aws:ecs:{self.plan["region"]}:{self.plan["account"]}:'
                  f'task-definition/{self.definition["family"]}:')
        if (not isinstance(arn, str) or not arn.startswith(prefix)
                or not arn.removeprefix(prefix).isdigit()):
            raise AwsConfigurationError('검사 태스크 정의 등록 결과가 불확실합니다.')
        self.definition_arn = arn
        self.checkpoint('registered', task_definition_arn=arn)
        return arn

    def launch(self) -> str:
        if self.definition_arn is None:
            raise AwsConfigurationError('검사 태스크 정의를 먼저 등록해야 합니다.')
        payload = verifier_run_request(self.plan, self.definition_arn, self.attempt_id)
        self.checkpoint('launching', task_definition_arn=self.definition_arn)
        with tempfile.NamedTemporaryFile('w', prefix='sky-restore-run-',
                                         suffix='.json', encoding='utf-8') as file:
            json.dump(payload, file)
            file.flush()
            response = json.loads(self.adapter.aws(['ecs', 'run-task',
                '--cli-input-json', 'file://' + file.name], private=True, quiet=True))
        tasks = response.get('tasks')
        task = tasks[0] if isinstance(tasks, list) and len(tasks) == 1 else {}
        arn = task.get('taskArn')
        prefix = f'arn:aws:ecs:{self.plan["region"]}:{self.plan["account"]}:task/default/'
        if (response.get('failures') or not isinstance(arn, str)
                or not arn.startswith(prefix)
                or not re.fullmatch(r'[a-f0-9]{32}', arn.removeprefix(prefix))):
            raise AwsConfigurationError('검사 태스크 시작 결과가 불확실합니다. 태스크 정의를 재조회하세요.')
        self.task_arn = arn
        self.checkpoint('launched', task_arn=arn)
        return arn

    def inspect(self, task_arn: str, definition_arn: str) -> dict:
        verifier_run_request(self.plan, definition_arn, self.attempt_id)
        prefix = f'arn:aws:ecs:{self.plan["region"]}:{self.plan["account"]}:task/default/'
        if not task_arn.startswith(prefix) or not re.fullmatch(
                r'[a-f0-9]{32}', task_arn.removeprefix(prefix)):
            raise ValueError('검사 태스크 ARN이 예상과 다릅니다.')
        response = json.loads(self.adapter.aws(['ecs', 'describe-tasks', '--cluster', 'default',
            '--tasks', task_arn, '--include', 'TAGS'], private=True, quiet=True))
        tasks = response.get('tasks')
        if response.get('failures') or not isinstance(tasks, list) or len(tasks) != 1:
            return {'status': 'unknown', 'task_arn': task_arn,
                    'task_definition_arn': definition_arn}
        task = tasks[0]
        tags = {tag.get('key'): tag.get('value') for tag in task.get('tags', [])}
        containers = task.get('containers')
        container = containers[0] if isinstance(containers, list) and len(containers) == 1 else {}
        if (task.get('taskArn') != task_arn
                or task.get('taskDefinitionArn') != definition_arn
                or task.get('clusterArn') != f'arn:aws:ecs:{self.plan["region"]}:{self.plan["account"]}:cluster/default'
                or task.get('launchType') != 'FARGATE'
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != self.plan['application_id']
                or tags.get('sky-restore-target') != self.plan['target_database_id']
                or tags.get('sky-attempt') != self.attempt_id
                or (container and container.get('name') != 'verify')):
            raise AwsConfigurationError('검사 태스크의 소유권·실행 구성이 예상과 다릅니다.')
        if task.get('lastStatus') == 'STOPPED':
            status = 'succeeded' if container.get('exitCode') == 0 else 'failed'
        elif task.get('lastStatus') in {'PROVISIONING', 'PENDING', 'ACTIVATING',
                                         'RUNNING', 'DEACTIVATING', 'STOPPING',
                                         'DEPROVISIONING'}:
            status = 'running'
        else:
            status = 'unknown'
        return {'status': status, 'task_arn': task_arn,
                'task_definition_arn': definition_arn,
                'exit_code': container.get('exitCode') if container else None}

    def inspect_result(self, task_arn: str, definition_arn: str) -> dict:
        outcome = self.inspect(task_arn, definition_arn)
        if outcome['status'] != 'succeeded':
            return outcome
        task_id = task_arn.rsplit('/', 1)[-1]
        stream = 'restore-verify/verify/' + task_id
        events = []
        token = None
        for _ in range(5):
            command = ['logs', 'get-log-events', '--log-group-name', self.plan['log_group'],
                       '--log-stream-name', stream, '--start-from-head', '--limit', '100',
                       '--no-paginate']
            if token:
                command += ['--next-token', token]
            response = json.loads(self.adapter.aws(command, private=True, quiet=True))
            page = response.get('events')
            next_token = response.get('nextForwardToken')
            if not isinstance(page, list) or not isinstance(next_token, str):
                raise AwsConfigurationError('검사 결과 로그를 완전히 확인하지 못했습니다.')
            events.extend(page)
            if len(events) > 100:
                raise AwsConfigurationError('검사 결과 로그가 예상보다 많습니다.')
            if token == next_token:
                break
            token = next_token
        else:
            raise AwsConfigurationError('검사 결과 로그 페이지를 완전히 확인하지 못했습니다.')
        passed = []
        for event in events:
            try:
                message = json.loads(event.get('message', ''))
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get('status') == 'passed':
                passed.append(message)
        if (len(passed) != 1
                or passed[0].get('migration_count') != self.plan['migration_count']
                or passed[0].get('marker_checked') is not (self.marker_id is not None)):
            raise AwsConfigurationError('검사 태스크의 SQL 결과를 확인하지 못했습니다.')
        return {**outcome, 'migration_count': self.plan['migration_count'],
                'marker_checked': self.marker_id is not None,
                'bundle_digest': self.plan['bundle_digest'], 'log_stream': stream}

    def run(self) -> dict:
        definition_arn = self.register()
        task_arn = self.launch()
        self.adapter.aws(['ecs', 'wait', 'tasks-stopped', '--cluster', 'default',
                          '--tasks', task_arn], timeout=900, private=True, quiet=True)
        for attempt in range(12):
            status = self.inspect(task_arn, definition_arn)['status']
            if status == 'failed':
                raise AwsConfigurationError('검사 태스크가 실패했습니다. ECS 작업을 확인하세요.')
            if status == 'unknown' and attempt == 11:
                raise AwsConfigurationError('검사 태스크 상태가 불확실합니다.')
            try:
                if status == 'succeeded':
                    result = self.inspect_result(task_arn, definition_arn)
                    self.checkpoint('verified', task_arn=task_arn,
                                    log_stream=result['log_stream'])
                    return result
            except AwsConfigurationError:
                if attempt == 11:
                    raise
            time.sleep(5)
        raise AwsConfigurationError('검사 SQL 결과를 확인하지 못했습니다.')

    def cleanup_completed(self, outcome: dict) -> dict:
        if (outcome.get('status') != 'succeeded'
                or outcome.get('task_arn') != self.task_arn
                or outcome.get('task_definition_arn') != self.definition_arn
                or 'log_stream' not in outcome):
            raise AwsConfigurationError('성공이 확인된 검사 작업만 정리할 수 있습니다.')
        response = json.loads(self.adapter.aws(['ecs', 'deregister-task-definition',
            '--task-definition', self.definition_arn], private=True, quiet=True))
        definition = response.get('taskDefinition', {})
        if (definition.get('taskDefinitionArn') != self.definition_arn
                or definition.get('status') != 'INACTIVE'):
            raise AwsConfigurationError('검사 태스크 정의 정리를 확인하지 못했습니다.')
        self.checkpoint('definition_retired', task_definition_arn=self.definition_arn)
        removed = json.loads(self.adapter.aws(['ecr', 'batch-delete-image',
            '--repository-name', 'sky-managed', '--image-ids',
            'imageTag=' + self.tag], private=True, quiet=True))
        if (removed.get('failures') or not any(item.get('imageTag') == self.tag
                                             for item in removed.get('imageIds', []))):
            raise AwsConfigurationError('검사 이미지 태그 정리를 확인하지 못했습니다.')
        self.checkpoint('cleaned', image_tag=self.tag)
        return {'task_definition_arn': self.definition_arn,
                'image_tag': self.tag, 'status': 'cleaned'}

    def execute(self, bundle: MigrationBundle) -> dict:
        self.build_and_push(bundle)
        result = self.run()
        try:
            result['cleanup'] = self.cleanup_completed(result)
        except AwsConfigurationError:
            result['cleanup'] = {'status': 'needs_attention', 'image_tag': self.tag,
                                 'task_definition_arn': self.definition_arn}
        return result


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Read-only ECS restore verification task plan')
    parser.add_argument('--application', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--db-group-id', required=True)
    parser.add_argument('--probe-group-id', required=True)
    parser.add_argument('--service-security-group', required=True)
    parser.add_argument('--project', type=Path, required=True)
    args = parser.parse_args(argv)
    settings = AwsSettings(args.region, expected_account=args.account,
                           account_pin_required=True,
                           service_security_group=args.service_security_group)
    result = plan_restore_verifier_task(args.application, args.snapshot_id, args.target_id,
                                        settings, args.vpc_id, args.db_group_id,
                                        args.probe_group_id, args.project)
    print('읽기 전용 ECS 검사 작업 계획입니다. 작업을 등록·실행하지 않습니다.', flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
