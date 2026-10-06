import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.aws import AwsConfigurationError, AwsSettings
from sky_platform.runtime.postgres import (AwsPostgresProvisioner, PostgresRequest, TEMPLATE,
                                discover_existing_postgres, inspect_postgres_backup_status, main)


ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
VPC = 'vpc-12345678'
SUBNETS = ('subnet-11111111', 'subnet-22222222')
SERVICE_GROUP = 'sg-33333333'
DB_GROUP = 'sg-44444444'
STACK = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/sky-db-demo-app/stack-id'
DB_ARN = f'arn:aws:rds:{REGION}:{ACCOUNT}:db:sky-demo-app'
SECRET = f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:rds-managed-secret'
ROLE = f'arn:aws:iam::{ACCOUNT}:role/sky-db-demo-app-execution'
HOST = f'sky-demo-app.abc.{REGION}.rds.amazonaws.com'


class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.request = PostgresRequest('demo-app', ACCOUNT, REGION, VPC, SUBNETS, SERVICE_GROUP)
        self.provisioner = AwsPostgresProvisioner(self.request)

    def test_template_retains_private_encrypted_database_and_managed_secret(self):
        template = json.loads(TEMPLATE.read_text())
        db = template['Resources']['Database']
        self.assertEqual(db['DeletionPolicy'], 'Retain')
        self.assertEqual(db['UpdateReplacePolicy'], 'Retain')
        properties = db['Properties']
        self.assertFalse(properties['PubliclyAccessible'])
        self.assertTrue(properties['DeletionProtection'])
        self.assertTrue(properties['StorageEncrypted'])
        self.assertTrue(properties['ManageMasterUserPassword'])
        self.assertEqual(properties['EngineVersion'], {'Ref': 'EngineVersion'})
        self.assertEqual(properties['AvailabilityZone'], {'Ref': 'AvailabilityZone'})
        self.assertEqual(properties['EngineLifecycleSupport'],
                         'open-source-rds-extended-support-disabled')
        self.assertNotIn('MasterUserPassword', properties)
        self.assertEqual(template['Resources']['DatabaseSecurityGroup']['Properties']
                         ['SecurityGroupIngress'][0]['SourceSecurityGroupId'],
                         {'Ref': 'ServiceSecurityGroupId'})
        role = template['Resources']['DatabaseExecutionRole']['Properties']
        self.assertEqual(role['AssumeRolePolicyDocument']['Statement'][0]['Principal'],
                         {'Service': 'ecs-tasks.amazonaws.com'})
        self.assertEqual(role['Policies'][0]['PolicyDocument']['Statement'], [{
            'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
            'Resource': {'Fn::GetAtt': ['Database', 'MasterUserSecret.SecretArn']}}])
        self.assertEqual(template['Outputs']['DatabaseExecutionRoleArn']['Value'],
                         {'Fn::GetAtt': ['DatabaseExecutionRole', 'Arn']})
        self.assertEqual(template['Resources']['MigrationLogGroup']['Properties']['RetentionInDays'], 14)

    def test_backup_status_audits_db_and_limits_snapshot_output(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT,
                               service_security_group=SERVICE_GROUP)
        database = {'database_id': 'sky-demo-app', 'status': 'available',
                    'deletion_protection': True,
                    'retained_on_stack_delete': True}
        snapshots = [{'DBSnapshotIdentifier': f'demo-snapshot-{n}',
                      'DBInstanceIdentifier': 'sky-demo-app',
                      'DBSnapshotArn': f'arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:demo-snapshot-{n}',
                      'SnapshotType': 'manual', 'Status': 'available',
                      'SnapshotCreateTime': f'2026-10-{n:02d}T00:00:00Z',
                      'Encrypted': True} for n in range(1, 13)]
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['rds', 'describe-db-instances']:
                return json.dumps({'DBInstances': [{'DBInstanceIdentifier': 'sky-demo-app',
                    'DBInstanceArn': DB_ARN, 'BackupRetentionPeriod': 7,
                    'LatestRestorableTime': '2026-10-12T00:00:00Z'}]})
            return json.dumps({'DBSnapshots': snapshots})
        with patch('sky_platform.runtime.postgres.discover_existing_postgres', return_value=database), \
                patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', autospec=True, side_effect=aws):
            result = inspect_postgres_backup_status('demo-app', settings)
        self.assertEqual(result['backup_retention_days'], 7)
        self.assertEqual(result['database_status'], 'available')
        self.assertEqual(result['manual_snapshot_count'], 12)
        self.assertEqual(len(result['manual_snapshots']), 10)
        self.assertEqual(result['manual_snapshots'][0]['snapshot_id'], 'demo-snapshot-12')
        self.assertEqual(calls, [['rds', 'describe-db-instances'], ['rds', 'describe-db-snapshots']])

    def test_backup_status_allows_snapshot_still_creating_without_timestamp(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT,
                               service_security_group=SERVICE_GROUP)
        database = {'database_id': 'sky-demo-app', 'status': 'available',
                    'deletion_protection': True, 'retained_on_stack_delete': True}
        snapshot = {'DBSnapshotIdentifier': 'sky-demo-app-before-migration',
                    'DBInstanceIdentifier': 'sky-demo-app',
                    'DBSnapshotArn': f'arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:sky-demo-app-before-migration',
                    'SnapshotType': 'manual', 'Status': 'creating', 'Encrypted': True}
        with patch('sky_platform.runtime.postgres.discover_existing_postgres', return_value=database), \
                patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', side_effect=[
                    json.dumps({'DBInstances': [{'DBInstanceIdentifier': 'sky-demo-app',
                        'DBInstanceArn': DB_ARN, 'BackupRetentionPeriod': 7}]}),
                    json.dumps({'DBSnapshots': [snapshot]})]):
            result = inspect_postgres_backup_status('demo-app', settings)
        self.assertEqual(result['manual_snapshots'][0]['status'], 'creating')
        self.assertIsNone(result['manual_snapshots'][0]['created_at'])

    def test_backup_status_rejects_foreign_snapshot(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT,
                               service_security_group=SERVICE_GROUP)
        database = {'database_id': 'sky-demo-app', 'deletion_protection': True,
                    'retained_on_stack_delete': True}
        with patch('sky_platform.runtime.postgres.discover_existing_postgres', return_value=database), \
                patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', side_effect=[
                    json.dumps({'DBInstances': [{'DBInstanceIdentifier': 'sky-demo-app',
                        'DBInstanceArn': DB_ARN, 'BackupRetentionPeriod': 7}]}),
                    json.dumps({'DBSnapshots': [{'DBSnapshotIdentifier': 'foreign',
                        'DBInstanceIdentifier': 'sky-demo-app',
                        'DBSnapshotArn': f'arn:aws:rds:{REGION}:999999999999:snapshot:foreign',
                        'SnapshotType': 'manual', 'Status': 'available',
                        'SnapshotCreateTime': '2026-10-01T00:00:00Z',
                        'Encrypted': True}]})]):
            with self.assertRaisesRegex(AwsConfigurationError, '스냅샷'):
                inspect_postgres_backup_status('demo-app', settings)

    def test_preflight_checks_account_vpc_group_and_two_azs(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]})
            if args[:2] == ['ec2', 'describe-security-groups']:
                return json.dumps({'SecurityGroups': [{'GroupId': SERVICE_GROUP,
                    'VpcId': VPC, 'OwnerId': ACCOUNT, 'IpPermissions': [],
                    'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                             {'Key': 'sky-app', 'Value': 'demo-app'}]}]})
            if args[:2] == ['ec2', 'describe-subnets']:
                return json.dumps({'Subnets': [
                    {'SubnetId': SUBNETS[0], 'VpcId': VPC, 'State': 'available', 'AvailabilityZone': 'a'},
                    {'SubnetId': SUBNETS[1], 'VpcId': VPC, 'State': 'available', 'AvailabilityZone': 'b'}]})
            if args[:2] == ['rds', 'describe-orderable-db-instance-options']:
                return json.dumps({'OrderableDBInstanceOptions': [{
                    'Engine': 'postgres', 'EngineVersion': '17.5',
                    'DBInstanceClass': 'db.t4g.micro', 'StorageType': 'gp3', 'Vpc': True,
                    'SupportsStorageEncryption': True, 'MinStorageSize': 20,
                    'MaxStorageSize': 65536,
                    'AvailabilityZones': [{'Name': 'a'}]}]})
            if args[:2] == ['rds', 'describe-db-engine-versions']:
                return json.dumps({'DBEngineVersions': [{'Engine': 'postgres', 'EngineVersion': '17.5'}]})
            raise AssertionError(args)
        price = {'baseline_730h_usd': '20.87'}
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch('sky_platform.runtime.postgres.estimate_postgres_base_capacity', return_value=price):
            result = self.provisioner.preflight()
        self.assertEqual(result['availability_zones'], ['a', 'b'])
        self.assertEqual(result['database_availability_zone'], 'a')
        self.assertEqual(result['engine_version'], '17.5')
        self.assertEqual(result['storage_type'], 'gp3')
        self.assertEqual(result['pricing'], price)
        self.assertEqual(calls, [['sts', 'get-caller-identity'], ['ec2', 'describe-vpcs'],
                                 ['ec2', 'describe-security-groups'], ['ec2', 'describe-subnets'],
                                 ['rds', 'describe-db-engine-versions'],
                                 ['rds', 'describe-orderable-db-instance-options']])
        with patch.object(self.provisioner.adapter, 'aws',
                          side_effect=lambda args, **kwargs: json.dumps({'Account': '999999999999'})):
            with self.assertRaisesRegex(AwsConfigurationError, '현재 AWS 계정'):
                self.provisioner.preflight()
        def wrong_storage(args, **kwargs):
            result = json.loads(aws(args, **kwargs))
            if args[:2] == ['rds', 'describe-orderable-db-instance-options']:
                result['OrderableDBInstanceOptions'][0]['StorageType'] = 'gp2'
            return json.dumps(result)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=wrong_storage), \
                patch('sky_platform.runtime.postgres.estimate_postgres_base_capacity') as pricing:
            with self.assertRaisesRegex(AwsConfigurationError, 'gp3'):
                self.provisioner.preflight()
            pricing.assert_not_called()

    def test_preflight_accepts_existing_express_gateway_ingress(self):
        def aws(args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            if args[:2] == ['ec2', 'describe-vpcs']:
                return json.dumps({'Vpcs': [{'VpcId': VPC, 'IsDefault': True}]})
            if args[:2] == ['ec2', 'describe-security-groups']:
                return json.dumps({'SecurityGroups': [{'GroupId': SERVICE_GROUP,
                    'VpcId': VPC, 'OwnerId': ACCOUNT,
                    'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                             {'Key': 'sky-app', 'Value': 'demo-app'}],
                    'IpPermissions': [{
                        'IpProtocol': 'tcp', 'FromPort': 3000, 'ToPort': 3000,
                        'UserIdGroupPairs': [{'GroupId': 'sg-55555555',
                                              'UserId': ACCOUNT, 'VpcId': VPC}]}]}]})
            if args[:2] == ['ec2', 'describe-subnets']:
                return json.dumps({'Subnets': [
                    {'SubnetId': SUBNETS[0], 'VpcId': VPC, 'State': 'available', 'AvailabilityZone': 'a'},
                    {'SubnetId': SUBNETS[1], 'VpcId': VPC, 'State': 'available', 'AvailabilityZone': 'b'}]})
            if args[:2] == ['rds', 'describe-db-engine-versions']:
                return json.dumps({'DBEngineVersions': [{'Engine': 'postgres', 'EngineVersion': '17.5'}]})
            if args[:2] == ['rds', 'describe-orderable-db-instance-options']:
                return json.dumps({'OrderableDBInstanceOptions': [{
                    'Engine': 'postgres', 'EngineVersion': '17.5',
                    'DBInstanceClass': 'db.t4g.micro', 'StorageType': 'gp3', 'Vpc': True,
                    'SupportsStorageEncryption': True, 'MinStorageSize': 20,
                    'MaxStorageSize': 65536,
                    'AvailabilityZones': [{'Name': 'a'}]}]})
            raise AssertionError(args)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch('sky_platform.runtime.postgres.estimate_postgres_base_capacity',
                      return_value={'baseline_730h_usd': '20.87'}):
            result = self.provisioner.preflight()
            self.assertEqual(result['engine_version'], '17.5')
            self.assertEqual(result['database_availability_zone'], 'a')

    def test_service_group_rejects_another_apps_group(self):
        group = {'GroupId': SERVICE_GROUP, 'VpcId': VPC, 'OwnerId': ACCOUNT,
                 'IpPermissions': [], 'Tags': [
                     {'Key': 'sky-managed', 'Value': 'true'},
                     {'Key': 'sky-app', 'Value': 'other-app'}]}
        with patch.object(self.provisioner.adapter, 'aws',
                          return_value=json.dumps({'SecurityGroups': [group]})):
            with self.assertRaisesRegex(AwsConfigurationError, '앱 전용 소유 태그'):
                self.provisioner.verify_service_group()

    def test_create_uses_create_only_and_checks_output_after_wait(self):
        calls = []
        events = []
        def aws(args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ['cloudformation', 'create-stack']:
                spec = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
                self.assertEqual(spec.stat().st_mode & 0o777, 0o600)
                payload = json.loads(spec.read_text())
                self.assertEqual(payload['StackName'], self.request.stack_name)
                self.assertTrue(payload['EnableTerminationProtection'])
                self.assertEqual(payload['Capabilities'], ['CAPABILITY_IAM'])
                self.assertIn({'ParameterKey': 'EngineVersion', 'ParameterValue': '17.5'},
                              payload['Parameters'])
                self.assertIn({'ParameterKey': 'AvailabilityZone', 'ParameterValue': 'a'},
                              payload['Parameters'])
                self.assertEqual(json.loads(payload['TemplateBody'])['Resources']['Database']
                                 ['Properties']['PubliclyAccessible'], False)
                return json.dumps({'StackId': STACK})
            if args[:2] == ['cloudformation', 'wait']:
                return ''
            raise AssertionError(args)
        with patch.object(self.provisioner, 'preflight', return_value={
                'engine_version': '17.5', 'database_availability_zone': 'a',
                'pricing': {'baseline_730h_usd': '20.87'}}), \
                patch.object(self.provisioner.adapter, 'event',
                             side_effect=lambda stage, message: events.append((stage, message))), \
                patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'inspect', return_value={'status': 'available'}) as inspect:
            self.assertEqual(self.provisioner.create(), {'status': 'available'})
        self.assertEqual(calls, [['cloudformation', 'create-stack'], ['cloudformation', 'wait']])
        self.assertEqual(events[0][0], 'cost')
        self.assertIn('20.87 USD', events[0][1])
        inspect.assert_called_once_with(STACK)

    def test_create_rejects_changed_approved_plan_before_aws_mutation(self):
        with patch.object(self.provisioner, 'preflight', return_value={
                'engine_version': '18.3', 'database_availability_zone': 'a',
                'pricing': {'baseline_730h_usd': '22.00'}}), \
                patch.object(self.provisioner.adapter, 'aws') as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '생성 계획이 변경'):
                self.provisioner.create(expected_plan={
                    'engine_version': '18.3', 'database_availability_zone': 'b',
                    'pricing': {'baseline_730h_usd': '20.87'}})
        aws.assert_not_called()

    def test_existing_stack_name_blocks_new_database_plan(self):
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({
                'StackSummaries': [{'StackName': self.request.stack_name,
                                    'StackStatus': 'CREATE_COMPLETE'}]})):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.provisioner.assert_stack_available()
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({
                'StackSummaries': [{'StackName': 'unrelated-stack'}]})):
            self.provisioner.assert_stack_available()
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({
                'StackSummaries': [], 'NextToken': 'truncated'})):
            with self.assertRaisesRegex(AwsConfigurationError, '완전히'):
                self.provisioner.assert_stack_available()
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({
                'StackSummaries': [{'StackName': self.request.stack_name,
                                    'StackStatus': 'DELETE_COMPLETE'}]})):
            self.provisioner.assert_stack_available(allow_deleted=True)
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.provisioner.assert_stack_available()
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({
                'StackSummaries': [{'StackName': self.request.stack_name,
                                    'StackStatus': 'DELETE_COMPLETE'},
                                   {'StackName': self.request.stack_name,
                                    'StackStatus': 'CREATE_COMPLETE'}]})):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.provisioner.assert_stack_available(allow_deleted=True)

    def test_inspect_rejects_public_or_misowned_database(self):
        stack = {'StackId': STACK, 'StackStatus': 'CREATE_COMPLETE',
                 'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                          {'Key': 'sky-app', 'Value': 'demo-app'}],
                 'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in {
                     'DatabaseIdentifier': 'sky-demo-app', 'DatabaseArn': DB_ARN,
                     'RequestedEngineVersion': '17.5',
                     'EndpointAddress': HOST, 'EndpointPort': '5432',
                     'SecretArn': SECRET, 'DatabaseExecutionRoleArn': ROLE,
                     'MigrationLogGroupName': '/sky/migrations/demo-app',
                     'DatabaseSecurityGroupId': DB_GROUP}.items()]}
        db = {'DBInstanceIdentifier': 'sky-demo-app', 'DBInstanceArn': DB_ARN,
              'DBInstanceStatus': 'available', 'Engine': 'postgres', 'EngineVersion': '17.5',
              'DBInstanceClass': 'db.t4g.micro', 'StorageType': 'gp3', 'AllocatedStorage': 20,
              'EngineLifecycleSupport': 'open-source-rds-extended-support-disabled',
              'DBName': 'appdb',
              'PubliclyAccessible': False, 'DeletionProtection': True, 'StorageEncrypted': True,
              'DBSubnetGroup': {'VpcId': VPC, 'Subnets': [
                  {'SubnetIdentifier': subnet} for subnet in SUBNETS]},
              'VpcSecurityGroups': [{'VpcSecurityGroupId': DB_GROUP}],
              'MasterUserSecret': {'SecretArn': SECRET},
              'Endpoint': {'Address': HOST, 'Port': 5432}}
        group = {'GroupId': DB_GROUP, 'VpcId': VPC, 'IpPermissions': [{
            'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432,
            'UserIdGroupPairs': [{'GroupId': SERVICE_GROUP, 'UserId': ACCOUNT, 'VpcId': VPC}]}]}
        service_group = {'GroupId': SERVICE_GROUP, 'VpcId': VPC, 'OwnerId': ACCOUNT,
                         'IpPermissions': [], 'Tags': [
                             {'Key': 'sky-managed', 'Value': 'true'},
                             {'Key': 'sky-app', 'Value': 'demo-app'}]}
        def aws(args, **_kwargs):
            if args[:2] == ['cloudformation', 'describe-stacks']:
                return json.dumps({'Stacks': [stack]})
            if args[:2] == ['ec2', 'describe-security-groups']:
                selected = service_group if SERVICE_GROUP in args else group
                return json.dumps({'SecurityGroups': [selected]})
            return json.dumps({'DBInstances': [db]})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'verify_execution_role'):
            result = self.provisioner.inspect(STACK)
            self.assertEqual(result['secret_arn'], SECRET)
            self.assertEqual(result['execution_role_arn'], ROLE)
            self.assertEqual(result['engine_version'], '17.5')
            db['EngineLifecycleSupport'] = 'open-source-rds-extended-support'
            with self.assertRaisesRegex(AwsConfigurationError, '실제 인스턴스'):
                self.provisioner.inspect(STACK)
            db['EngineLifecycleSupport'] = 'open-source-rds-extended-support-disabled'
            db['PubliclyAccessible'] = True
            with self.assertRaisesRegex(AwsConfigurationError, '비공개'):
                self.provisioner.inspect(STACK)
            db['PubliclyAccessible'] = False
            stack['Tags'][0]['Value'] = 'false'
            with self.assertRaisesRegex(AwsConfigurationError, '소유 태그'):
                self.provisioner.inspect(STACK)
            stack['Tags'][0]['Value'] = 'true'
            group['IpPermissions'][0]['IpRanges'] = [{'CidrIp': '0.0.0.0/0'}]
            with self.assertRaisesRegex(AwsConfigurationError, '인바운드'):
                self.provisioner.inspect(STACK)
            group['IpPermissions'][0].pop('IpRanges')
            db['StorageEncrypted'] = False
            with self.assertRaisesRegex(AwsConfigurationError, '비공개'):
                self.provisioner.inspect(STACK)
            db['StorageEncrypted'] = True
            service_group['Tags'][1]['Value'] = 'other-app'
            with self.assertRaisesRegex(AwsConfigurationError, '앱 전용 소유 태그'):
                self.provisioner.inspect(STACK)

    def test_execution_role_must_only_read_owned_secret(self):
        role_name = ROLE.rsplit('/', 1)[-1]
        role = {'Arn': ROLE, 'RoleName': role_name,
                'Tags': [{'Key': 'sky-managed', 'Value': 'true'},
                         {'Key': 'sky-app', 'Value': 'demo-app'}],
                'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                    'Effect': 'Allow', 'Principal': {'Service': 'ecs-tasks.amazonaws.com'},
                    'Action': 'sts:AssumeRole'}]}}
        managed = {'AttachedPolicies': [{'PolicyArn':
            'arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'}],
            'IsTruncated': False}
        inline = {'PolicyNames': ['ReadManagedDatabaseSecret'], 'IsTruncated': False}
        policy = {'RoleName': role_name, 'PolicyName': 'ReadManagedDatabaseSecret',
                  'PolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                      'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
                      'Resource': SECRET}]}}
        def aws(args, **_kwargs):
            if args[:2] == ['iam', 'get-role']:
                return json.dumps({'Role': role})
            if args[:2] == ['iam', 'list-attached-role-policies']:
                return json.dumps(managed)
            if args[:2] == ['iam', 'list-role-policies']:
                return json.dumps(inline)
            if args[:2] == ['iam', 'get-role-policy']:
                return json.dumps(policy)
            raise AssertionError(args)
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws):
            self.provisioner.verify_execution_role(ROLE, SECRET)
            role['AssumeRolePolicyDocument']['Statement'][0]['Principal'] = {'AWS': '*'}
            with self.assertRaisesRegex(AwsConfigurationError, '신뢰 정책'):
                self.provisioner.verify_execution_role(ROLE, SECRET)
            role['AssumeRolePolicyDocument']['Statement'][0]['Principal'] = {
                'Service': 'ecs-tasks.amazonaws.com'}
            policy['PolicyDocument']['Statement'][0]['Resource'] = '*'
            with self.assertRaisesRegex(AwsConfigurationError, '비밀 조회 권한'):
                self.provisioner.verify_execution_role(ROLE, SECRET)
            policy['PolicyDocument']['Statement'][0]['Resource'] = SECRET
            managed['AttachedPolicies'].append({'PolicyArn': 'arn:aws:iam::aws:policy/AdministratorAccess'})
            with self.assertRaisesRegex(AwsConfigurationError, '예상 밖의 정책'):
                self.provisioner.verify_execution_role(ROLE, SECRET)

    def test_inspect_current_checks_account_and_stack_name(self):
        calls = []
        def aws(args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'Stacks': [{'StackId': STACK}]})
        with patch.object(self.provisioner.adapter, 'aws', side_effect=aws), \
                patch.object(self.provisioner, 'inspect', return_value={'status': 'available'}) as inspect:
            self.assertEqual(self.provisioner.inspect_current(), {'status': 'available'})
        self.assertIn(self.request.stack_name, calls[1])
        inspect.assert_called_once_with(STACK)
        with patch.object(self.provisioner.adapter, 'aws', return_value=json.dumps({'Account': '999999999999'})), \
                patch.object(self.provisioner, 'inspect') as inspect:
            with self.assertRaisesRegex(AwsConfigurationError, '계정'):
                self.provisioner.inspect_current()
            inspect.assert_not_called()

    def test_discovery_uses_app_instance_network_then_checks_full_ownership(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT,
                               service_security_group=SERVICE_GROUP)
        calls = []
        def aws(_adapter, args, **_kwargs):
            calls.append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'DBInstances': [{'DBInstanceIdentifier': 'sky-demo-app',
                'DBSubnetGroup': {'VpcId': VPC, 'Subnets': [
                    {'SubnetIdentifier': subnet} for subnet in SUBNETS]}}]})
        verified = {'database_id': 'sky-demo-app', 'engine_version': '18.3',
                    'status': 'available', 'deletion_protection': True,
                    'retained_on_stack_delete': True, 'secret_arn': SECRET}
        with patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', autospec=True, side_effect=aws), \
                patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.inspect_current',
                      return_value=verified) as inspect:
            result = discover_existing_postgres('demo-app', settings)
        self.assertEqual(calls[1][:4], ['rds', 'describe-db-instances',
                                        '--db-instance-identifier', 'sky-demo-app'])
        self.assertEqual(inspect.call_args.args, ())
        self.assertEqual(result['subnet_ids'], list(SUBNETS))
        self.assertNotIn('secret_arn', result)

    def test_discovery_rejects_wrong_account_before_database_read(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT,
                               service_security_group=SERVICE_GROUP)
        with patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws',
                   return_value=json.dumps({'Account': '999999999999'})) as aws:
            with self.assertRaisesRegex(AwsConfigurationError, '계정'):
                discover_existing_postgres('demo-app', settings)
        self.assertEqual(aws.call_count, 1)

    def test_discovery_resolves_group_from_verified_application_network(self):
        settings = AwsSettings(REGION, expected_account=ACCOUNT)
        def aws(_adapter, args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': ACCOUNT})
            return json.dumps({'DBInstances': [{'DBInstanceIdentifier': 'sky-demo-app',
                'DBSubnetGroup': {'VpcId': VPC, 'Subnets': [
                    {'SubnetIdentifier': subnet} for subnet in SUBNETS]}}]})
        verified = {'database_id': 'sky-demo-app', 'engine_version': '18.3',
                    'status': 'available', 'deletion_protection': True,
                    'retained_on_stack_delete': True}
        with patch('sky_platform.runtime.postgres.AwsExpressAdapter.aws', autospec=True, side_effect=aws), \
                patch('sky_platform.runtime.postgres.AwsServiceNetworkProvisioner.inspect_current',
                      return_value={'service_security_group': SERVICE_GROUP}) as network, \
                patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.inspect_current',
                      return_value=verified) as inspect:
            result = discover_existing_postgres('demo-app', settings)
        self.assertEqual(network.call_args.args, ())
        self.assertEqual(inspect.call_args.args, ())
        self.assertEqual(result['vpc_id'], VPC)

    def test_dry_run_never_creates_stack(self):
        arguments = ['--application', 'demo-app', '--account', ACCOUNT, '--region', REGION,
                     '--vpc-id', VPC, '--subnet-id', SUBNETS[0], '--subnet-id', SUBNETS[1],
                     '--service-security-group', SERVICE_GROUP]
        with patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.preflight',
                   return_value={'account': ACCOUNT}), \
                patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.create') as create:
            main(arguments)
            create.assert_not_called()

    def test_inspect_cli_never_creates_stack(self):
        arguments = ['--application', 'demo-app', '--account', ACCOUNT, '--region', REGION,
                     '--vpc-id', VPC, '--subnet-id', SUBNETS[0], '--subnet-id', SUBNETS[1],
                     '--service-security-group', SERVICE_GROUP, '--inspect']
        with patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.inspect_current',
                   return_value={'status': 'available'}) as inspect, \
                patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.preflight') as preflight, \
                patch('sky_platform.runtime.postgres.AwsPostgresProvisioner.create') as create:
            main(arguments)
        inspect.assert_called_once_with()
        preflight.assert_not_called()
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
