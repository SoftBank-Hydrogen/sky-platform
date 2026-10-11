"""Local one-action bridge to the registered, private workload pool.

Only trusted Sky allocation code runs with the pool administrator role. Uploaded
code runs later with an execution role that can read exactly its own secret.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time

from assets import ASSET_ROOT
from adapters.aws.ecs import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from adapters.aws.postgres import PostgresRequest, policy_document
from adapters.aws.role_boundary import managed_role_boundary
from adapters.aws.shared_database import AwsSharedDatabaseAllocator, AwsSharedPoolSettings
from domain.shared_database import SharedDatabasePool, PoolAllocationRequest


def pool_settings(document):
    values = dict(document['settings'])
    if values.get('sslrootcert') == '/etc/ssl/certs/sky-rds-global-bundle.pem' and not Path(values['sslrootcert']).is_file():
        values['sslrootcert'] = str(ASSET_ROOT / 'infra' / 'rds-global-bundle.pem')
    values['pool'] = SharedDatabasePool(**values['pool'])
    values['allowed_client_groups'] = tuple(values['allowed_client_groups'])
    return AwsSharedPoolSettings(**values)


@dataclass(frozen=True)
class SharedPostgresRequest(PostgresRequest):
    organization_id: str
    registration: dict
    allocation_receipt: dict

    @property
    def stack_name(self):
        return 'sky-pool-app-' + self.application_id

    @property
    def database_id(self):
        return self.registration['pool_configuration']['settings']['pool']['instance_id']

    def allocation(self):
        return PoolAllocationRequest(pool_settings(self.registration['pool_configuration']).pool,
                                     self.organization_id, self.application_id)

    def validate(self):
        super().validate()
        settings = pool_settings(self.registration['pool_configuration'])
        request = self.allocation()
        receipt = self.allocation_receipt
        if (settings.pool.account_id != self.account or settings.pool.region != self.region
                or settings.vpc_id != self.vpc_id
                or self.service_security_group not in settings.allowed_client_groups
                or tuple(self.registration['subnet_ids']) != self.subnet_ids
                or receipt.get('status') != 'ready'
                or receipt.get('binding') != asdict(request.binding())
                or receipt.get('allocation_id') != request.id
                or receipt.get('login_role') != request.login_role
                or receipt.get('connection_limit') != request.connection_limit
                or receipt.get('verified_scope') != 'postgresql_role_and_database_acl'
                or not re.fullmatch(re.escape(
                    f'arn:aws:secretsmanager:{self.region}:{self.account}:secret:sky-pool/{settings.pool.id}/{request.id}-')
                    + r'[A-Za-z0-9]{6}', receipt.get('secret_ref', ''))):
            raise ValueError('Shared allocation receipt differs from the app, pool or network')

    def inspect(self, adapter):
        self.validate()
        settings = pool_settings(self.registration['pool_configuration'])
        allocator = AwsSharedDatabaseAllocator(settings)
        host, port, _ = allocator._audit()
        secret = allocator.clients['secretsmanager'].describe_secret(SecretId=self.allocation_receipt['secret_ref'])
        tags = {item['Key']: item['Value'] for item in secret.get('Tags', [])}
        expected_tags = {'sky-managed': 'true', 'sky-database-role': 'shared_workload',
            'sky-pool-id': settings.pool.id, 'sky-allocation-id': self.allocation().id,
            'sky-organization-id': self.organization_id, 'sky-application-id': self.application_id,
            'sky-rds-resource-id': settings.resource_id}
        if (secret.get('ARN') != self.allocation_receipt['secret_ref']
                or secret.get('KmsKeyId') != settings.app_secret_kms_arn
                or secret.get('DeletedDate') or secret.get('RotationEnabled')
                or secret.get('ReplicationStatus') or secret.get('OwningService')
                or any(tags.get(k) != v for k, v in expected_tags.items())
                or allocator.clients['secretsmanager'].get_resource_policy(SecretId=secret['ARN']).get('ResourcePolicy')):
            raise AwsConfigurationError('App secret ownership or encryption has changed')
        stacks = json.loads(adapter.aws(['cloudformation', 'describe-stacks', '--stack-name', self.stack_name],
                                        private=True, quiet=True)).get('Stacks', [])
        if len(stacks) != 1 or stacks[0]['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
            raise AwsConfigurationError('App execution stack is not ready')
        owned = {x['Key']: x['Value'] for x in stacks[0].get('Tags', [])}
        if any(owned.get(k) != v for k, v in {'sky-managed': 'true',
                'sky-app': self.application_id, 'sky-allocation': self.allocation().id}.items()):
            raise AwsConfigurationError('App execution stack ownership differs')
        outputs = {x['OutputKey']: x['OutputValue'] for x in stacks[0]['Outputs']}
        role = outputs['ExecutionRoleArn']
        if not re.fullmatch(f'arn:aws:iam::{self.account}:role/[A-Za-z0-9+=,.@_-]+', role):
            raise AwsConfigurationError('App execution role is outside the account')
        name = role.rsplit('/', 1)[1]
        actual = json.loads(adapter.aws(['iam', 'get-role-policy', '--role-name', name,
                       '--policy-name', 'AppSecret'], private=True, quiet=True))
        attached = json.loads(adapter.aws(['iam', 'list-attached-role-policies', '--role-name', name],
                                           private=True, quiet=True))
        inline = json.loads(adapter.aws(['iam', 'list-role-policies', '--role-name', name],
                                         private=True, quiet=True))
        role_data = json.loads(adapter.aws(['iam', 'get-role', '--role-name', name], private=True, quiet=True))['Role']
        expected_role = execution_template(self)['Resources']['ExecutionRole']['Properties']
        if (attached.get('IsTruncated') or inline.get('IsTruncated')
                or [x['PolicyArn'] for x in attached.get('AttachedPolicies', [])]
                   != expected_role['ManagedPolicyArns']
                or inline.get('PolicyNames') != ['AppSecret']
                or role_data.get('Arn') != role
                or role_data.get('PermissionsBoundary', {}).get('PermissionsBoundaryArn', '')
                   != expected_role.get('PermissionsBoundary', '')
                or policy_document(role_data.get('AssumeRolePolicyDocument'))
                   != expected_role['AssumeRolePolicyDocument']):
            raise AwsConfigurationError('App role trust, boundary or attached policies changed')
        if policy_document(actual.get('PolicyDocument')) != secret_policy(self):
            raise AwsConfigurationError('App execution permissions differ from the owned secret')
        return {'database_id': self.database_id, 'database_name': self.allocation().database_name,
            'endpoint': host, 'port': port, 'secret_arn': secret['ARN'], 'execution_role_arn': role,
            'service_security_group': self.service_security_group,
            'database_security_group': settings.database_security_group,
            'migration_log_group': '/sky/migrations/' + self.application_id,
            'binding': asdict(self.allocation().binding()), 'allocation_id': self.allocation().id,
            'retained_on_stack_delete': True}


def secret_policy(request):
    settings = pool_settings(request.registration['pool_configuration'])
    secret = request.allocation_receipt['secret_ref']
    return {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['secretsmanager:GetSecretValue'], 'Resource': secret},
        {'Effect': 'Allow', 'Action': ['kms:Decrypt'], 'Resource': settings.app_secret_kms_arn,
         'Condition': {'StringEquals': {'kms:ViaService': f'secretsmanager.{request.region}.amazonaws.com',
                                      'kms:EncryptionContext:SecretARN': secret}}}]}


def execution_template(request):
    request.validate()
    boundary = managed_role_boundary(request.account)
    role = {'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Principal': {'Service': 'ecs-tasks.amazonaws.com'},
         'Action': 'sts:AssumeRole', 'Condition': {
            'StringEquals': {'aws:SourceAccount': request.account},
            'ArnLike': {'aws:SourceArn': f'arn:aws:ecs:{request.region}:{request.account}:*'}}}]},
        'ManagedPolicyArns': ['arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'],
        'Policies': [{'PolicyName': 'AppSecret', 'PolicyDocument': secret_policy(request)}]}
    if boundary:
        role['PermissionsBoundary'] = boundary
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Resources': {
        'ExecutionRole': {'Type': 'AWS::IAM::Role', 'Properties': role},
        'MigrationLogs': {'Type': 'AWS::Logs::LogGroup', 'Properties': {
            'LogGroupName': '/sky/migrations/' + request.application_id, 'RetentionInDays': 14}}},
        'Outputs': {'ExecutionRoleArn': {'Value': {'Fn::GetAtt': ['ExecutionRole', 'Arn']}}}}


class LocalSharedApplication:
    def __init__(self, registration, settings, event, checkpoint):
        self.registration, self.settings = registration, settings
        self.adapter = AwsExpressAdapter(event, settings)
        self.checkpoint, self.event = checkpoint, event
        pool = pool_settings(registration['pool_configuration'])
        if (pool.pool.account_id != settings.expected_account or pool.pool.region != settings.region
                or settings.vpc_id != pool.vpc_id
                or settings.service_security_group not in pool.allowed_client_groups
                or tuple(registration['subnet_ids']) != tuple(settings.subnet_ids)):
            raise ValueError('Local deployment registration differs from the private pool')
        self.adapter.settings.validate()

    def prepare(self, application_id, organization_id, job_id):
        registration, adapter = self.registration, self.adapter
        request = PoolAllocationRequest(pool_settings(registration['pool_configuration']).pool,
                                        organization_id, application_id)
        account = json.loads(adapter.aws(['sts', 'get-caller-identity'], private=True, quiet=True))['Account']
        if account != self.settings.expected_account:
            raise AwsConfigurationError('Allocation account differs from the pinned account')
        adapter.validate_service_security_group()
        if not re.fullmatch(f'arn:aws:ecs:{self.settings.region}:{account}:cluster/sky-[A-Za-z0-9_-]+',
                            registration['cluster_arn']):
            raise ValueError('Allocation cluster differs from the registered account')
        arn = registration['task_definition_arn']
        if not re.fullmatch(f'arn:aws:ecs:{self.settings.region}:{account}:task-definition/sky-[A-Za-z0-9_-]+:[0-9]+', arn):
            raise ValueError('An explicit trusted allocation task revision is required')
        definition = json.loads(adapter.aws(['ecs', 'describe-task-definition', '--task-definition', arn],
                                            private=True, quiet=True))['taskDefinition']
        containers = definition['containerDefinitions']
        if len(containers) != 1 or containers[0]['name'] != registration['container_name']:
            raise ValueError('Allocation task container differs from registration')
        original = containers[0]
        env = {x['name']: x['value'] for x in original.get('environment', [])}
        if (json.loads(env.get('SKY_SHARED_DATABASE_POOL_JSON', '{}')) != registration['pool_configuration']
                or original['image'] != registration['image']):
            raise ValueError('Allocation task image or pool differs from registration')
        image = registration['image']
        repository, tag = image.rsplit(':', 1)
        if repository != f'{account}.dkr.ecr.{self.settings.region}.amazonaws.com/sky-platform':
            raise ValueError('Trusted allocator must use the registered Sky service image')
        images = json.loads(adapter.aws(['ecr', 'describe-images', '--repository-name', 'sky-platform',
            '--image-ids', f'imageTag={tag}'], private=True, quiet=True))['imageDetails']
        if len(images) != 1 or images[0]['imageDigest'] != registration['image_digest']:
            raise ValueError('Registered allocation image changed')
        # A new digest-pinned definition prevents a mutable image tag race.
        pinned = {k: definition[k] for k in ('taskRoleArn', 'executionRoleArn', 'networkMode',
            'requiresCompatibilities', 'cpu', 'memory', 'runtimePlatform') if k in definition}
        pinned['family'] = 'sky-pool-allocate-' + job_id
        pinned['containerDefinitions'] = [dict(original, image=repository + '@' + registration['image_digest'], entryPoint=['python'])]
        self.event('database_allocating', '공유 RDS에 앱 전용 DB·사용자를 할당합니다.')
        self.checkpoint(shared_preparation={'status': 'intent_recorded', 'allocation_id': request.id})
        definition_arn = self._json_call(['ecs', 'register-task-definition'], pinned)['taskDefinition']['taskDefinitionArn']
        self.checkpoint(shared_preparation={'status': 'task_registered', 'task_definition_arn': definition_arn,
                                            'allocation_id': request.id})
        code = ('import json; from interfaces.shared_database_worker import load_pool_configuration; '
                'from adapters.aws.shared_database import AwsSharedDatabaseAllocator; '
                'from domain.shared_database import PoolAllocationRequest; '
                's,_=load_pool_configuration(None); '
                f'r=PoolAllocationRequest(s.pool,{organization_id!r},{application_id!r}); '
                'print("SKY_ALLOCATION_RECEIPT="+json.dumps(AwsSharedDatabaseAllocator(s).allocate(r)),flush=True)')
        tags = [{'key': 'sky-managed', 'value': 'true'}, {'key': 'sky-app', 'value': application_id},
                {'key': 'sky-job', 'value': job_id}]
        # Do not retry RunTask after an uncertain response; the saved intent remains.
        self.checkpoint(shared_preparation={'status': 'starting', 'task_definition_arn': definition_arn,
                                            'allocation_id': request.id})
        response = self._json_call(['ecs', 'run-task'], {'cluster': registration['cluster_arn'],
            'taskDefinition': definition_arn, 'launchType': 'FARGATE', 'count': 1,
            'clientToken': 'sky-pool-' + job_id, 'tags': tags,
            'networkConfiguration': {'awsvpcConfiguration': {'subnets': list(self.settings.subnet_ids),
                'securityGroups': [self.settings.service_security_group], 'assignPublicIp': 'ENABLED'}},
            'overrides': {'containerOverrides': [{'name': original['name'], 'command': ['-c', code]}]}})
        if response.get('failures') or len(response.get('tasks', [])) != 1:
            raise AwsConfigurationError('Allocation task start requires review')
        task_arn = response['tasks'][0]['taskArn']
        self.checkpoint(shared_preparation={'status': 'allocating', 'task_definition_arn': definition_arn,
                                            'task_arn': task_arn, 'allocation_id': request.id})
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            tasks = json.loads(adapter.aws(['ecs', 'describe-tasks', '--cluster', registration['cluster_arn'],
                '--tasks', task_arn, '--include', 'TAGS'], private=True, quiet=True)).get('tasks', [])
            if len(tasks) != 1 or tasks[0]['taskDefinitionArn'] != definition_arn:
                raise AwsConfigurationError('Allocation task ownership requires review')
            task = tasks[0]
            if task['lastStatus'] == 'STOPPED':
                if (len(task.get('containers', [])) != 1 or task['containers'][0].get('exitCode') != 0
                        or task['containers'][0].get('imageDigest') != registration['image_digest']):
                    raise AwsConfigurationError('Private DB allocation failed; resources preserved for review')
                break
            time.sleep(3)
        else:
            raise AwsConfigurationError('Allocation timed out; do not repeat the uncertain allocation')
        options = original['logConfiguration']['options']
        stream = options['awslogs-stream-prefix'] + '/' + original['name'] + '/' + task_arn.rsplit('/', 1)[1]
        events = json.loads(adapter.aws(['logs', 'get-log-events', '--log-group-name', options['awslogs-group'],
            '--log-stream-name', stream, '--start-from-head'], private=True, quiet=True))['events']
        receipts = [json.loads(x['message'].split('=', 1)[1]) for x in events
                    if x['message'].startswith('SKY_ALLOCATION_RECEIPT=')]
        if len(receipts) != 1:
            raise AwsConfigurationError('Allocation receipt is missing or ambiguous')
        result = SharedPostgresRequest(application_id, account, self.settings.region, self.settings.vpc_id,
            tuple(self.settings.subnet_ids), self.settings.service_security_group,
            organization_id, registration, receipts[0])
        result.validate()
        adapter.aws(['ecs', 'deregister-task-definition', '--task-definition', definition_arn], private=True, quiet=True)
        self.checkpoint(shared_preparation={'status': 'allocated', 'allocation_id': request.id,
            'task_arn': task_arn, 'task_definition_arn': definition_arn, 'receipt': receipts[0]})
        import boto3
        cloudformation = boto3.Session(region_name=result.region).client('cloudformation')
        try:
            existing = cloudformation.describe_stacks(StackName=result.stack_name)['Stacks']
        except cloudformation.exceptions.ClientError as error:
            if (error.response['Error']['Code'] != 'ValidationError'
                    or 'does not exist' not in error.response['Error']['Message']):
                raise AwsConfigurationError('Unable to audit execution stack before deployment') from None
        else:
            expected = {'sky-managed': 'true', 'sky-app': application_id, 'sky-allocation': request.id}
            if len(existing) != 1 or any({x['Key']: x['Value'] for x in existing[0].get('Tags', [])}.get(k) != v
                                        for k, v in expected.items()):
                raise AwsConfigurationError('Existing execution stack is not owned by this app')
        template = execution_template(result)
        with tempfile.NamedTemporaryFile('w', suffix='.json') as file:
            json.dump(template, file); file.flush()
            adapter.aws(['cloudformation', 'deploy', '--template-file', file.name,
                '--stack-name', result.stack_name, '--capabilities', 'CAPABILITY_IAM',
                '--tags', 'sky-managed=true', 'sky-app=' + application_id, 'sky-allocation=' + request.id], timeout=600)
        result.inspect(adapter)
        return result

    def _json_call(self, command, payload):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as file:
            json.dump(payload, file); file.flush()
            return json.loads(self.adapter.aws([*command, '--cli-input-json', 'file://' + file.name],
                                               private=True, quiet=True))


def registration_digest(registration):
    return hashlib.sha256(json.dumps(registration, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_local_registration(path):
    with Path(path).open('rb') as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError('Local pool registration exceeds 64 KiB')
    registration = json.loads(data)
    if set(registration) != {'version', 'pool_configuration', 'subnet_ids', 'task_definition_arn',
            'cluster_arn', 'container_name', 'image', 'image_digest'} or (type(registration['version']) is not int or registration['version'] != 1):
        raise ValueError('Invalid local pool registration')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', registration['image_digest']):
        raise ValueError('Trusted allocation image digest is required')
    pool_settings(registration['pool_configuration'])
    return registration


def verify_application_database(request, adapter, attempt_id):
    """Read/write through the app login over TLS; roll back a temporary probe.

    This proves database connectivity and SQL privileges, not business routes.
    The trusted verifier has the app execution role, never the pool admin role.
    """
    request.validate()
    if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
        raise ValueError('Owned deployment attempt required for database verification')
    database = request.inspect(adapter)
    reg = request.registration
    repository = reg['image'].rsplit(':', 1)[0]
    code = ("import json,os,boto3,psycopg; "
        f"s=json.loads(boto3.client('secretsmanager',region_name={request.region!r}).get_secret_value(SecretId={database['secret_arn']!r})['SecretString']); "
        f"assert s['dbname']=={database['database_name']!r} and s['username']=={request.allocation().login_role!r}; "
        f"c=psycopg.connect(host={database['endpoint']!r},port={database['port']!r},dbname=s['dbname'],user=s['username'],password=s['password'],sslmode='verify-full',sslrootcert='/etc/ssl/certs/sky-rds-global-bundle.pem',connect_timeout=10); "
        "identity=c.execute('SELECT current_database(),current_user').fetchone(); "
        "c.execute('CREATE TEMP TABLE sky_runtime_probe(value text)'); "
        f"c.execute('INSERT INTO sky_runtime_probe VALUES (%s)',({attempt_id!r},)); "
        f"assert c.execute('SELECT value FROM sky_runtime_probe').fetchone()==({attempt_id!r},); "
        "c.rollback();c.close(); "
        "print('SKY_DATABASE_VERIFIED='+json.dumps({'database_name':identity[0],'login_role':identity[1],'status':'passed','scope':'app_login_tls_temporary_table_read_write','persistent_data_modified':False}),flush=True)")
    payload = {'family': 'sky-db-probe-' + attempt_id, 'networkMode': 'awsvpc',
        'requiresCompatibilities': ['FARGATE'], 'cpu': '256', 'memory': '512',
        'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
        'executionRoleArn': database['execution_role_arn'], 'taskRoleArn': database['execution_role_arn'],
        'containerDefinitions': [{'name': 'database-probe', 'essential': True,
            'image': repository + '@' + reg['image_digest'], 'entryPoint': ['python'], 'command': ['-c', code],
            'logConfiguration': {'logDriver': 'awslogs', 'options': {'awslogs-group': database['migration_log_group'],
                'awslogs-region': request.region, 'awslogs-stream-prefix': 'database-probe'}}}]}
    bridge = LocalSharedApplication(reg, adapter.settings, adapter.event, adapter.checkpoint or (lambda **_: None))
    definition = bridge._json_call(['ecs', 'register-task-definition'], payload)['taskDefinition']['taskDefinitionArn']
    if adapter.checkpoint:
        adapter.checkpoint(database_verification={'status': 'starting', 'task_definition_arn': definition})
    started = bridge._json_call(['ecs', 'run-task'], {'cluster': 'default', 'taskDefinition': definition,
        'launchType': 'FARGATE', 'count': 1, 'clientToken': 'sky-db-probe-' + hashlib.sha256(definition.encode()).hexdigest()[:32],
        'tags': [{'key': 'sky-managed', 'value': 'true'}, {'key': 'sky-app', 'value': request.application_id},
                 {'key': 'sky-attempt', 'value': attempt_id}],
        'networkConfiguration': {'awsvpcConfiguration': {'subnets': list(request.subnet_ids),
            'securityGroups': [request.service_security_group], 'assignPublicIp': 'ENABLED'}}})
    if started.get('failures') or len(started.get('tasks', [])) != 1:
        raise AwsConfigurationError('Database verification start requires reconciliation')
    task_arn = started['tasks'][0]['taskArn']
    if adapter.checkpoint:
        adapter.checkpoint(database_verification={'status': 'running', 'task_arn': task_arn,
                                                  'task_definition_arn': definition})
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        tasks = json.loads(adapter.aws(['ecs', 'describe-tasks', '--cluster', 'default', '--tasks', task_arn],
                                        private=True, quiet=True)).get('tasks', [])
        if len(tasks) != 1 or tasks[0]['taskDefinitionArn'] != definition:
            raise AwsConfigurationError('Database verification ownership changed')
        if tasks[0]['lastStatus'] == 'STOPPED':
            containers = tasks[0].get('containers', [])
            if len(containers) != 1 or containers[0].get('exitCode') != 0:
                raise AwsConfigurationError('App database read/write verification failed')
            break
        time.sleep(3)
    else:
        raise AwsConfigurationError('Database verification outcome is uncertain')
    stream = 'database-probe/database-probe/' + task_arn.rsplit('/', 1)[1]
    events = json.loads(adapter.aws(['logs', 'get-log-events', '--log-group-name', database['migration_log_group'],
        '--log-stream-name', stream, '--start-from-head'], private=True, quiet=True))['events']
    receipts = [json.loads(x['message'].split('=', 1)[1]) for x in events if x['message'].startswith('SKY_DATABASE_VERIFIED=')]
    if len(receipts) != 1 or receipts[0] != {'database_name': database['database_name'],
            'login_role': request.allocation().login_role, 'status': 'passed',
            'scope': 'app_login_tls_temporary_table_read_write', 'persistent_data_modified': False}:
        raise AwsConfigurationError('Database verification receipt differs from the app binding')
    adapter.aws(['ecs', 'deregister-task-definition', '--task-definition', definition], private=True, quiet=True)
    return {**receipts[0], 'task_arn': task_arn, 'task_definition_arn': definition,
            'definition_deregistered': True}
