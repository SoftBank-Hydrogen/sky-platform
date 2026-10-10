"""AWS ECS Express Mode deployment with a managed CloudFormation base stack."""
from __future__ import annotations

import json
import http.client
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assets import ASSET_ROOT
from adapters.aws.role_boundary import managed_role_boundary
from application.analysis import redact
from application.client_urls import check_browser_client_urls
from application.deployment_core import source_digest, validate_environment
from application.source_secrets import reject_plaintext_cloud_secrets, reject_supplied_secrets_in_source
from application.execution import ExecutionCapabilities
from adapters.build.image import ImageBuilder, RDS_CA_CONTAINER_PATH
from adapters.local.rehearsal import inspect_image_id, rehearse_image
from engine.compatibility import TARGET_CAPABILITIES


class AwsConfigurationError(RuntimeError):
    retryable = False


def service_group_ingress_is_restricted(group: dict) -> bool:
    """Allow an empty group or one ECS gateway-to-container-port rule only."""
    rules = group.get('IpPermissions')
    if rules == []:
        return True
    if not isinstance(rules, list) or len(rules) != 1:
        return False
    rule = rules[0]
    pairs = rule.get('UserIdGroupPairs', [])
    if (rule.get('IpProtocol') != 'tcp'
            or not isinstance(rule.get('FromPort'), int)
            or not 1 <= rule['FromPort'] <= 65535
            or rule.get('ToPort') != rule['FromPort']
            or len(pairs) != 1
            or rule.get('IpRanges', []) or rule.get('Ipv6Ranges', [])
            or rule.get('PrefixListIds', [])):
        return False
    pair = pairs[0]
    return (isinstance(pair.get('GroupId'), str)
            and re.fullmatch(r'sg-[a-f0-9]{8,17}', pair['GroupId']) is not None
            and pair['GroupId'] != group.get('GroupId')
            and pair.get('UserId', group.get('OwnerId')) == group.get('OwnerId')
            and pair.get('VpcId', group.get('VpcId')) == group.get('VpcId'))


def database_configuration_matches(configuration, database, expected_ssl_environment=None):
    if database is None:
        return True
    container = configuration.get('primaryContainer', {})
    expected_environment = [
        {'name': 'PGHOST', 'value': database['endpoint']},
        {'name': 'PGPORT', 'value': str(database['port'])},
        {'name': 'PGDATABASE', 'value': 'appdb'}]
    ssl_profiles = ([expected_ssl_environment] if expected_ssl_environment is not None else [
        [{'name': 'PGSSLMODE', 'value': 'require'}],
        postgres_ssl_environment('python')])
    expected_secrets = [
        {'name': 'PGUSER', 'valueFrom': database['secret_arn'] + ':username::'},
        {'name': 'PGPASSWORD', 'valueFrom': database['secret_arn'] + ':password::'}]
    actual_environment = [item for item in container.get('environment', [])
                          if item.get('name') in {'PGHOST', 'PGPORT', 'PGDATABASE',
                                                  'PGSSLMODE', 'PGSSLROOTCERT'}]
    return (configuration.get('executionRoleArn') == database['execution_role_arn']
            and container.get('secrets') == expected_secrets
            and any(len(actual_environment) == len(expected_environment) + len(profile)
                    and all(item in actual_environment for item in expected_environment + profile)
                    for profile in ssl_profiles))


def postgres_ssl_environment(runtime):
    if runtime in {'python', 'python-asgi', 'python-wsgi'}:
        return [{'name': 'PGSSLMODE', 'value': 'verify-full'},
                {'name': 'PGSSLROOTCERT', 'value': RDS_CA_CONTAINER_PATH}]
    return [{'name': 'PGSSLMODE', 'value': 'require'}]


@dataclass(frozen=True)
class AwsSettings:
    region: str = ''
    stack_name: str = 'sky-core'
    expected_account: str = ''
    account_pin_required: bool = False
    service_security_group: str = ''

    @classmethod
    def from_environment(cls):
        region = os.getenv('SKY_AWS_REGION') or os.getenv('AWS_REGION') or os.getenv('AWS_DEFAULT_REGION')
        if not region and shutil.which('aws'):
            try:
                result = subprocess.run(['aws', 'configure', 'get', 'region'], capture_output=True, text=True, timeout=5)
                region = result.stdout.strip() if result.returncode == 0 else ''
            except (OSError, subprocess.TimeoutExpired):
                region = ''
        return cls(region or '', expected_account=os.getenv('SKY_AWS_ACCOUNT_ID', ''),
                   account_pin_required=True,
                   service_security_group=os.getenv('SKY_AWS_SERVICE_SECURITY_GROUP', ''))

    def validate(self):
        if not re.fullmatch(r'[a-z]{2}-[a-z]+-\d', self.region):
            raise AwsConfigurationError('SKY_AWS_REGION에 유효한 AWS 리전을 설정하세요.')
        if self.stack_name != 'sky-core':
            raise AwsConfigurationError('AWS 기반 스택 이름이 예상과 다릅니다.')
        if self.expected_account and not re.fullmatch(r'\d{12}', self.expected_account):
            raise AwsConfigurationError('SKY_AWS_ACCOUNT_ID에 12자리 AWS 계정 ID를 설정하세요.')
        if self.account_pin_required and not self.expected_account:
            raise AwsConfigurationError('AWS 배포 전에 SKY_AWS_ACCOUNT_ID로 대상 계정을 지정하세요.')
        if self.service_security_group and not re.fullmatch(r'sg-[a-f0-9]{8,17}', self.service_security_group):
            raise AwsConfigurationError('SKY_AWS_SERVICE_SECURITY_GROUP에 유효한 보안 그룹 ID를 설정하세요.')

    def unavailable_reason(self):
        try:
            self.validate()
            if not shutil.which('aws'):
                return 'AWS CLI 설치와 로그인이 필요합니다.'
            if not shutil.which('docker'):
                return 'Docker CLI 설치가 필요합니다.'
        except AwsConfigurationError as exc:
            return str(exc)
        return None


class AwsExpressAdapter:
    @staticmethod
    def execution_capabilities() -> ExecutionCapabilities:
        declared = TARGET_CAPABILITIES['aws-ecs-express']
        return ExecutionCapabilities(
            target='aws-ecs-express',
            access_modes=frozenset(declared['access_modes']),
            postgresql_binding=declared['postgresql_binding'],
            remote_host=declared['remote_host'],
        )

    def __init__(self, event, settings: AwsSettings, existing=None, checkpoint=None,
                 rehearsal=False, promoted_image=None):
        if rehearsal and promoted_image is not None:
            raise ValueError('로컬 이미지 승격과 AWS 자체 리허설을 동시에 실행할 수 없습니다.')
        self.output, self.settings = event, settings
        self.existing = existing
        self.checkpoint = checkpoint
        self.sensitive = []
        self.service_arn = None
        self.service_created = False
        self.image = None
        self.image_pushed = False
        self.image_built = False
        self.updated_existing = False
        self.previous_deployment_arn = None
        self.rehearsal = rehearsal
        self.promoted_image = promoted_image
        self.rehearsal_result = None
        self.rehearsal_image = None
        self.rehearsal_image_built = False
        self.image_digest = None

    def event(self, stage, message):
        for value in sorted(set(self.sensitive), key=len, reverse=True):
            if value:
                message = message.replace(value, '[REDACTED]')
        self.output(stage, redact(message))

    def command(self, args, timeout=300, stdin=None, private=False, quiet=False):
        if not quiet:
            self.event('command', ' '.join(args))
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
        if not private and not quiet:
            if result.stdout:
                self.event('output', result.stdout[-12000:])
            if result.stderr:
                self.event('output', result.stderr[-12000:])
        if result.returncode:
            if args[0] == 'docker' and 'push' in args and any(
                    marker in (result.stderr + result.stdout).lower()
                    for marker in ('timeout awaiting response headers', 'i/o timeout', 'connection reset')):
                raise AwsConfigurationError('ECR 이미지 업로드 연결이 시간 초과됐습니다.')
            raise AwsConfigurationError(f'{args[0]} 명령 실패 (종료 코드 {result.returncode}). AWS 권한·리전·계정 설정을 확인하세요.')
        return result.stdout.strip()

    def aws(self, args, **kwargs):
        return self.command(['aws', *args, '--region', self.settings.region, '--no-cli-pager', '--output', 'json'], **kwargs)

    def validate_service_security_group(self):
        group_id = self.settings.service_security_group
        if not group_id:
            return
        self.settings.validate()
        vpcs = json.loads(self.aws(['ec2', 'describe-vpcs', '--filters', 'Name=isDefault,Values=true'],
                                   private=True, quiet=True)).get('Vpcs', [])
        if len(vpcs) != 1 or not re.fullmatch(r'vpc-[a-f0-9]{8,17}', vpcs[0].get('VpcId', '')):
            raise AwsConfigurationError('AWS 기본 VPC를 하나로 확인하지 못했습니다.')
        groups = json.loads(self.aws(['ec2', 'describe-security-groups', '--group-ids', group_id],
                                     private=True, quiet=True)).get('SecurityGroups', [])
        if (len(groups) != 1 or groups[0].get('GroupId') != group_id
                or groups[0].get('VpcId') != vpcs[0]['VpcId']
                or not service_group_ingress_is_restricted(groups[0])):
            raise AwsConfigurationError('추가 서비스 보안 그룹은 기본 VPC에 있어야 하며 인바운드는 단일 보안 그룹의 앱 포트만 허용해야 합니다.')
        self.event('infrastructure', '추가 ECS 서비스 보안 그룹의 VPC와 인바운드 규칙을 확인했습니다.')

    def prepare_infrastructure(self):
        self.settings.validate()
        if not shutil.which('aws') or not shutil.which('docker'):
            raise AwsConfigurationError('AWS CLI와 Docker CLI가 필요합니다.')
        account = json.loads(self.aws(['sts', 'get-caller-identity'], private=True)).get('Account', '')
        if not re.fullmatch(r'\d{12}', account):
            raise AwsConfigurationError('AWS 계정 ID를 확인하지 못했습니다.')
        if self.settings.expected_account and account != self.settings.expected_account:
            raise AwsConfigurationError('현재 AWS 계정이 SKY_AWS_ACCOUNT_ID와 다릅니다. 리소스를 생성하지 않았습니다.')
        boundary = managed_role_boundary(account)
        template = ASSET_ROOT / 'infra' / 'aws-ecs-express.yaml'
        self.event('infrastructure', 'CloudFormation으로 ECR 저장소와 ECS Express 역할 준비')
        command = ['cloudformation', 'deploy', '--template-file', str(template),
                   '--stack-name', self.settings.stack_name, '--capabilities', 'CAPABILITY_IAM',
                   '--tags', 'sky-managed=true']
        if boundary:
            command.extend(['--parameter-overrides', f'RoleBoundaryArn={boundary}'])
        self.aws(command, timeout=900)
        described = json.loads(self.aws(['cloudformation', 'describe-stacks', '--stack-name', self.settings.stack_name], private=True))
        stacks = described.get('Stacks', [])
        if len(stacks) != 1 or stacks[0].get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
            raise AwsConfigurationError('Sky 기반 스택이 준비 상태가 아닙니다.')
        outputs = {item['OutputKey']: item['OutputValue'] for item in stacks[0].get('Outputs', [])}
        repository = outputs.get('RepositoryUri', '')
        execution = outputs.get('ExecutionRoleArn', '')
        infrastructure = outputs.get('InfrastructureRoleArn', '')
        if repository != f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/sky-managed':
            raise AwsConfigurationError('ECR 저장소 URI가 예상과 다릅니다.')
        if not re.fullmatch(r'arn:aws:iam::' + account + r':role/[A-Za-z0-9_+=,.@/-]+', execution):
            raise AwsConfigurationError('ECS 실행 역할 ARN이 예상과 다릅니다.')
        if not re.fullmatch(r'arn:aws:iam::' + account + r':role/[A-Za-z0-9_+=,.@/-]+', infrastructure):
            raise AwsConfigurationError('ECS 인프라 역할 ARN이 예상과 다릅니다.')
        return account, repository, execution, infrastructure

    def deploy(self, project, plan, attempt_id, environment=None, postgres=None, migrations=None):
        self.settings.validate()
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise ValueError('Invalid deployment attempt ID')
        if plan.target != 'aws-ecs-express':
            raise ValueError('AWS ECS Express adapter requires an aws-ecs-express plan')
        if self.promoted_image is not None:
            candidate = self.promoted_image
            owner = candidate.get('attempt_id') if isinstance(candidate, dict) else None
            expected = f'sky/{owner}:latest'
            if (not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner)
                    or candidate.get('image') != expected
                    or candidate.get('platform') != 'linux/amd64'
                    or not isinstance(candidate.get('image_id'), str)
                    or not re.fullmatch(r'sha256:[a-f0-9]{64}', candidate['image_id'])
                    or not plan.source_digest or source_digest(project) != plan.source_digest):
                raise AwsConfigurationError('로컬 검증 이미지의 출처 또는 소스 무결성을 확인할 수 없습니다.')
            inspected = json.loads(self.command(['docker', 'image', 'inspect', expected],
                                                timeout=30, private=True))
            if (len(inspected) != 1 or inspected[0].get('Id') != candidate['image_id']
                    or expected not in (inspected[0].get('RepoTags') or [])
                    or (inspected[0].get('Config', {}).get('Labels') or {}).get('app') != 'sky'
                    or inspected[0].get('Os') != 'linux'
                    or inspected[0].get('Architecture') != 'amd64'):
                raise AwsConfigurationError('로컬 검증 이미지가 변경됐거나 linux/amd64 이미지가 아닙니다.')
        if migrations is not None and postgres is None:
            raise ValueError('SQL 마이그레이션에는 검증된 PostgreSQL 연결 요청이 필요합니다.')
        database = None
        if postgres is not None:
            from adapters.aws.postgres import AwsPostgresProvisioner, MANAGED_POSTGRES_ENV, PostgresRequest
            if not isinstance(postgres, PostgresRequest):
                raise ValueError('PostgreSQL 연결 요청이 올바르지 않습니다.')
            postgres.validate()
            if (self.settings.expected_account != postgres.account
                    or self.settings.region != postgres.region
                    or self.settings.service_security_group != postgres.service_security_group):
                raise AwsConfigurationError('PostgreSQL 계정·리전·서비스 보안 그룹이 AWS 배포 대상과 다릅니다.')
            managed_names = MANAGED_POSTGRES_ENV
            if managed_names.intersection(environment or {}):
                raise ValueError('PostgreSQL 접속 환경변수는 배포 시스템이 설정합니다.')
            environment = validate_environment(environment, [name for name in plan.required_env
                                                               if name not in managed_names])
            database = AwsPostgresProvisioner(postgres).inspect_current()
        else:
            environment = validate_environment(environment, plan.required_env)
        reject_supplied_secrets_in_source(project, environment)
        reject_plaintext_cloud_secrets(environment, 'aws-ecs-express')
        check_browser_client_urls(environment, 'aws-ecs-express')
        if self.existing is not None and self.existing.get('database') != database:
            raise AwsConfigurationError('기존 AWS 서비스의 PostgreSQL 연결 구성이 배포 기록과 다릅니다.')
        self.sensitive.extend(environment.values())
        self.validate_service_security_group()
        migration_runner = None
        if migrations is not None:
            from adapters.aws.migrations import AwsMigrationRunner
            from adapters.database.migrations import MigrationBundle
            if not isinstance(migrations, MigrationBundle):
                raise ValueError('SQL 마이그레이션 묶음이 올바르지 않습니다.')
            migration_runner = AwsMigrationRunner(self, postgres, database, migrations,
                f'{postgres.account}.dkr.ecr.{postgres.region}.amazonaws.com/sky-managed', attempt_id)
            migration_runner.preflight()
        if self.rehearsal:
            if database is not None:
                raise ValueError('PostgreSQL 앱의 로컬 리허설은 별도 DB 경로가 필요합니다.')
            self.rehearsal_image = f'sky/rehearsal-{attempt_id}:latest'
            ImageBuilder(self.command, self.event).build(
                project, plan, self.rehearsal_image, platform='linux/amd64')
            self.rehearsal_image_built = True
            self.rehearsal_result = rehearse_image(
                self.command, self.event, self.rehearsal_image, plan, attempt_id, environment)
        account, repository, execution, infrastructure = self.prepare_infrastructure()
        if migration_runner is not None and repository != migration_runner.repository:
            raise AwsConfigurationError('마이그레이션 ECR 저장소가 AWS 기반 스택 결과와 다릅니다.')
        self.image = f'{repository}:{attempt_id}'
        owner_attempt = attempt_id
        service = f'sky-{attempt_id}'
        previous_images = []
        previous_deployment = None
        previous_task_definition = None
        if self.existing is not None:
            prior = self.existing
            owner_attempt = prior.get('owner_attempt') or prior.get('service', '').removeprefix('sky-')
            service = f'sky-{owner_attempt}'
            expected_arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
            if (not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner_attempt)
                    or prior.get('service') != service or prior.get('service_arn') != expected_arn
                    or prior.get('account') != account or prior.get('region') != self.settings.region
                    or prior.get('target') != 'aws-ecs-express'
                    or prior.get('service_security_group', '') != self.settings.service_security_group):
                raise AwsConfigurationError('기존 AWS 배포의 리소스 정보가 예상과 다릅니다.')
            self.validate_url(prior.get('url', ''), service, self.settings.region)
            described = json.loads(self.aws(['ecs', 'describe-express-gateway-service',
                                              '--service-arn', expected_arn, '--include', 'TAGS'], private=True))['service']
            tags = {item['key']: item['value'] for item in described.get('tags', [])}
            images = [config.get('primaryContainer', {}).get('image')
                      for config in described.get('activeConfigurations', [])]
            if (described.get('serviceArn') != expected_arn
                    or described.get('status', {}).get('statusCode') != 'ACTIVE'
                    or tags.get('sky-managed') != 'true'
                    or tags.get('sky-attempt') != owner_attempt
                    or prior.get('image') not in images):
                raise AwsConfigurationError('기존 AWS 서비스의 소유권 또는 실행 이미지가 변경됐습니다.')
            previous_configs = [config for config in described.get('activeConfigurations', [])
                                if config.get('primaryContainer', {}).get('image') == prior['image']]
            if len(previous_configs) != 1:
                raise AwsConfigurationError('이전 ECS 실행 구성을 하나로 확인할 수 없습니다.')
            if (self.settings.service_security_group and self.settings.service_security_group not in
                    previous_configs[0].get('networkConfiguration', {}).get('securityGroups', [])):
                raise AwsConfigurationError('기존 ECS 서비스의 보안 그룹 구성이 배포 기록과 다릅니다.')
            if not database_configuration_matches(previous_configs[0], database):
                raise AwsConfigurationError('기존 ECS 서비스의 PostgreSQL 연결 구성이 배포 기록과 다릅니다.')
            previous_task_definition = previous_configs[0].get('taskDefinitionArn')
            deployments = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                               '--service', service], private=True, quiet=True))
            items = deployments.get('serviceDeployments', [])
            latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
            previous_deployment = latest.get('serviceDeploymentArn')
            self.previous_deployment_arn = previous_deployment
            if not previous_deployment or latest.get('status') not in {'SUCCESSFUL', 'ROLLBACK_SUCCESSFUL'}:
                raise AwsConfigurationError('이전 ECS 배포가 아직 완료되지 않았습니다. 잠시 후 다시 시도하세요.')
            previous_images = prior.get('images') or [prior['image']]
            if not isinstance(previous_images, list) or prior['image'] not in previous_images:
                raise AwsConfigurationError('기존 이미지 이력이 올바르지 않습니다.')
            self.service_arn = expected_arn
        ca_bundle = None
        if database is not None:
            from adapters.database.migrations import trusted_rds_ca_bundle
            ca_bundle = trusted_rds_ca_bundle()
        if self.rehearsal_result or self.promoted_image:
            source_image = self.rehearsal_image if self.rehearsal_result else self.promoted_image['image']
            expected_image_id = (self.rehearsal_result or self.promoted_image)['image_id']
            self.command(['docker', 'tag', source_image, self.image], timeout=30, quiet=True)
            self.image_built = True
            if inspect_image_id(self.command, self.image) != expected_image_id:
                raise AwsConfigurationError('로컬 검증 이미지와 ECR 업로드 이미지의 구성 ID가 다릅니다.')
            if self.rehearsal_result:
                self.command(['docker', 'image', 'rm', self.rehearsal_image], timeout=30, quiet=True)
                self.rehearsal_image = None
            self.event('rehearsal', '로컬에서 검증한 동일 이미지를 ECR 태그로 승격했습니다.')
        else:
            ImageBuilder(self.command, self.event).build(project, plan, self.image,
                                                          platform='linux/amd64', extra_ca_bundle=ca_bundle)
            self.image_built = True
        registry = repository.split('/')[0]
        password = self.aws(['ecr', 'get-login-password'], private=True)
        if not password:
            raise AwsConfigurationError('ECR 로그인 암호를 받지 못했습니다.')
        self.sensitive.append(password)
        endpoint = os.getenv('DOCKER_HOST') or self.command(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], private=True)
        with tempfile.TemporaryDirectory(prefix='sky-aws-docker-auth-') as config:
            docker = ['docker', '--config', config, '--host', endpoint]
            self.command(docker + ['login', '--username', 'AWS', '--password-stdin', registry], stdin=password, private=True)
            self.image_pushed = True
            self.event('uploading', '이미지를 ECR에 업로드합니다.')
            for push_attempt in range(3):
                try:
                    self.command(docker + ['push', self.image], timeout=600)
                    break
                except AwsConfigurationError as exc:
                    if '시간 초과' not in str(exc) or push_attempt == 2:
                        raise
                    self.event('retry', f'ECR 업로드 시간 초과, {push_attempt + 2}/3 재시도')
                    time.sleep(5)
        if self.rehearsal_result or self.promoted_image:
            details = json.loads(self.aws(['ecr', 'describe-images', '--repository-name',
                                           'sky-managed', '--image-ids',
                                           'imageTag=' + attempt_id], private=True))
            images = details.get('imageDetails', [])
            digest = images[0].get('imageDigest') if len(images) == 1 else None
            if not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
                raise AwsConfigurationError('ECR 이미지 매니페스트 다이제스트를 확인할 수 없습니다.')
            self.image_digest = digest
            self.event('uploading', '리허설한 이미지의 ECR 매니페스트 다이제스트를 기록했습니다.')
        migration_result = migration_runner.build_and_run() if migration_runner else None
        payload = {'healthCheckPath': plan.health_path,
                   'primaryContainer': {'image': self.image, 'containerPort': plan.port,
                                        'environment': [{'name': 'PORT', 'value': str(plan.port)}] +
                                                       [{'name': key, 'value': value} for key, value in environment.items()]}}
        if database is not None:
            payload['executionRoleArn'] = database['execution_role_arn']
            payload['primaryContainer']['environment'].extend([
                {'name': 'PGHOST', 'value': database['endpoint']},
                {'name': 'PGPORT', 'value': str(database['port'])},
                {'name': 'PGDATABASE', 'value': 'appdb'}]
                + postgres_ssl_environment(plan.runtime))
            payload['primaryContainer']['secrets'] = [
                {'name': 'PGUSER', 'valueFrom': database['secret_arn'] + ':username::'},
                {'name': 'PGPASSWORD', 'valueFrom': database['secret_arn'] + ':password::'}]
        if self.existing is None:
            payload.update({'serviceName': service, 'executionRoleArn': payload.get('executionRoleArn', execution),
                            'infrastructureRoleArn': infrastructure,
                            'scalingTarget': {'minTaskCount': 1, 'maxTaskCount': 1},
                            'tags': [{'key': 'sky-managed', 'value': 'true'},
                                     {'key': 'sky-attempt', 'value': attempt_id}]})
            if self.settings.service_security_group:
                payload['networkConfiguration'] = {
                    'securityGroups': [self.settings.service_security_group]}
        else:
            payload['serviceArn'] = self.service_arn
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-aws-service-', encoding='utf-8') as spec:
            json.dump(payload, spec)
            spec.flush()
            if self.existing is None:
                self.event('deploying', 'ECS Express Mode 서비스를 생성합니다.')
                self.service_arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
                self.service_created = True
                operation = 'create-express-gateway-service'
            else:
                self.event('deploying', '기존 ECS Express Mode 서비스에 새 리비전을 배포합니다.')
                operation = 'update-express-gateway-service'
            if self.existing is not None:
                if self.checkpoint:
                    self.checkpoint(aws_update_submitted=True,
                                    aws_update_submitted_at=datetime.now(timezone.utc).isoformat(),
                                    aws_previous_deployment_arn=self.previous_deployment_arn,
                                    aws_candidate_image=self.image)
                self.updated_existing = True
                self.event('update_submitting', 'AWS 기존 서비스 업데이트 명령을 호출합니다.')
            created = json.loads(self.aws(['ecs', operation, '--cli-input-json', 'file://' + spec.name],
                                         timeout=600, private=True))
        if created.get('service', {}).get('serviceArn') != self.service_arn:
            raise AwsConfigurationError('ECS 서비스 ARN이 예상과 다릅니다. 생성된 리소스를 확인하세요.')
        if self.updated_existing:
            self.event('update_accepted', 'AWS가 기존 서비스 업데이트 요청을 수락했습니다.')
        ready = None
        new_deployment = created.get('service', {}).get('currentDeployment')
        last_deployment_status = None
        for _ in range(360 if self.existing is not None else 180):
            described = json.loads(self.aws(['ecs', 'describe-express-gateway-service', '--service-arn', self.service_arn], private=True, quiet=True))
            ready = described.get('service', {})
            state = ready.get('status', {}).get('statusCode')
            paths = [path.get('endpoint') for config in ready.get('activeConfigurations', [])
                     for path in config.get('ingressPaths', [])
                     if path.get('accessType') == 'PUBLIC' and path.get('endpoint')
                     and (self.existing is None or config.get('primaryContainer', {}).get('image') == self.image)]
            deployment_ready = False
            deployment_arn = ready.get('currentDeployment') or new_deployment
            if not deployment_arn:
                deployments = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                                   '--service', service], private=True, quiet=True))
                items = deployments.get('serviceDeployments', [])
                latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
                deployment_arn = latest.get('serviceDeploymentArn')
            if deployment_arn and (self.existing is None or deployment_arn != previous_deployment):
                new_deployment = deployment_arn
                deployments = json.loads(self.aws(['ecs', 'describe-service-deployments',
                                                   '--service-deployment-arns', deployment_arn], private=True, quiet=True))
                items = deployments.get('serviceDeployments', [])
                deployment_status = items[0].get('status') if len(items) == 1 else None
                if deployment_status and deployment_status != last_deployment_status:
                    self.event('deploying', 'ECS 새 리비전 상태: ' + deployment_status)
                    last_deployment_status = deployment_status
                deployment_ready = deployment_status == 'SUCCESSFUL'
                if deployment_status in {'STOPPED', 'ROLLBACK_SUCCESSFUL', 'ROLLBACK_FAILED'}:
                    raise RuntimeError(f'ECS 새 리비전 배포에 실패했습니다: {deployment_status}')
            active = ready.get('activeConfigurations', [])
            settled = (not ready.get('currentDeployment') and len(active) == 1
                       and active[0].get('primaryContainer', {}).get('image') == self.image)
            if (settled and self.settings.service_security_group
                    and self.settings.service_security_group not in
                    active[0].get('networkConfiguration', {}).get('securityGroups', [])):
                raise AwsConfigurationError('ECS 서비스에 추가 보안 그룹이 적용되지 않았습니다.')
            if settled and not database_configuration_matches(
                    active[0], database, postgres_ssl_environment(plan.runtime) if database else None):
                raise AwsConfigurationError('ECS 서비스의 PostgreSQL 역할·비밀·접속 설정이 예상과 다릅니다.')
            if state == 'ACTIVE' and paths and deployment_ready and settled:
                break
            if state in {'FAILED', 'INACTIVE'}:
                raise RuntimeError('ECS Express 서비스 준비에 실패했습니다: ' + ready.get('status', {}).get('statusReason', '')[:300])
            time.sleep(5)
        else:
            raise RuntimeError('ECS Express 서비스 준비 시간 제한을 초과했습니다.')
        endpoint = paths[0].rstrip('/')
        url = endpoint if endpoint.startswith('https://') else 'https://' + endpoint
        self.validate_url(url, service, self.settings.region)
        if self.existing is not None and url != self.existing['url'].rstrip('/'):
            raise AwsConfigurationError('기존 ECS 서비스의 공개 URL이 변경됐습니다.')
        self.event('verifying', f'ECS Express HTTPS URL에서 실제 HTTP 응답 확인: {url}')
        self.verify(url + plan.health_path)
        if self.image_digest:
            details = json.loads(self.aws(['ecr', 'describe-images', '--repository-name',
                                           'sky-managed', '--image-ids',
                                           'imageTag=' + attempt_id], private=True))
            images = details.get('imageDetails', [])
            if len(images) != 1 or images[0].get('imageDigest') != self.image_digest:
                raise AwsConfigurationError('배포 중 ECR 이미지 태그의 다이제스트가 변경됐습니다.')
        active_configs = [config for config in ready.get('activeConfigurations', [])
                          if config.get('primaryContainer', {}).get('image') == self.image]
        task_definition = active_configs[0].get('taskDefinitionArn') if len(active_configs) == 1 else None
        expected_task_prefix = f'arn:aws:ecs:{self.settings.region}:{account}:task-definition/'
        if (not isinstance(task_definition, str) or not task_definition.startswith(expected_task_prefix)
                or not re.fullmatch(r'[A-Za-z0-9_-]+:\d+', task_definition.removeprefix(expected_task_prefix))):
            raise AwsConfigurationError('배포된 ECS 태스크 정의 ARN을 확인할 수 없습니다.')
        return {'url': url, 'health_url': url + plan.health_path, 'service': service,
                'service_arn': self.service_arn, 'image': self.image, 'target': 'aws-ecs-express',
                'region': self.settings.region, 'account': account, 'public': True,
                'service_security_group': self.settings.service_security_group,
                'owner_attempt': owner_attempt, 'images': [*previous_images, self.image],
                **({'rehearsal': self.rehearsal_result} if self.rehearsal_result else {}),
                **({'promotion': self.promoted_image} if self.promoted_image else {}),
                **({'image_digest': self.image_digest} if self.image_digest else {}),
                **({'database': database} if database is not None else {}),
                **({'migration': migration_result} if migration_result is not None else {}),
                'task_definition_arn': task_definition,
                'previous_task_definition_arn': previous_task_definition}

    @staticmethod
    def validate_url(url, service, region):
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ''
        label = hostname.removesuffix(f'.ecs.{region}.on.aws')
        if (parsed.scheme != 'https' or hostname == label
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', label)
                or parsed.username or parsed.password or parsed.port or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise AwsConfigurationError('ECS Express가 예상하지 못한 서비스 URL을 반환했습니다.')

    def verify(self, url):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        last_error = '응답 없음'
        for attempt in range(300):
            try:
                with opener.open(url, timeout=5) as response:
                    if response.status == 200:
                        return
                    last_error = f'HTTP {response.status}'
            except urllib.error.HTTPError as exc:
                last_error = f'HTTP {exc.code}'
            except urllib.error.URLError as exc:
                last_error = str(exc)[:200]
                if isinstance(exc.reason, socket.gaierror):
                    status = self.probe_with_dns_fallback(url)
                    if status == 200:
                        return
                    if status is not None:
                        last_error = f'HTTP {status}'
            except OSError as exc:
                last_error = str(exc)[:200]
            if attempt % 10 == 9:
                self.event('verifying', f'공개 주소 준비 대기 중 ({attempt + 1}/300): {last_error}')
            time.sleep(3)
        raise RuntimeError(f'ECS Express 앱이 HTTP 200을 반환하지 않았습니다: {last_error}')

    @staticmethod
    def probe_with_dns_fallback(url):
        """Bypass stale local DNS only; keep TLS SNI and hostname validation."""
        if not shutil.which('dig'):
            return None
        parsed = urllib.parse.urlsplit(url)
        addresses = []
        for resolver in (None, '@1.1.1.1', '@8.8.8.8'):
            args = ['dig', '+short', '+time=2', '+tries=1']
            if resolver:
                args.append(resolver)
            try:
                answer = subprocess.run([*args, parsed.hostname, 'A'], capture_output=True,
                                        text=True, timeout=5, check=True)
            except (OSError, subprocess.SubprocessError):
                continue
            for line in answer.stdout.splitlines():
                try:
                    address = ipaddress.ip_address(line.strip())
                except ValueError:
                    continue
                if address.version == 4 and address.is_global:
                    addresses.append(str(address))
            if addresses:
                break
        last_status = None
        for address in addresses[:4]:
            connection = http.client.HTTPSConnection(parsed.hostname, timeout=5, context=ssl.create_default_context())
            connection._create_connection = lambda _target, timeout, source_address=None: socket.create_connection(
                (address, 443), timeout, source_address)
            try:
                connection.request('GET', parsed.path or '/')
                last_status = connection.getresponse().status
                if last_status == 200:
                    return 200
            except (OSError, ssl.SSLError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        return last_status

    def cleanup_failure(self, attempt_id):
        service_inactive = not self.service_created
        if self.service_created and self.service_arn:
            try:
                if (not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id)
                        or not re.fullmatch(r'arn:aws:ecs:' + re.escape(self.settings.region)
                                            + r':\d{12}:service/default/sky-' + re.escape(attempt_id),
                                            self.service_arn)):
                    raise AwsConfigurationError('정리할 ECS 서비스 ARN이 예상과 다릅니다.')
                def describe_owned():
                    described = json.loads(self.aws(['ecs', 'describe-express-gateway-service',
                        '--service-arn', self.service_arn, '--include', 'TAGS'], private=True))['service']
                    tags = {item.get('key'): item.get('value') for item in described.get('tags', [])}
                    if (described.get('serviceArn') != self.service_arn
                            or tags.get('sky-managed') != 'true'
                            or tags.get('sky-attempt') != attempt_id):
                        raise AwsConfigurationError('정리할 ECS 서비스의 소유권을 확인할 수 없습니다.')
                    return described
                described = describe_owned()
                status = described.get('status', {}).get('statusCode')
                if status == 'ACTIVE':
                    self.aws(['ecs', 'delete-express-gateway-service', '--service-arn', self.service_arn], timeout=300)
                elif status not in {'DRAINING', 'INACTIVE'}:
                    raise AwsConfigurationError('정리할 ECS 서비스 상태를 확인할 수 없습니다.')
                for _ in range(180):
                    if describe_owned().get('status', {}).get('statusCode') == 'INACTIVE':
                        service_inactive = True
                        break
                    time.sleep(5)
                if not service_inactive:
                    self.event('cleanup', 'ECS 서비스가 아직 종료되지 않아 ECR 이미지를 보존합니다: ' + self.service_arn)
            except Exception:
                self.event('cleanup', 'ECS Express 서비스 정리 확인 필요: ' + self.service_arn)
        if self.updated_existing and self.image:
            self.event('cleanup', '기존 ECS 서비스의 롤백 확인 전 새 ECR 이미지를 보존합니다: ' + self.image)
        elif self.image_pushed and self.image:
            if not service_inactive:
                self.event('cleanup', 'ECS 서비스 종료 확인 전 ECR 이미지를 보존합니다: ' + self.image)
            else:
                try:
                    if (not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id)
                            or not re.fullmatch(r'\d{12}\.dkr\.ecr\.' + re.escape(self.settings.region)
                                                + r'\.amazonaws\.com/sky-managed:' + re.escape(attempt_id),
                                                self.image)):
                        raise AwsConfigurationError('정리할 ECR 이미지가 예상과 다릅니다.')
                    deleted = json.loads(self.aws(['ecr', 'batch-delete-image', '--repository-name',
                        'sky-managed', '--image-ids', 'imageTag=' + attempt_id], timeout=60))
                    failures = deleted.get('failures', [])
                    if failures and not all(item.get('failureCode') == 'ImageNotFound' for item in failures):
                        raise RuntimeError('ECR 이미지 태그 삭제 결과를 확인할 수 없습니다.')
                except Exception:
                    self.event('cleanup', 'ECR 이미지 정리 확인 필요: ' + self.image)
        if self.image_built and self.image:
            try:
                self.command(['docker', 'image', 'rm', self.image], timeout=30)
            except Exception:
                self.event('cleanup', '로컬 이미지 정리 확인 필요: ' + self.image)
        if self.rehearsal_image_built and self.rehearsal_image:
            try:
                self.command(['docker', 'image', 'rm', self.rehearsal_image], timeout=30)
            except Exception:
                self.event('cleanup', '로컬 리허설 이미지 정리 확인 필요: ' + self.rehearsal_image)

    def cleanup_abandoned_image(self, result, candidate_image, attempt_id):
        """Remove one failed-update tag only after the owned service is back on its prior image."""
        self.settings.validate()
        account = result.get('account', '')
        stored_service = result.get('service', '')
        owner_attempt = result.get('owner_attempt') or (
            stored_service.removeprefix('sky-') if isinstance(stored_service, str) else '')
        service = 'sky-' + owner_attempt
        arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
        repository = f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/sky-managed'
        if (not isinstance(account, str) or not re.fullmatch(r'\d{12}', account)
                or not isinstance(owner_attempt, str)
                or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner_attempt)
                or not isinstance(attempt_id, str)
                or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id)
                or stored_service != service or result.get('service_arn') != arn
                or result.get('region') != self.settings.region or result.get('target') != 'aws-ecs-express'
                or not isinstance(result.get('image'), str)
                or not re.fullmatch(re.escape(repository) + r':[a-f0-9]{16}-a[1-3]', result['image'])
                or candidate_image != repository + ':' + attempt_id or candidate_image == result['image']):
            raise AwsConfigurationError('정리할 AWS 이미지 또는 서비스 정보가 예상과 다릅니다.')
        self.validate_url(result.get('url', ''), service, self.settings.region)
        current_account = json.loads(self.aws(['sts', 'get-caller-identity'], private=True, quiet=True)).get('Account')
        if current_account != account:
            raise AwsConfigurationError('현재 AWS 계정이 배포 계정과 다릅니다.')
        described = json.loads(self.aws(['ecs', 'describe-express-gateway-service', '--service-arn', arn,
                                         '--include', 'TAGS'], private=True, quiet=True)).get('service', {})
        tags = {item.get('key'): item.get('value') for item in described.get('tags', [])}
        active_images = {configuration.get('primaryContainer', {}).get('image')
                         for configuration in described.get('activeConfigurations', [])}
        if (described.get('serviceArn') != arn or described.get('status', {}).get('statusCode') != 'ACTIVE'
                or described.get('currentDeployment') or active_images != {result['image']}
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-attempt') != owner_attempt):
            raise AwsConfigurationError('기존 이미지의 단독 실행과 ECS 서비스 소유권을 확인할 수 없습니다.')
        deployments = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                           '--service', service], private=True, quiet=True))
        items = deployments.get('serviceDeployments', [])
        latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
        if latest.get('status') not in {'SUCCESSFUL', 'ROLLBACK_SUCCESSFUL'}:
            raise AwsConfigurationError('ECS 배포가 아직 완료되지 않아 이미지를 삭제하지 않았습니다.')
        deleted = json.loads(self.aws(['ecr', 'batch-delete-image', '--repository-name', 'sky-managed',
                                      '--image-ids', 'imageTag=' + attempt_id], private=True, timeout=60))
        failures = deleted.get('failures', [])
        if failures and not all(item.get('failureCode') == 'ImageNotFound' for item in failures):
            raise RuntimeError('실패한 ECR 이미지 태그를 삭제하지 못했습니다.')
        return {'image': candidate_image, 'state': 'deleted'}

    def request_update_rollback(self, result, candidate_image, previous_deployment_arn, attempt_id):
        """Stop only this owned, ongoing ECS deployment after checking both revision images."""
        self.settings.validate()
        account = result.get('account', '')
        owner_attempt = result.get('owner_attempt') or result.get('service', '').removeprefix('sky-')
        service = 'sky-' + owner_attempt
        service_arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
        repository = f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/sky-managed'
        deployment_prefix = f'arn:aws:ecs:{self.settings.region}:{account}:service-deployment/default/{service}/'
        revision_prefix = f'arn:aws:ecs:{self.settings.region}:{account}:service-revision/default/{service}/'
        if (not isinstance(account, str) or not re.fullmatch(r'\d{12}', account)
                or not isinstance(owner_attempt, str)
                or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner_attempt)
                or not isinstance(attempt_id, str)
                or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id)
                or result.get('service') != service or result.get('service_arn') != service_arn
                or result.get('target') != 'aws-ecs-express' or result.get('region') != self.settings.region
                or not isinstance(result.get('image'), str)
                or not re.fullmatch(re.escape(repository) + r':[a-f0-9]{16}-a[1-3]', result['image'])
                or candidate_image != repository + ':' + attempt_id
                or not isinstance(previous_deployment_arn, str)
                or not previous_deployment_arn.startswith(deployment_prefix)):
            raise AwsConfigurationError('롤백할 AWS 배포 정보가 예상과 다릅니다.')
        self.validate_url(result.get('url', ''), service, self.settings.region)
        account_now = json.loads(self.aws(['sts', 'get-caller-identity'], private=True, quiet=True)).get('Account')
        if account_now != account:
            raise AwsConfigurationError('현재 AWS 계정이 배포 계정과 다릅니다.')
        service_data = json.loads(self.aws(['ecs', 'describe-express-gateway-service',
                                            '--service-arn', service_arn, '--include', 'TAGS'],
                                           private=True, quiet=True)).get('service', {})
        tags = {item.get('key'): item.get('value') for item in service_data.get('tags', [])}
        if (service_data.get('serviceArn') != service_arn
                or service_data.get('status', {}).get('statusCode') != 'ACTIVE'
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-attempt') != owner_attempt):
            raise AwsConfigurationError('ECS 서비스 소유권을 확인할 수 없습니다.')
        listed = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                      '--service', service], private=True, quiet=True))
        items = listed.get('serviceDeployments', [])
        latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
        deployment_arn = latest.get('serviceDeploymentArn', '')
        status = latest.get('status')
        if (not isinstance(deployment_arn, str) or not deployment_arn.startswith(deployment_prefix)
                or deployment_arn == previous_deployment_arn
                or service_data.get('currentDeployment') != deployment_arn
                or status not in {'PENDING', 'IN_PROGRESS', 'ROLLBACK_REQUESTED', 'ROLLBACK_IN_PROGRESS'}):
            raise AwsConfigurationError('롤백할 새 ECS 배포가 진행 중인지 확인할 수 없습니다.')
        details = json.loads(self.aws(['ecs', 'describe-service-deployments',
                                       '--service-deployment-arns', deployment_arn], private=True, quiet=True))
        deployments = details.get('serviceDeployments', [])
        if len(deployments) != 1 or deployments[0].get('serviceArn') != service_arn:
            raise AwsConfigurationError('새 ECS 배포의 서비스 소유권을 확인할 수 없습니다.')
        detail = deployments[0]
        sources = detail.get('sourceServiceRevisions', [])
        target_arn = detail.get('targetServiceRevision', {}).get('arn')
        source_arn = sources[0].get('arn') if len(sources) == 1 else None
        if (detail.get('serviceDeploymentArn') != deployment_arn or detail.get('status') != status
                or not isinstance(target_arn, str) or not target_arn.startswith(revision_prefix)
                or not isinstance(source_arn, str) or not source_arn.startswith(revision_prefix)
                or source_arn == target_arn):
            raise AwsConfigurationError('이전·새 ECS 서비스 리비전을 확인할 수 없습니다.')
        revisions = json.loads(self.aws(['ecs', 'describe-service-revisions',
                                         '--service-revision-arns', source_arn, target_arn],
                                        private=True, quiet=True)).get('serviceRevisions', [])
        by_arn = {item.get('serviceRevisionArn'): item for item in revisions}
        if set(by_arn) != {source_arn, target_arn} or any(
                item.get('serviceArn') != service_arn for item in revisions):
            raise AwsConfigurationError('ECS 서비스 리비전의 소유권을 확인할 수 없습니다.')
        for revision_arn, expected_image in ((source_arn, result['image']), (target_arn, candidate_image)):
            task_definition = by_arn[revision_arn].get('taskDefinition', '')
            if not isinstance(task_definition, str) or not re.fullmatch(
                    rf'arn:aws:ecs:{re.escape(self.settings.region)}:{account}:task-definition/[A-Za-z0-9_-]+:\d+',
                    task_definition):
                raise AwsConfigurationError('ECS 태스크 정의 ARN이 예상과 다릅니다.')
            task = json.loads(self.aws(['ecs', 'describe-task-definition', '--task-definition', task_definition],
                                       private=True, quiet=True)).get('taskDefinition', {})
            containers = task.get('containerDefinitions', [])
            if (task.get('taskDefinitionArn') != task_definition or len(containers) != 1
                    or containers[0].get('name') != 'Main'
                    or containers[0].get('image') != expected_image):
                raise AwsConfigurationError('ECS 롤백 대상 이미지가 작업 기록과 다릅니다.')
        if status in {'ROLLBACK_REQUESTED', 'ROLLBACK_IN_PROGRESS'}:
            return {'service_deployment_arn': deployment_arn, 'state': 'already_requested'}
        stopped = json.loads(self.aws(['ecs', 'stop-service-deployment', '--service-deployment-arn', deployment_arn,
                                       '--stop-type', 'ROLLBACK'], timeout=60, private=True))
        if stopped.get('serviceDeploymentArn') != deployment_arn:
            raise AwsConfigurationError('AWS 롤백 요청의 배포 ARN이 예상과 다릅니다.')
        return {'service_deployment_arn': deployment_arn, 'state': 'requested'}

    def rollback_release(self, current, previous, health_path, checkpoint=None):
        """Redeploy the previous managed task definition on the same Express service."""
        self.settings.validate()
        account = current.get('account', '')
        owner_attempt = current.get('owner_attempt') or current.get('service', '').removeprefix('sky-')
        service = 'sky-' + owner_attempt
        arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
        repository = f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/sky-managed'
        task_prefix = f'arn:aws:ecs:{self.settings.region}:{account}:task-definition/'
        task_arn = previous.get('task_definition_arn') or current.get('previous_task_definition_arn')
        if (not isinstance(account, str) or not re.fullmatch(r'\d{12}', account)
                or not isinstance(owner_attempt, str)
                or not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner_attempt)
                or current.get('service') != service or current.get('service_arn') != arn
                or current.get('region') != self.settings.region or current.get('target') != 'aws-ecs-express'
                or any(previous.get(key) != current.get(key)
                       for key in ('service', 'service_arn', 'region', 'account', 'target', 'url'))
                or not isinstance(current.get('image'), str) or not isinstance(previous.get('image'), str)
                or not re.fullmatch(re.escape(repository) + r':[a-f0-9]{16}-a[1-3]', current['image'])
                or not re.fullmatch(re.escape(repository) + r':[a-f0-9]{16}-a[1-3]', previous['image'])
                or previous['image'] == current['image']
                or previous['image'] not in (current.get('images') or [])
                or not isinstance(task_arn, str) or not task_arn.startswith(task_prefix)
                or not re.fullmatch(r'[A-Za-z0-9_-]+:\d+', task_arn.removeprefix(task_prefix))
                or not isinstance(health_path, str) or not health_path.startswith('/')
                or '?' in health_path or '#' in health_path):
            raise AwsConfigurationError('이전 AWS 릴리스의 롤백 정보가 예상과 다릅니다.')
        self.validate_url(current.get('url', ''), service, self.settings.region)
        caller = json.loads(self.aws(['sts', 'get-caller-identity'], private=True, quiet=True)).get('Account')
        if caller != account:
            raise AwsConfigurationError('현재 AWS 계정이 배포 계정과 다릅니다.')
        described = json.loads(self.aws(['ecs', 'describe-express-gateway-service',
                                         '--service-arn', arn, '--include', 'TAGS'], private=True, quiet=True))['service']
        tags = {item.get('key'): item.get('value') for item in described.get('tags', [])}
        configs = described.get('activeConfigurations', [])
        if (described.get('serviceArn') != arn or described.get('status', {}).get('statusCode') != 'ACTIVE'
                or described.get('currentDeployment') or len(configs) != 1
                or configs[0].get('primaryContainer', {}).get('image') != current['image']
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-attempt') != owner_attempt):
            raise AwsConfigurationError('현재 ECS 릴리스의 소유권과 완료 상태를 확인할 수 없습니다.')
        task = json.loads(self.aws(['ecs', 'describe-task-definition', '--task-definition', task_arn],
                                   private=True, quiet=True)).get('taskDefinition', {})
        containers = task.get('containerDefinitions', [])
        if (task.get('taskDefinitionArn') != task_arn or len(containers) != 1
                or containers[0].get('name') != 'Main'
                or containers[0].get('image') != previous['image']):
            raise AwsConfigurationError('이전 태스크 정의의 이미지가 릴리스 기록과 다릅니다.')
        listed = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                      '--service', service], private=True, quiet=True))
        items = listed.get('serviceDeployments', [])
        latest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
        previous_deployment_arn = latest.get('serviceDeploymentArn')
        if not previous_deployment_arn or latest.get('status') != 'SUCCESSFUL':
            raise AwsConfigurationError('현재 ECS 배포가 완료되지 않아 롤백을 시작하지 않았습니다.')
        payload = {'serviceArn': arn, 'taskDefinitionArn': task_arn, 'healthCheckPath': health_path}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-aws-rollback-', encoding='utf-8') as spec:
            json.dump(payload, spec)
            spec.flush()
            if checkpoint:
                checkpoint(release_rollback_submitted=True,
                           release_rollback_previous_deployment_arn=previous_deployment_arn,
                           release_rollback_submitted_at=datetime.now(timezone.utc).isoformat())
            self.event('rollback', '이전 ECS 태스크 정의를 같은 서비스에 배포합니다.')
            updated = json.loads(self.aws(['ecs', 'update-express-gateway-service',
                                           '--cli-input-json', 'file://' + spec.name], timeout=600, private=True))
        if updated.get('service', {}).get('serviceArn') != arn:
            raise AwsConfigurationError('롤백 요청의 ECS 서비스 ARN이 예상과 다릅니다.')
        new_deployment_arn = updated.get('service', {}).get('currentDeployment')
        for _ in range(360):
            service_data = json.loads(self.aws(['ecs', 'describe-express-gateway-service',
                                                '--service-arn', arn], private=True, quiet=True)).get('service', {})
            deployments = json.loads(self.aws(['ecs', 'list-service-deployments', '--cluster', 'default',
                                               '--service', service], private=True, quiet=True))
            items = deployments.get('serviceDeployments', [])
            newest = max(items, key=lambda item: item.get('createdAt', '')) if items else {}
            deployment_arn = newest.get('serviceDeploymentArn')
            if deployment_arn and deployment_arn != previous_deployment_arn:
                new_deployment_arn = deployment_arn
                if newest.get('status') in {'STOPPED', 'ROLLBACK_SUCCESSFUL', 'ROLLBACK_FAILED'}:
                    raise RuntimeError('이전 릴리스 재배포에 실패했습니다: ' + newest['status'])
                active = service_data.get('activeConfigurations', [])
                if (newest.get('status') == 'SUCCESSFUL'
                        and service_data.get('status', {}).get('statusCode') == 'ACTIVE'
                        and len(active) == 1
                        and active[0].get('primaryContainer', {}).get('image') == previous['image']
                        and active[0].get('taskDefinitionArn') == task_arn
                        and not service_data.get('currentDeployment')):
                    endpoints = [path.get('endpoint') for path in active[0].get('ingressPaths', [])
                                 if path.get('accessType') == 'PUBLIC']
                    url = endpoints[0].rstrip('/') if len(endpoints) == 1 and endpoints[0] else ''
                    if url and not url.startswith('https://'):
                        url = 'https://' + url
                    if url != current['url'].rstrip('/'):
                        raise AwsConfigurationError('롤백 후 ECS 공개 URL이 변경됐습니다.')
                    self.verify(url + health_path)
                    return {'service_deployment_arn': new_deployment_arn, 'url': url,
                            'image': previous['image'], 'state': 'successful'}
            time.sleep(5)
        raise RuntimeError('이전 ECS 릴리스 재배포 시간 제한을 초과했습니다.')

    def retire(self, result, attempt_id):
        """Delete one verified Sky service, then its image tag; keep the shared stack."""
        self.settings.validate()
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise AwsConfigurationError('배포 시도 ID가 올바르지 않습니다.')
        account = result.get('account', '')
        service = f'sky-{attempt_id}'
        arn = f'arn:aws:ecs:{self.settings.region}:{account}:service/default/{service}'
        repository = f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/sky-managed'
        image = result.get('image', '')
        images = result.get('images') or [image]
        valid_image = lambda item: isinstance(item, str) and re.fullmatch(
            re.escape(repository) + r':[a-f0-9]{16}-a[1-3]', item)
        if (not re.fullmatch(r'\d{12}', account) or result.get('service') != service
                or result.get('service_arn') != arn or result.get('owner_attempt', attempt_id) != attempt_id
                or not valid_image(image) or not isinstance(images, list) or image not in images
                or not all(valid_image(item) for item in images) or len(images) != len(set(images))
                or result.get('region') != self.settings.region
                or result.get('target') != 'aws-ecs-express'):
            raise AwsConfigurationError('작업 기록의 AWS 리소스 정보가 예상과 다릅니다.')
        self.validate_url(result.get('url', ''), service, self.settings.region)
        current_account = json.loads(self.aws(['sts', 'get-caller-identity'], private=True)).get('Account')
        if current_account != account:
            raise AwsConfigurationError('현재 AWS 계정이 배포 계정과 다릅니다.')

        def describe():
            response = json.loads(self.aws(['ecs', 'describe-express-gateway-service', '--service-arn', arn,
                                            '--include', 'TAGS'], private=True, quiet=True))
            service_data = response.get('service', {})
            tags = {item['key']: item['value'] for item in service_data.get('tags', [])}
            if (service_data.get('serviceArn') != arn or tags.get('sky-managed') != 'true'
                    or tags.get('sky-attempt') != attempt_id):
                raise AwsConfigurationError('ECS 서비스 소유권이 확인되지 않아 삭제를 중단했습니다.')
            return service_data

        service_data = describe()
        state = service_data.get('status', {}).get('statusCode')
        if state == 'ACTIVE':
            active_images = [configuration.get('primaryContainer', {}).get('image')
                             for configuration in service_data.get('activeConfigurations', [])]
            if image not in active_images:
                raise AwsConfigurationError('ECS 서비스 이미지가 배포 기록과 달라 삭제를 중단했습니다.')
            self.event('retiring', 'ECS Express 서비스를 종료합니다.')
            self.aws(['ecs', 'delete-express-gateway-service', '--service-arn', arn], timeout=300, private=True)
        elif state not in {'DRAINING', 'INACTIVE'}:
            raise AwsConfigurationError(f'ECS 서비스를 종료할 수 없는 상태입니다: {state}')

        for _ in range(180):
            service_data = describe()
            if service_data.get('status', {}).get('statusCode') == 'INACTIVE':
                break
            time.sleep(5)
        else:
            raise RuntimeError('ECS 서비스 종료가 아직 완료되지 않았습니다. 이미지 삭제는 보류했습니다.')
        self.event('retiring', 'ECR 이미지 태그를 삭제합니다.')
        for managed_image in images:
            tag = managed_image.rsplit(':', 1)[-1]
            deleted = json.loads(self.aws(['ecr', 'batch-delete-image', '--repository-name', 'sky-managed',
                                           '--image-ids', 'imageTag=' + tag], timeout=60, private=True))
            failures = deleted.get('failures', [])
            if failures and not all(item.get('failureCode') == 'ImageNotFound' for item in failures):
                raise RuntimeError('ECR 이미지 삭제에 실패했습니다. 태그를 확인하세요.')
        return {'service_arn': arn, 'image': image, 'state': 'deleted'}
