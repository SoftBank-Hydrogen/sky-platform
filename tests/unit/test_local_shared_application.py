import json
from dataclasses import asdict, replace
from unittest.mock import patch

import pytest

from adapters.aws.ecs import (
    AwsConfigurationError,
    AwsExpressAdapter,
    AwsSettings,
    database_configuration_matches,
)
from adapters.aws.shared_application import SharedPostgresRequest, execution_template, secret_policy
from application.state_recovery import postgres_request_from_job
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool

ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'


def request():
    pool = SharedDatabasePool('dev', ACCOUNT, REGION, 'sky-workload-pool', 'sky_pool_dev', 30)
    allocation = PoolAllocationRequest(pool, 'org', 'demo-app')
    reg = {'version': 1, 'pool_configuration': {'version': 1, 'settings': {
        'pool': asdict(pool), 'resource_id': 'db-ABC', 'vpc_id': 'vpc-12345678',
        'database_security_group': 'sg-12345678', 'allowed_client_groups': ['sg-22222222'],
        'admin_secret_arn': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:pool-admin-ABC123',
        'app_secret_kms_arn': f'arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012',
        'sslrootcert': '/etc/ssl/certs/sky-rds-global-bundle.pem'}},
        'subnet_ids': ['subnet-11111111', 'subnet-22222222']}
    receipt = {'status': 'ready', 'binding': asdict(allocation.binding()), 'allocation_id': allocation.id,
        'login_role': allocation.login_role, 'connection_limit': 5,
        'verified_scope': 'postgresql_role_and_database_acl',
        'secret_ref': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:sky-pool/dev/{allocation.id}-ABC123'}
    return SharedPostgresRequest('demo-app', ACCOUNT, REGION, 'vpc-12345678',
        tuple(reg['subnet_ids']), 'sg-22222222', 'org', reg, receipt)


def test_shared_receipt_rejects_other_app_and_dedicated_secret():
    value = request()
    value.validate()
    with pytest.raises(ValueError):
        replace(value, organization_id='other').validate()
    with pytest.raises(ValueError):
        replace(value, allocation_receipt={**value.allocation_receipt,
            'secret_ref': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:other-ABC123'}).validate()


def test_execution_role_only_reads_its_app_secret_and_context_bound_kms(monkeypatch):
    monkeypatch.delenv('SKY_ENVIRONMENT', raising=False)
    monkeypatch.delenv('SKY_AWS_ROLE_BOUNDARY_ARN', raising=False)
    value = request()
    statements = secret_policy(value)['Statement']
    assert statements[0]['Resource'] == value.allocation_receipt['secret_ref']
    assert statements[1]['Condition']['StringEquals']['kms:EncryptionContext:SecretARN'] == statements[0]['Resource']
    assert 'rds' not in json.dumps(statements)
    template = execution_template(value)
    assert not any(x['Type'] == 'AWS::RDS::DBInstance' for x in template['Resources'].values())


def test_shared_state_restore_binds_owner_and_network():
    value = request()
    settings = AwsSettings(REGION, expected_account=ACCOUNT, service_security_group=value.service_security_group,
        vpc_id=value.vpc_id, subnet_ids=value.subnet_ids)
    job = json.loads(json.dumps({'shared_postgres': asdict(value), 'aws': asdict(settings),
        'target': 'aws-ecs-express', 'application_id': value.application_id, 'organization_id': 'org'}))
    assert postgres_request_from_job(job) == value
    job['organization_id'] = 'other'
    with pytest.raises(ValueError):
        postgres_request_from_job(job)


def test_custom_vpc_rejects_foreign_subnet_before_launch():
    settings = AwsSettings(REGION, expected_account=ACCOUNT, service_security_group='sg-22222222',
        vpc_id='vpc-12345678', subnet_ids=('subnet-11111111', 'subnet-22222222'))
    adapter = AwsExpressAdapter(lambda *_: None, settings)
    responses = [{'Vpcs': [{'VpcId': settings.vpc_id, 'OwnerId': ACCOUNT}]}, {'Subnets': [
        {'SubnetId': x, 'VpcId': settings.vpc_id, 'OwnerId': ACCOUNT if i == 0 else '999999999999',
         'State': 'available', 'AvailabilityZone': REGION + str(i)} for i, x in enumerate(settings.subnet_ids)]}]
    with patch.object(adapter, 'aws', side_effect=[json.dumps(x) for x in responses]), pytest.raises(AwsConfigurationError):
        adapter.validate_service_security_group()


def test_custom_vpc_incomplete_registration_is_blocked():
    with pytest.raises(AwsConfigurationError):
        AwsSettings(REGION, expected_account=ACCOUNT, vpc_id='vpc-12345678').validate()


def test_database_configuration_distinguishes_logical_tenants():
    value = request()
    db = {'endpoint': 'db.example', 'port': 5432, 'database_name': value.allocation().database_name,
          'execution_role_arn': 'role', 'secret_arn': value.allocation_receipt['secret_ref']}
    config = {'executionRoleArn': 'role', 'primaryContainer': {'environment': [
        {'name': 'PGHOST', 'value': 'db.example'}, {'name': 'PGPORT', 'value': '5432'},
        {'name': 'PGDATABASE', 'value': db['database_name']}, {'name': 'PGSSLMODE', 'value': 'require'}],
        'secrets': [{'name': 'PGUSER', 'valueFrom': db['secret_arn'] + ':username::'},
                    {'name': 'PGPASSWORD', 'valueFrom': db['secret_arn'] + ':password::'}]}}
    assert database_configuration_matches(config, db)
    assert not database_configuration_matches(config, {**db, 'database_name': 'appdb'})


def test_sqlite_zip_admission_automatically_prepares_conversion_without_db_headers(tmp_path):
    import sqlite3

    from application.analysis import AISettings
    from application.infrastructure import inspect_infrastructure
    from domain.access import ResourceOwner
    from interfaces.http.server import App

    value = request()
    settings = AwsSettings(REGION, expected_account=ACCOUNT, service_security_group=value.service_security_group,
        vpc_id=value.vpc_id, subnet_ids=value.subnet_ids)
    app = App(tmp_path / 'state', ai_settings=AISettings('test-key'), aws_settings=settings,
              monitor_interval=0, github_poll_interval=0)
    app.local_shared_registration = value.registration
    project = app.root / ('a' * 16) / 'source'
    project.mkdir(parents=True)
    (project / 'requirements.txt').write_text('Flask\ngunicorn\n')
    (project / 'app.py').write_text("import sqlite3\nfrom flask import Flask\napp=Flask(__name__)\n")
    with sqlite3.connect(project / 'notes.db') as db:
        db.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO notes VALUES(1,'preserved')")
    profile = inspect_infrastructure(project)
    with patch.object(settings.__class__, 'unavailable_reason', return_value=None):
        app.create_shared_job('a' * 16, project, 'demo-app', profile, ResourceOwner('org', 'user'), True)
    job = app.jobs['a' * 16]
    assert job['sqlite_conversion']['row_counts'] == {'notes': 1}
    assert job['deployment_policy']['allow_data_migration'] is True
    assert job['shared_preparation']['status'] == 'accepted'
    assert 'postgres' not in job and 'shared_postgres' not in job
    assert (project / 'notes.db').is_file()


def test_changed_source_blocks_allocation_before_external_work(tmp_path):
    from application.analysis import AISettings
    from domain.access import ResourceOwner
    from interfaces.http.server import App

    value = request()
    settings = AwsSettings(REGION, expected_account=ACCOUNT, service_security_group=value.service_security_group,
        vpc_id=value.vpc_id, subnet_ids=value.subnet_ids)
    app = App(tmp_path / 'state', ai_settings=AISettings('test-key'), aws_settings=settings,
              monitor_interval=0, github_poll_interval=0)
    app.local_shared_registration = value.registration
    project = app.root / ('a' * 16) / 'source'
    project.mkdir(parents=True)
    (project / 'requirements.txt').write_text('Flask\ngunicorn\npsycopg[binary]\n')
    (project / 'app.py').write_text('import psycopg\nfrom flask import Flask\napp=Flask(__name__)\n')
    (project / 'migrations').mkdir()
    (project / 'migrations/0001_init.sql').write_text('CREATE TABLE notes(id INTEGER);')
    from application.infrastructure import inspect_infrastructure

    with patch.object(settings.__class__, 'unavailable_reason', return_value=None):
        app.create_shared_job('a' * 16, project, 'demo-app', inspect_infrastructure(project),
                              ResourceOwner('org', 'user'), True)
    (project / 'app.py').write_text('changed source')
    with patch('application.local_shared_deployments.LocalSharedApplication') as bridge:
        app.run_shared_then_agent('a' * 16)
    bridge.assert_not_called()
    assert app.jobs['a' * 16]['status'] == 'failed'


def test_consistency_rejects_different_logical_db_in_same_rds():
    from application.consistency import check_database_consistency
    from engine.compatibility import (
        InfrastructureProfile,
        explicit_infrastructure_plan,
        infrastructure_compatibility,
    )

    value = request()
    profile = InfrastructureProfile('database', ('app.py',), 1, ('database',), ('postgresql',))
    plan = explicit_infrastructure_plan('aws-ecs-express', profile, existing_postgres_id=value.database_id)
    plan['compatibility'] = infrastructure_compatibility(profile, 'aws-ecs-express', postgres=True, public_access=True)
    plan['database']['workload_binding'] = asdict(value.allocation().binding())
    assert check_database_consistency(plan, profile, postgres_request=value)['status'] == 'pass'
    plan['database']['workload_binding']['database_name'] = 'peer_db'
    with pytest.raises(ValueError, match='Shared logical database'):
        check_database_consistency(plan, profile, postgres_request=value)


def test_database_probe_uses_app_role_and_digest_and_records_temporary_scope():
    from adapters.aws.shared_application import LocalSharedApplication, verify_application_database

    value = request()
    registration = {**value.registration,
        'image': f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-platform:trusted',
        'image_digest': 'sha256:' + 'a' * 64}
    value = replace(value, registration=registration)
    settings = AwsSettings(REGION, expected_account=ACCOUNT, service_security_group=value.service_security_group,
        vpc_id=value.vpc_id, subnet_ids=value.subnet_ids)
    updates = []
    adapter = AwsExpressAdapter(lambda *_: None, settings, checkpoint=lambda **x: updates.append(x))
    db = {'database_name': value.allocation().database_name, 'endpoint': 'db.example', 'port': 5432,
        'secret_arn': value.allocation_receipt['secret_ref'],
        'execution_role_arn': f'arn:aws:iam::{ACCOUNT}:role/app-only', 'migration_log_group': '/sky/migrations/demo-app'}
    definition = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/sky-db-probe-owned:1'
    task = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/default/' + 'b' * 32
    receipt = {'database_name': db['database_name'], 'login_role': value.allocation().login_role,
        'status': 'passed', 'scope': 'app_login_tls_temporary_table_read_write', 'persistent_data_modified': False}
    calls = []

    def invoke(command, payload):
        calls.append((command, payload))
        return ({'taskDefinition': {'taskDefinitionArn': definition}} if command[1] == 'register-task-definition'
                else {'tasks': [{'taskArn': task}]})

    def aws(command, **_):
        if command[1] == 'describe-tasks':
            return json.dumps({'tasks': [{'taskDefinitionArn': definition, 'lastStatus': 'STOPPED',
                'containers': [{'exitCode': 0}]}]})
        if command[1] == 'get-log-events':
            return json.dumps({'events': [{'message': 'SKY_DATABASE_VERIFIED=' + json.dumps(receipt)}]})
        return '{}'

    with (patch.object(SharedPostgresRequest, 'inspect', return_value=db),
          patch.object(LocalSharedApplication, '_json_call', side_effect=invoke),
          patch.object(adapter, 'aws', side_effect=aws)):
        verified = verify_application_database(value, adapter, 'a' * 16 + '-a1')
    payload = calls[0][1]
    assert payload['taskRoleArn'] == db['execution_role_arn']
    assert payload['executionRoleArn'] == db['execution_role_arn']
    assert payload['containerDefinitions'][0]['image'].endswith('@' + registration['image_digest'])
    assert 'pool-admin' not in json.dumps(payload)
    assert verified['scope'] == receipt['scope']
    assert verified['definition_deregistered']
    assert updates[-1]['database_verification']['task_arn'] == task
