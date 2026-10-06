import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.aws import AwsConfigurationError
from sky_platform.runtime.postgres import PostgresRequest
from sky_platform.runtime.postgres_retirement import apply, plan


class FakeAdapter:
    def __init__(self, owner):
        self.owner = owner
        self.settings = object()

    def aws(self, args, **_kwargs):
        self.owner.calls.append(args)
        if args[:2] == ['rds', 'list-tags-for-resource']:
            return json.dumps({'TagList': [
                {'Key': 'sky-managed', 'Value': 'true'},
                {'Key': 'sky-app', 'Value': 'demo-app'}]})
        if args[:2] == ['cloudformation', 'describe-stacks']:
            return json.dumps({'Stacks': [{'StackId': self.owner.database['stack_id'],
                'StackStatus': 'CREATE_COMPLETE',
                'EnableTerminationProtection': not self.owner.stack_unprotected}]})
        if args[:2] == ['rds', 'modify-db-instance']:
            if '--deletion-protection' in args:
                self.owner.protection_removed = False
                return json.dumps({'DBInstance': {'DBInstanceIdentifier': 'sky-demo-app',
                    'DBInstanceArn': self.owner.database['database_arn'], 'DeletionProtection': True}})
            self.owner.protection_removed = True
            return json.dumps({'DBInstance': {'DBInstanceIdentifier': 'sky-demo-app',
                'DBInstanceArn': self.owner.database['database_arn'], 'DeletionProtection': False}})
        if args[:2] == ['rds', 'describe-db-instances']:
            return json.dumps({'DBInstances': [{'DBInstanceArn': self.owner.database['database_arn'],
                'DBInstanceStatus': 'available', 'DeletionProtection': False}]})
        if args[:2] == ['rds', 'delete-db-instance']:
            self.owner.deleted = True
            return json.dumps({'DBInstance': {'DBInstanceArn': self.owner.database['database_arn'],
                                             'DBInstanceStatus': 'deleting'}})
        if args[:2] == ['cloudformation', 'update-termination-protection']:
            self.owner.stack_unprotected = True
        return '{}'


class FakeProvisioner:
    instances = []

    def __init__(self, request):
        self.request = request
        self.calls = []
        self.database = {'database_id': request.database_id,
                         'database_arn': f'arn:aws:rds:{request.region}:{request.account}:db:{request.database_id}',
                         'stack_id': f'arn:aws:cloudformation:{request.region}:{request.account}:stack/{request.stack_name}/id',
                         'secret_arn': 'arn:aws:secretsmanager:region:account:secret:one',
                         'deletion_protection': True, 'retained_on_stack_delete': True}
        self.protection_removed = False
        self.stack_unprotected = False
        self.deleted = False
        self.adapter = FakeAdapter(self)
        self.instances.append(self)

    def inspect_current(self):
        return self.database


class RetirementTests(unittest.TestCase):
    def setUp(self):
        FakeProvisioner.instances.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'retirement.json'
        self.request = PostgresRequest('demo-app', '123456789012', 'ap-northeast-2',
            'vpc-12345678', ('subnet-11111111', 'subnet-22222222'), 'sg-33333333')
        guard = patch('sky_platform.runtime.postgres_retirement.AwsPostgresProvisioner', FakeProvisioner)
        guard.start()
        self.addCleanup(guard.stop)

    def test_plan_rejects_active_workload_without_mutation(self):
        with patch('sky_platform.runtime.postgres_retirement.active_database_users', return_value=['service']):
            with self.assertRaisesRegex(AwsConfigurationError, '남아'):
                plan(self.request)
        self.assertFalse(self.path.exists())
        self.assertFalse(any('delete' in ' '.join(call) for call in FakeProvisioner.instances[0].calls))

    def test_apply_requires_exact_database_confirmation(self):
        with self.assertRaisesRegex(ValueError, '정확히'):
            apply(self.request, self.path, confirm_database_id='wrong')
        self.assertFalse(self.path.exists())
        self.assertFalse(FakeProvisioner.instances)

    def test_snapshot_failure_leaves_protection_and_database_intact(self):
        with patch('sky_platform.runtime.postgres_retirement.active_database_users', return_value=[]), \
             patch('sky_platform.runtime.postgres_retirement.create_snapshot',
                   side_effect=AwsConfigurationError('snapshot failed')):
            with self.assertRaisesRegex(AwsConfigurationError, 'snapshot failed'):
                apply(self.request, self.path, confirm_database_id='sky-demo-app')
        state = json.loads(self.path.read_text())
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['stage'], 'creating_final_snapshot')
        self.assertFalse(any(item.protection_removed or item.deleted
                             for item in FakeProvisioner.instances))

    def test_apply_verifies_snapshot_before_deleting_and_refuses_replay(self):
        events = []
        def snapshot(_app, snapshot_id, _settings):
            events.append('create')
            return {'snapshot_id': snapshot_id}
        def inspect(_app, snapshot_id, _settings):
            events.append('inspect')
            return {'snapshot_id': snapshot_id, 'status': 'available', 'encrypted': True}
        with patch('sky_platform.runtime.postgres_retirement.active_database_users', return_value=[]), \
             patch('sky_platform.runtime.postgres_retirement.create_snapshot', side_effect=snapshot), \
             patch('sky_platform.runtime.postgres_retirement.inspect_snapshot', side_effect=inspect):
            result = apply(self.request, self.path, confirm_database_id='sky-demo-app')
            with self.assertRaises(FileExistsError):
                apply(self.request, self.path, confirm_database_id='sky-demo-app')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['stage'], 'stack_deleted')
        self.assertEqual(events[:2], ['create', 'inspect'])
        self.assertTrue(any(item.deleted for item in FakeProvisioner.instances))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_unavailable_final_snapshot_never_disables_protection(self):
        with patch('sky_platform.runtime.postgres_retirement.active_database_users', return_value=[]), \
             patch('sky_platform.runtime.postgres_retirement.create_snapshot',
                   side_effect=lambda _app, snapshot_id, _settings: {'snapshot_id': snapshot_id}), \
             patch('sky_platform.runtime.postgres_retirement.inspect_snapshot',
                   return_value={'status': 'failed', 'encrypted': True}):
            with self.assertRaisesRegex(AwsConfigurationError, '사용 가능'):
                apply(self.request, self.path, confirm_database_id='sky-demo-app')
        self.assertFalse(any(item.protection_removed or item.deleted
                             for item in FakeProvisioner.instances))

    def test_post_protection_workload_check_stops_before_delete(self):
        users = [[], [], ['new-service']]
        with patch('sky_platform.runtime.postgres_retirement.active_database_users',
                   side_effect=users), \
             patch('sky_platform.runtime.postgres_retirement.create_snapshot',
                   side_effect=lambda _app, snapshot_id, _settings: {'snapshot_id': snapshot_id}), \
             patch('sky_platform.runtime.postgres_retirement.inspect_snapshot',
                   return_value={'status': 'available', 'encrypted': True}):
            with self.assertRaisesRegex(AwsConfigurationError, '다시'):
                apply(self.request, self.path, confirm_database_id='sky-demo-app')
        state = json.loads(self.path.read_text())
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['stage'], 'db_protection_restored')
        self.assertFalse(any(item.protection_removed for item in FakeProvisioner.instances))
        self.assertFalse(any(item.deleted for item in FakeProvisioner.instances))


if __name__ == '__main__':
    unittest.main()
