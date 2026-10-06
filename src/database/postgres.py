"""Opt-in, retained RDS PostgreSQL provisioning primitive for the AWS data path."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path

from assets import ASSET_ROOT
from deployment.aws import (AwsConfigurationError, AwsExpressAdapter, AwsSettings,
                           service_group_ingress_is_restricted)
from deployment.aws_pricing import estimate_postgres_base_capacity
from deployment.aws_network import AwsServiceNetworkProvisioner, ServiceNetworkRequest


TEMPLATE = ASSET_ROOT / 'infra' / 'aws-postgres.json'
MANAGED_POSTGRES_ENV = frozenset({'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD',
                                  'PGSSLMODE', 'PGSSLROOTCERT'})


def policy_document(value):
    """AWS CLI versions may return IAM policy documents as JSON or URL-encoded JSON."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(urllib.parse.unquote(value))
            return decoded if isinstance(decoded, dict) else {}
        except ValueError:
            pass
    return {}


@dataclass(frozen=True)
class PostgresRequest:
    application_id: str
    account: str
    region: str
    vpc_id: str
    subnet_ids: tuple[str, ...]
    service_security_group: str

    @property
    def stack_name(self) -> str:
        return 'sky-db-' + self.application_id

    @property
    def database_id(self) -> str:
        return 'sky-' + self.application_id

    def validate(self) -> None:
        if (not 3 <= len(self.application_id) <= 31
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', self.application_id)):
            raise ValueError('DB 앱 ID는 소문자로 시작하는 3–31자리 영문·숫자와 내부 하이픈이어야 합니다.')
        if not re.fullmatch(r'\d{12}', self.account):
            raise ValueError('AWS 계정 ID는 12자리 숫자여야 합니다.')
        if not re.fullmatch(r'vpc-[a-f0-9]{8,17}', self.vpc_id):
            raise ValueError('VPC ID가 올바르지 않습니다.')
        if (not 2 <= len(self.subnet_ids) <= 8 or len(set(self.subnet_ids)) != len(self.subnet_ids)
                or any(not re.fullmatch(r'subnet-[a-f0-9]{8,17}', subnet) for subnet in self.subnet_ids)):
            raise ValueError('서로 다른 DB 서브넷 ID를 2–8개 지정하세요.')
        AwsSettings(self.region, expected_account=self.account, account_pin_required=True,
                    service_security_group=self.service_security_group).validate()
        if not self.service_security_group:
            raise ValueError('ECS 태스크에 추가할 서비스 보안 그룹 ID가 필요합니다.')


def postgres_settings_for_application(application_id: str, vpc_id: str,
                                      settings: AwsSettings) -> AwsSettings:
    """Resolve a verified app-owned service group when no fixed group is configured."""
    settings.validate()
    if not settings.expected_account:
        raise AwsConfigurationError('PostgreSQL 경로에는 AWS 계정 ID를 서버에 고정해야 합니다.')
    if settings.service_security_group:
        return settings
    request = ServiceNetworkRequest(application_id, settings.expected_account,
                                    settings.region, vpc_id)
    network = AwsServiceNetworkProvisioner(request).inspect_current()
    return replace(settings, service_security_group=network['service_security_group'])


def discover_existing_postgres(application_id: str, settings: AwsSettings) -> dict:
    """Find only this app's database, then run the full read-only ownership audit."""
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', application_id or '') \
            or not 3 <= len(application_id) <= 31:
        raise ValueError('DB 앱 ID가 올바르지 않습니다.')
    settings.validate()
    if not settings.expected_account:
        raise AwsConfigurationError('PostgreSQL 경로에는 AWS 계정 ID를 서버에 고정해야 합니다.')
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    identity = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
    if identity.get('Account') != settings.expected_account:
        raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
    database_id = 'sky-' + application_id
    instances = json.loads(adapter.aws(['rds', 'describe-db-instances',
        '--db-instance-identifier', database_id], private=True, quiet=True)).get('DBInstances', [])
    if len(instances) != 1 or instances[0].get('DBInstanceIdentifier') != database_id:
        raise AwsConfigurationError('지정한 앱의 PostgreSQL 인스턴스를 확인하지 못했습니다.')
    subnet_group = instances[0].get('DBSubnetGroup', {})
    effective_settings = postgres_settings_for_application(
        application_id, subnet_group.get('VpcId', ''), settings)
    request = PostgresRequest(application_id, settings.expected_account, settings.region,
        subnet_group.get('VpcId', ''), tuple(item.get('SubnetIdentifier', '')
        for item in subnet_group.get('Subnets', [])), effective_settings.service_security_group)
    request.validate()
    database = AwsPostgresProvisioner(request).inspect_current()
    return {'database_id': database['database_id'], 'account': settings.expected_account,
            'region': settings.region, 'vpc_id': request.vpc_id,
            'subnet_ids': list(request.subnet_ids), 'engine_version': database['engine_version'],
            'status': database['status'], 'deletion_protection': database['deletion_protection'],
            'retained_on_stack_delete': database['retained_on_stack_delete']}


def inspect_postgres_backup_status(application_id: str, settings: AwsSettings) -> dict:
    """Report backup settings and same-account manual snapshots after the DB ownership audit."""
    database = discover_existing_postgres(application_id, settings)
    database_id = database['database_id']
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    instances = json.loads(adapter.aws(['rds', 'describe-db-instances',
        '--db-instance-identifier', database_id], private=True, quiet=True)).get('DBInstances', [])
    instance = instances[0] if len(instances) == 1 else {}
    expected_arn = f'arn:aws:rds:{settings.region}:{settings.expected_account}:db:{database_id}'
    retention = instance.get('BackupRetentionPeriod')
    restorable = instance.get('LatestRestorableTime')
    if (instance.get('DBInstanceIdentifier') != database_id
            or instance.get('DBInstanceArn') != expected_arn
            or not isinstance(retention, int) or isinstance(retention, bool)
            or not 0 <= retention <= 35
            or (restorable is not None and not isinstance(restorable, str))):
        raise AwsConfigurationError('PostgreSQL 백업 설정을 확인하지 못했습니다.')
    described = json.loads(adapter.aws(['rds', 'describe-db-snapshots',
        '--db-instance-identifier', database_id, '--snapshot-type', 'manual'],
        private=True, quiet=True))
    snapshots = described.get('DBSnapshots')
    if described.get('Marker') or not isinstance(snapshots, list):
        raise AwsConfigurationError('수동 스냅샷 목록을 완전히 확인하지 못했습니다.')
    prefix = f'arn:aws:rds:{settings.region}:{settings.expected_account}:snapshot:'
    results = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise AwsConfigurationError('수동 스냅샷 조회 결과가 올바르지 않습니다.')
        snapshot_id = snapshot.get('DBSnapshotIdentifier')
        if (snapshot.get('DBInstanceIdentifier') != database_id
                or snapshot.get('SnapshotType') != 'manual'
                or not isinstance(snapshot_id, str)
                or not re.fullmatch(r'[a-z][a-z0-9-]{0,254}', snapshot_id)
                or snapshot.get('DBSnapshotArn') != prefix + snapshot_id
                or not isinstance(snapshot.get('Status'), str)
                or (snapshot.get('SnapshotCreateTime') is not None
                    and not isinstance(snapshot.get('SnapshotCreateTime'), str))
                or not isinstance(snapshot.get('Encrypted'), bool)):
            raise AwsConfigurationError('수동 스냅샷의 계정·DB·상태가 예상과 다릅니다.')
        results.append({'snapshot_id': snapshot_id, 'status': snapshot['Status'],
                        'created_at': snapshot.get('SnapshotCreateTime'),
                        'encrypted': snapshot['Encrypted']})
    results.sort(key=lambda item: item['created_at'] or '', reverse=True)
    return {'application_id': application_id, 'database_id': database_id,
            'region': settings.region, 'account': settings.expected_account,
            'database_status': database['status'],
            'backup_retention_days': retention, 'latest_restorable_time': restorable,
            'deletion_protection': database['deletion_protection'],
            'retained_on_stack_delete': database['retained_on_stack_delete'],
            'manual_snapshot_count': len(results), 'manual_snapshots': results[:10]}


class AwsPostgresProvisioner:
    def __init__(self, request: PostgresRequest, event=lambda *_: None):
        request.validate()
        self.request = request
        self.adapter = AwsExpressAdapter(event, AwsSettings(
            request.region, expected_account=request.account, account_pin_required=True,
            service_security_group=request.service_security_group))

    def verify_service_group(self) -> None:
        """Require an app-owned group so another service cannot inherit DB access."""
        req = self.request
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups', '--group-ids',
                                              req.service_security_group], private=True,
                                             quiet=True)).get('SecurityGroups', [])
        group = groups[0] if len(groups) == 1 else {}
        tags = {item.get('Key'): item.get('Value') for item in group.get('Tags', [])}
        if (group.get('GroupId') != req.service_security_group
                or group.get('VpcId') != req.vpc_id
                or group.get('OwnerId') != req.account
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != req.application_id
                or not service_group_ingress_is_restricted(group)):
            raise AwsConfigurationError('서비스 보안 그룹은 지정한 계정·VPC의 앱 전용 소유 태그가 있어야 하며 인바운드는 단일 보안 그룹의 앱 포트만 허용해야 합니다.')

    def preflight(self) -> dict:
        """Read-only account and network checks; no DB or stack mutations."""
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        vpcs = json.loads(self.adapter.aws(['ec2', 'describe-vpcs', '--vpc-ids', req.vpc_id],
                                           private=True, quiet=True)).get('Vpcs', [])
        if len(vpcs) != 1 or vpcs[0].get('VpcId') != req.vpc_id or not vpcs[0].get('IsDefault'):
            raise AwsConfigurationError('ECS Express 공개 경로와 같은 기본 VPC가 필요합니다.')
        self.verify_service_group()
        subnets = json.loads(self.adapter.aws(['ec2', 'describe-subnets', '--subnet-ids', *req.subnet_ids],
                                              private=True, quiet=True)).get('Subnets', [])
        by_id = {item.get('SubnetId'): item for item in subnets}
        if (len(by_id) != len(req.subnet_ids) or set(by_id) != set(req.subnet_ids)
                or any(item.get('VpcId') != req.vpc_id or item.get('State') != 'available'
                       or not item.get('AvailabilityZone') for item in subnets)
                or len({item['AvailabilityZone'] for item in subnets}) < 2):
            raise AwsConfigurationError('DB 서브넷은 같은 VPC의 사용 가능한 서로 다른 가용 영역 두 곳 이상에 있어야 합니다.')
        versions = json.loads(self.adapter.aws(['rds', 'describe-db-engine-versions', '--engine',
            'postgres', '--default-only'], private=True, quiet=True)).get('DBEngineVersions', [])
        version = versions[0].get('EngineVersion') if len(versions) == 1 else None
        if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version)
                or versions[0].get('Engine') != 'postgres'):
            raise AwsConfigurationError('이 리전의 기본 PostgreSQL 엔진 버전을 확인하지 못했습니다.')
        orderable = json.loads(self.adapter.aws(['rds', 'describe-orderable-db-instance-options',
            '--engine', 'postgres', '--engine-version', version,
            '--db-instance-class', 'db.t4g.micro', '--vpc'],
            private=True, quiet=True)).get('OrderableDBInstanceOptions', [])
        zones = {item['AvailabilityZone'] for item in subnets}
        eligible_zones = sorted({az.get('Name')
            for option in orderable
            if option.get('Engine') == 'postgres'
            and option.get('EngineVersion') == version
            and option.get('DBInstanceClass') == 'db.t4g.micro'
            and option.get('StorageType') == 'gp3'
            and option.get('Vpc') is True
            and option.get('SupportsStorageEncryption') is True
            and isinstance(option.get('MinStorageSize'), int)
            and option['MinStorageSize'] <= 20
            and isinstance(option.get('MaxStorageSize'), int)
            and option['MaxStorageSize'] >= 20
            for az in option.get('AvailabilityZones', [])
            if az.get('Name') in zones})
        if not eligible_zones:
            raise AwsConfigurationError('이 리전·가용 영역에서 기본 PostgreSQL 버전의 암호화된 db.t4g.micro/gp3 20GiB 구성을 확인하지 못했습니다.')
        pricing = estimate_postgres_base_capacity(self.adapter, req.region)
        return {'account': req.account, 'region': req.region, 'vpc_id': req.vpc_id,
                'subnet_ids': list(req.subnet_ids), 'availability_zones': sorted({
                    item['AvailabilityZone'] for item in subnets}),
                'database_availability_zone': eligible_zones[0],
                'service_security_group': req.service_security_group,
                'stack_name': req.stack_name, 'database_id': req.database_id,
                'instance_class': 'db.t4g.micro', 'engine_version': version,
                'storage_type': 'gp3', 'storage_gib': 20,
                'extended_support': False,
                'pricing': pricing,
                'publicly_accessible': False, 'deletion_protection': True,
                'retained_on_stack_delete': True}

    def assert_database_absent(self) -> None:
        """Check the full account lists before reusing a cleaned application DB ID."""
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'],
                                              private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('AWS 계정이 PostgreSQL 생성 요청과 다릅니다.')
        databases = json.loads(self.adapter.aws(['rds', 'describe-db-instances'],
                                               private=True, quiet=True))
        instances = databases.get('DBInstances')
        if (databases.get('Marker') or not isinstance(instances, list)
                or any(not isinstance(item, dict)
                       or item.get('DBInstanceIdentifier') == req.database_id
                       for item in instances)):
            raise AwsConfigurationError('앱 DB가 없음을 완전히 확인하지 못했습니다.')
        snapshots = json.loads(self.adapter.aws(['rds', 'describe-db-snapshots',
            '--snapshot-type', 'manual'], private=True, quiet=True))
        items = snapshots.get('DBSnapshots')
        if (snapshots.get('Marker') or not isinstance(items, list)
                or any(not isinstance(item, dict)
                       or item.get('DBInstanceIdentifier') == req.database_id
                       for item in items)):
            raise AwsConfigurationError('앱 수동 스냅샷이 없음을 완전히 확인하지 못했습니다.')

    def assert_stack_available(self, *, allow_deleted: bool = False) -> None:
        """Fail closed when this deterministic stack name already appears in the account."""
        response = json.loads(self.adapter.aws(['cloudformation', 'list-stacks'],
                                               private=True, quiet=True))
        stacks = response.get('StackSummaries')
        if (not isinstance(stacks, list) or response.get('NextToken')
                or any(not isinstance(item, dict) for item in stacks)):
            raise AwsConfigurationError('CloudFormation 스택 목록을 완전히 확인하지 못했습니다.')
        if any(item.get('StackName') == self.request.stack_name and
               (not allow_deleted or item.get('StackStatus') != 'DELETE_COMPLETE')
               for item in stacks):
            raise AwsConfigurationError('이 앱 ID의 RDS 스택 기록이 이미 있습니다. 기존 DB 조회 또는 생성 결과 재확인을 사용하세요.')

    def create(self, expected_plan: dict | None = None) -> dict:
        """Create one new stack only; never update or auto-delete a database."""
        plan = self.preflight()
        if expected_plan is not None and plan != expected_plan:
            raise AwsConfigurationError('RDS 생성 계획이 변경됐습니다. 가격과 네트워크를 다시 확인하세요.')
        self.adapter.event('cost', 'RDS 기본 용량의 730시간 기준 공개 가격: '
                           + plan['pricing']['baseline_730h_usd']
                           + ' USD. 백업 초과·전송·비밀·로그·ECS·세금은 제외합니다.')
        req = self.request
        template = TEMPLATE.read_text(encoding='utf-8')
        payload = {'StackName': req.stack_name, 'TemplateBody': template,
                   'EnableTerminationProtection': True,
                   'Capabilities': ['CAPABILITY_IAM'],
                   'Parameters': [
                       {'ParameterKey': 'ApplicationId', 'ParameterValue': req.application_id},
                       {'ParameterKey': 'EngineVersion', 'ParameterValue': plan['engine_version']},
                       {'ParameterKey': 'AvailabilityZone',
                        'ParameterValue': plan['database_availability_zone']},
                       {'ParameterKey': 'VpcId', 'ParameterValue': req.vpc_id},
                       {'ParameterKey': 'SubnetIds', 'ParameterValue': ','.join(req.subnet_ids)},
                       {'ParameterKey': 'ServiceSecurityGroupId', 'ParameterValue': req.service_security_group}],
                   'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                            {'Key': 'sky-app', 'Value': req.application_id}]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='sky-rds-',
                                         encoding='utf-8') as spec:
            json.dump(payload, spec)
            spec.flush()
            created = json.loads(self.adapter.aws(['cloudformation', 'create-stack',
                '--cli-input-json', 'file://' + spec.name], timeout=60, private=True))
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        stack_id = created.get('StackId', '')
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('생성 요청의 CloudFormation 스택 ARN이 예상과 다릅니다. AWS 리소스를 직접 확인하세요.')
        try:
            self.adapter.aws(['cloudformation', 'wait', 'stack-create-complete', '--stack-name', stack_id],
                             timeout=3600, private=True, quiet=True)
            return self.inspect(stack_id)
        except Exception as exc:
            raise RuntimeError(f'PostgreSQL 생성 결과를 확인하지 못했습니다. {stack_id} 상태와 비용을 확인하세요: {exc}') from None

    def inspect_current(self) -> dict:
        """Read and verify the existing application database without changing resources."""
        req = self.request
        identity = json.loads(self.adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))
        if identity.get('Account') != req.account:
            raise AwsConfigurationError('현재 AWS 계정이 지정한 계정과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks',
            '--stack-name', req.stack_name], private=True, quiet=True)).get('Stacks', [])
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        if len(stacks) != 1 or not isinstance(stacks[0].get('StackId'), str) or not stacks[0]['StackId'].startswith(prefix):
            raise AwsConfigurationError('지정한 앱의 PostgreSQL 스택을 확인하지 못했습니다.')
        return self.inspect(stacks[0]['StackId'])

    def verify_execution_role(self, role_arn: str, secret_arn: str) -> None:
        """Reject a role whose trust or grants drifted beyond the managed secret."""
        req = self.request
        role_name = role_arn.rsplit('/', 1)[-1]
        role = json.loads(self.adapter.aws(['iam', 'get-role', '--role-name', role_name],
                                           private=True, quiet=True)).get('Role', {})
        tags = {item.get('Key'): item.get('Value') for item in role.get('Tags', [])}
        trust = policy_document(role.get('AssumeRolePolicyDocument'))
        trust_statements = trust.get('Statement', [])
        if isinstance(trust_statements, dict):
            trust_statements = [trust_statements]
        expected_trust = {'Effect': 'Allow', 'Principal': {'Service': 'ecs-tasks.amazonaws.com'},
                          'Action': 'sts:AssumeRole'}
        if (role.get('Arn') != role_arn or role.get('RoleName') != role_name
                or tags.get('sky-managed') != 'true'
                or tags.get('sky-app') != req.application_id
                or trust_statements != [expected_trust]):
            raise AwsConfigurationError('PostgreSQL ECS 실행 역할의 소유권 또는 신뢰 정책이 예상과 다릅니다.')
        managed = json.loads(self.adapter.aws(['iam', 'list-attached-role-policies', '--role-name', role_name],
                                              private=True, quiet=True))
        inline = json.loads(self.adapter.aws(['iam', 'list-role-policies', '--role-name', role_name],
                                             private=True, quiet=True))
        if (managed.get('IsTruncated') or inline.get('IsTruncated')
                or {item.get('PolicyArn') for item in managed.get('AttachedPolicies', [])}
                   != {'arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'}
                or inline.get('PolicyNames') != ['ReadManagedDatabaseSecret']):
            raise AwsConfigurationError('PostgreSQL ECS 실행 역할에 예상 밖의 정책이 연결됐습니다.')
        document = json.loads(self.adapter.aws(['iam', 'get-role-policy', '--role-name', role_name,
                                                '--policy-name', 'ReadManagedDatabaseSecret'],
                                               private=True, quiet=True))
        policy = policy_document(document.get('PolicyDocument'))
        expected_statement = {'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
                              'Resource': secret_arn}
        statements = policy.get('Statement', [])
        if isinstance(statements, dict):
            statements = [statements]
        if (document.get('RoleName') != role_name
                or document.get('PolicyName') != 'ReadManagedDatabaseSecret'
                or statements != [expected_statement]):
            raise AwsConfigurationError('PostgreSQL ECS 실행 역할의 비밀 조회 권한이 예상과 다릅니다.')

    def inspect(self, stack_id: str) -> dict:
        """Verify owned stack, nonpublic instance, network, and secret ARN; never read secret value."""
        req = self.request
        prefix = f'arn:aws:cloudformation:{req.region}:{req.account}:stack/{req.stack_name}/'
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError('PostgreSQL 스택 ARN이 예상과 다릅니다.')
        stacks = json.loads(self.adapter.aws(['cloudformation', 'describe-stacks', '--stack-name', stack_id],
                                             private=True, quiet=True)).get('Stacks', [])
        if len(stacks) != 1 or stacks[0].get('StackId') != stack_id or stacks[0].get('StackStatus') != 'CREATE_COMPLETE':
            raise AwsConfigurationError('PostgreSQL 스택이 생성 완료 상태가 아닙니다.')
        tags = {item.get('Key'): item.get('Value') for item in stacks[0].get('Tags', [])}
        outputs = {item.get('OutputKey'): item.get('OutputValue') for item in stacks[0].get('Outputs', [])}
        db_arn = f'arn:aws:rds:{req.region}:{req.account}:db:{req.database_id}'
        secret_arn = outputs.get('SecretArn', '')
        execution_role_arn = outputs.get('DatabaseExecutionRoleArn', '')
        group_id = outputs.get('DatabaseSecurityGroupId', '')
        requested_version = outputs.get('RequestedEngineVersion', '')
        if (tags.get('sky-managed') != 'true' or tags.get('sky-app') != req.application_id
                or outputs.get('DatabaseIdentifier') != req.database_id or outputs.get('DatabaseArn') != db_arn
                or not isinstance(requested_version, str)
                or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', requested_version)
                or not isinstance(secret_arn, str) or not secret_arn.startswith(
                    f'arn:aws:secretsmanager:{req.region}:{req.account}:secret:')
                or not isinstance(execution_role_arn, str) or not re.fullmatch(
                    rf'arn:aws:iam::{req.account}:role/[A-Za-z0-9_+=,.@/-]+', execution_role_arn)
                or not isinstance(group_id, str) or not re.fullmatch(r'sg-[a-f0-9]{8,17}', group_id)
                or not isinstance(outputs.get('EndpointAddress'), str)
                or not outputs['EndpointAddress'].endswith(f'.{req.region}.rds.amazonaws.com')
                or outputs.get('EndpointPort') != '5432'
                or outputs.get('MigrationLogGroupName') != f'/sky/migrations/{req.application_id}'):
            raise AwsConfigurationError('PostgreSQL 스택 출력 또는 소유 태그가 예상과 다릅니다.')
        instances = json.loads(self.adapter.aws(['rds', 'describe-db-instances', '--db-instance-identifier',
                                                 req.database_id], private=True, quiet=True)).get('DBInstances', [])
        if len(instances) != 1:
            raise AwsConfigurationError('PostgreSQL 인스턴스를 하나로 확인하지 못했습니다.')
        db = instances[0]
        attached = {item.get('VpcSecurityGroupId') for item in db.get('VpcSecurityGroups', [])}
        if (db.get('DBInstanceIdentifier') != req.database_id or db.get('DBInstanceArn') != db_arn
                or db.get('DBInstanceStatus') != 'available' or db.get('Engine') != 'postgres'
                or not isinstance(db.get('EngineVersion'), str)
                or db['EngineVersion'].split('.')[0] != requested_version.split('.')[0]
                or db.get('DBInstanceClass') != 'db.t4g.micro'
                or db.get('StorageType') != 'gp3' or db.get('AllocatedStorage') != 20
                or db.get('EngineLifecycleSupport') != 'open-source-rds-extended-support-disabled'
                or db.get('DBName') != 'appdb' or db.get('PubliclyAccessible') is not False
                or db.get('DeletionProtection') is not True or db.get('StorageEncrypted') is not True
                or db.get('DBSubnetGroup', {}).get('VpcId') != req.vpc_id
                or {item.get('SubnetIdentifier') for item in db.get('DBSubnetGroup', {}).get('Subnets', [])}
                   != set(req.subnet_ids)
                or attached != {group_id}
                or db.get('MasterUserSecret', {}).get('SecretArn') != secret_arn
                or db.get('Endpoint', {}).get('Address') != outputs['EndpointAddress']
                or db.get('Endpoint', {}).get('Port') != 5432):
            raise AwsConfigurationError('PostgreSQL 실제 인스턴스가 비공개·보호 구성과 다릅니다.')
        groups = json.loads(self.adapter.aws(['ec2', 'describe-security-groups', '--group-ids', group_id],
                                             private=True, quiet=True)).get('SecurityGroups', [])
        rules = groups[0].get('IpPermissions', []) if len(groups) == 1 else []
        rule = rules[0] if len(rules) == 1 else {}
        pairs = rule.get('UserIdGroupPairs', [])
        if (len(groups) != 1 or groups[0].get('GroupId') != group_id
                or groups[0].get('VpcId') != req.vpc_id
                or rule.get('IpProtocol') != 'tcp' or rule.get('FromPort') != 5432
                or rule.get('ToPort') != 5432 or len(pairs) != 1
                or pairs[0].get('GroupId') != req.service_security_group
                or pairs[0].get('UserId', req.account) != req.account
                or pairs[0].get('VpcId', req.vpc_id) != req.vpc_id
                or rule.get('IpRanges', []) or rule.get('Ipv6Ranges', [])
                or rule.get('PrefixListIds', [])):
            raise AwsConfigurationError('PostgreSQL 보안 그룹의 인바운드 허용 범위가 예상과 다릅니다.')
        self.verify_service_group()
        self.verify_execution_role(execution_role_arn, secret_arn)
        return {'stack_id': stack_id, 'database_arn': db_arn, 'database_id': req.database_id,
                'endpoint': outputs['EndpointAddress'], 'port': 5432,
                'engine_version': db['EngineVersion'], 'storage_type': 'gp3', 'storage_gib': 20,
                'extended_support': False,
                'secret_arn': secret_arn, 'database_security_group': group_id,
                'execution_role_arn': execution_role_arn,
                'migration_log_group': outputs['MigrationLogGroupName'],
                'service_security_group': req.service_security_group,
                'vpc_id': req.vpc_id, 'subnet_ids': list(req.subnet_ids),
                'status': 'available', 'deletion_protection': True, 'retained_on_stack_delete': True}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Provision a retained private RDS PostgreSQL instance')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Create a billable RDS instance and managed secret')
    mode.add_argument('--inspect', action='store_true', help='Verify an existing database without changes')
    parser.add_argument('--application', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    parser.add_argument('--subnet-id', action='append', required=True)
    parser.add_argument('--service-security-group', required=True)
    args = parser.parse_args(argv)
    try:
        req = PostgresRequest(args.application, args.account, args.region, args.vpc_id,
                              tuple(args.subnet_id), args.service_security_group)
        provisioner = AwsPostgresProvisioner(req, event=lambda stage, message:
                                            print(message, flush=True) if stage == 'cost' else None)
        if args.inspect:
            result = provisioner.inspect_current()
        elif args.apply:
            result = provisioner.create()
        else:
            result = provisioner.preflight()
            print('읽기 전용 확인 완료. --apply 없이는 RDS 리소스를 생성하지 않습니다.', flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, AwsConfigurationError) as exc:
        parser.exit(2, f'PostgreSQL 사전 점검 실패: {exc}\n')


if __name__ == '__main__':
    main()
