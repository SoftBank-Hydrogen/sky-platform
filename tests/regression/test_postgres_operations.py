import json
import tempfile
import time
import unittest
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.postgres import PostgresRequest
from application.postgres_operations import PostgresOperations


class PostgresOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'database-operations'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                                    service_security_group='sg-33333333')
        self.request = PostgresRequest('demo-app', '123456789012', 'ap-northeast-2',
                                       'vpc-12345678', ('subnet-11111111', 'subnet-22222222'),
                                       'sg-33333333')
        self.manager = PostgresOperations(self.root, self.settings)
        self.quote = {'account': '123456789012', 'pricing': {'baseline_730h_usd': '20.87'}}
        guard = patch('application.postgres_operations.AwsPostgresProvisioner.assert_stack_available')
        guard.start()
        self.addCleanup(guard.stop)

    def test_accepted_create_is_journaled_before_worker_and_not_repeated_after_restart(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start') as start:
            plan = self.manager.plan(self.request)
            operation = self.manager.start('demo-app', plan['plan_id'])
        self.assertEqual(operation['status'], 'running')
        self.assertRegex(operation['creation_id'], r'^[a-f0-9]{16}$')
        self.assertEqual(start.call_count, 1)
        saved = json.loads((self.root / 'demo-app.json').read_text())
        self.assertEqual(saved['request']['subnet_ids'], list(self.request.subnet_ids))
        self.assertEqual(saved['creation_id'], operation['creation_id'])
        restored = PostgresOperations(self.root, self.settings)
        self.assertEqual(restored.get('demo-app')['status'], 'needs_attention')
        self.assertEqual(restored.get('demo-app')['creation_id'], operation['creation_id'])
        with self.assertRaisesRegex(ValueError, '이미'):
            restored.plan(self.request)

    def test_app_network_group_operation_restores_without_global_group(self):
        settings = AwsSettings('ap-northeast-2', expected_account='123456789012')
        manager = PostgresOperations(self.root, settings)
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = manager.plan(self.request)
            manager.start('demo-app', plan['plan_id'])
        restored = PostgresOperations(self.root, settings)
        self.assertFalse(restored.recovery_warnings)
        self.assertEqual(restored.get('demo-app')['status'], 'needs_attention')

    def test_changed_quote_rejects_before_creation_record(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   side_effect=[self.quote, {'account': '123456789012',
                       'pricing': {'baseline_730h_usd': '22.00'}}]), \
                patch('application.postgres_operations.threading.Thread.start') as start:
            plan = self.manager.plan(self.request)
            with self.assertRaisesRegex(ValueError, '변경'):
                self.manager.start('demo-app', plan['plan_id'])
        self.assertFalse((self.root / 'demo-app.json').exists())
        start.assert_not_called()

    def test_rds_baseline_cap_rejects_expensive_plan_before_creation(self):
        manager = PostgresOperations(self.root, self.settings, max_baseline_730h_usd='20.00')
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.AwsPostgresProvisioner.create') as create:
            with self.assertRaisesRegex(ValueError, '서버 상한'):
                manager.plan(self.request)
        self.assertFalse(manager.plans)
        self.assertFalse((self.root / 'demo-app.json').exists())
        create.assert_not_called()

    def test_rds_cap_is_rechecked_before_journal_and_worker(self):
        manager = PostgresOperations(self.root, self.settings, max_baseline_730h_usd='20.87')
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start') as worker:
            plan = manager.plan(self.request)
            manager.max_baseline_730h_usd = Decimal('20.00')
            with self.assertRaisesRegex(ValueError, '서버 상한'):
                manager.start('demo-app', plan['plan_id'])
        self.assertFalse((self.root / 'demo-app.json').exists())
        worker.assert_not_called()

    def test_rds_cap_requires_positive_decimal_usd(self):
        for value in ('', '0', '-1', '20.001', 'NaN', 'Infinity', '1e6', ' 20.87', 20):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'SKY_MAX_RDS_730H_USD'):
                PostgresOperations(self.root, self.settings, max_baseline_730h_usd=value)

    def test_rds_cap_fails_closed_when_quote_is_missing(self):
        manager = PostgresOperations(self.root, self.settings, max_baseline_730h_usd='20.00')
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value={'account': '123456789012'}):
            with self.assertRaisesRegex(ValueError, '견적을 확인하지 못해'):
                manager.plan(self.request)
        self.assertFalse(manager.plans)

    def test_only_current_reviewed_creation_plan_can_join_an_upload(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote):
            plan = self.manager.plan(self.request)
        self.assertEqual(self.manager.reviewed_request('demo-app', plan['plan_id']), self.request)
        with self.assertRaisesRegex(ValueError, '계획'):
            self.manager.reviewed_request('demo-app', 'wrong-token')
        self.manager.plans['demo-app']['expires'] = time.monotonic() - 1
        with self.assertRaisesRegex(ValueError, '만료'):
            self.manager.reviewed_request('demo-app', plan['plan_id'])

    def test_corrupt_active_journal_blocks_same_app_before_aws(self):
        (self.root / 'demo-app.json').write_text('{not-json')
        restored = PostgresOperations(self.root, self.settings)
        self.assertTrue(restored.recovery_warnings)
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight') as preflight:
            with self.assertRaisesRegex(AwsConfigurationError, '로컬 PostgreSQL'):
                restored.plan(self.request)
        preflight.assert_not_called()

    def test_corrupt_cleaned_archive_blocks_same_app_before_aws(self):
        archive = self.root / 'cleaned-attempts'
        (archive / 'demo-app-0123456789abcdef.json').write_text('{not-json')
        restored = PostgresOperations(self.root, self.settings)
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight') as preflight:
            with self.assertRaisesRegex(AwsConfigurationError, '로컬 PostgreSQL'):
                restored.plan(self.request)
        preflight.assert_not_called()

    def test_unidentified_journal_blocks_all_new_rds_creation(self):
        (self.root / '.postgres-unknown.json').write_text('{not-json')
        restored = PostgresOperations(self.root, self.settings)
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight') as preflight:
            with self.assertRaisesRegex(AwsConfigurationError, '로컬 PostgreSQL'):
                restored.plan(self.request)
        preflight.assert_not_called()

    def test_invalid_cleaned_record_is_not_archived_or_trusted(self):
        operation = {'application_id': 'demo-app', 'request': asdict(self.request),
                     'expected_plan': self.quote, 'status': 'failed_cleaned',
                     'created_at': '2026-10-03T00:00:00+00:00',
                     'cleaned_at': '2026-10-03T00:01:00+00:00',
                     'message': '정리 완료', 'attempt_id': '../wrong',
                     'recovery_stack_id': 'untrusted'}
        (self.root / 'demo-app.json').write_text(json.dumps(operation))
        restored = PostgresOperations(self.root, self.settings)
        self.assertTrue((self.root / 'demo-app.json').exists())
        self.assertFalse(list((self.root / 'cleaned-attempts').glob('*.json')))
        with self.assertRaisesRegex(AwsConfigurationError, '로컬 PostgreSQL'):
            restored.plan(self.request)

    def test_existing_stack_blocks_plan_before_any_creation_record(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.AwsPostgresProvisioner.assert_stack_available',
                      side_effect=AwsConfigurationError('스택 기록이 이미 있습니다.')):
            with self.assertRaisesRegex(AwsConfigurationError, '이미'):
                self.manager.plan(self.request)
        self.assertFalse((self.root / 'demo-app.json').exists())

    def test_successful_worker_records_database_without_secret(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        with patch('application.postgres_operations.AwsPostgresProvisioner.create',
                   return_value={'database_id': 'sky-demo-app',
                                 'secret_arn': 'secret'}) as create:
            self.manager._run('demo-app')
        create.assert_called_once_with(expected_plan=self.quote)
        operation = self.manager.get('demo-app')
        self.assertEqual(operation['status'], 'succeeded')
        self.assertNotIn('secret_arn', json.dumps(operation))
        self.assertEqual(operation['database_id'], 'sky-demo-app')

    def test_create_is_not_visible_as_succeeded_until_journal_is_saved(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        with patch('application.postgres_operations.AwsPostgresProvisioner.create',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch.object(self.manager, '_save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.manager._run('demo-app')
        self.assertEqual(self.manager.operations['demo-app']['status'], 'needs_attention')
        self.assertEqual(json.loads((self.root / 'demo-app.json').read_text())['status'], 'running')

    def test_reconcile_checks_owned_stack_and_completed_database(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        stack_id = ('arn:aws:cloudformation:ap-northeast-2:123456789012:'
                    'stack/sky-db-demo-app/stack-id')
        def aws(args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': '123456789012'})
            return json.dumps({'Stacks': [{'StackId': stack_id,
                'StackStatus': 'CREATE_COMPLETE', 'Tags': [
                    {'Key': 'sky-managed', 'Value': 'true'},
                    {'Key': 'sky-app', 'Value': 'demo-app'}]}]})
        with patch('application.postgres_operations.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}) as inspect, \
                patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                      side_effect=lambda _self, args, **kwargs: aws(args, **kwargs)):
            result = self.manager.reconcile('demo-app')
        inspect.assert_called_once_with()
        self.assertEqual(result['status'], 'succeeded')

    def test_reconcile_refuses_unowned_stack(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.plan(self.request)
            self.manager.start('demo-app', plan['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        wrong = {'Stacks': [{'StackId': ('arn:aws:cloudformation:ap-northeast-2:'
            '123456789012:stack/sky-db-demo-app/stack-id'),
            'StackStatus': 'CREATE_COMPLETE', 'Tags': []}]}
        with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                   side_effect=lambda _self, args, **_kwargs: json.dumps(
                       {'Account': '123456789012'} if args[0] == 'sts' else wrong)), \
                patch('application.postgres_operations.AwsPostgresProvisioner.inspect_current') as inspect:
            result = self.manager.reconcile('demo-app')
        self.assertEqual(result['status'], 'needs_attention')
        inspect.assert_not_called()

    def test_reconcile_reports_owned_rollback_without_treating_it_as_database(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            planned = self.manager.plan(self.request)
            self.manager.start('demo-app', planned['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        stack_id = ('arn:aws:cloudformation:ap-northeast-2:123456789012:'
                    'stack/sky-db-demo-app/stack-id')
        def aws(_adapter, args, **_kwargs):
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': '123456789012'})
            return json.dumps({'Stacks': [{'StackId': stack_id,
                'StackStatus': 'ROLLBACK_COMPLETE', 'Tags': [
                    {'Key': 'sky-managed', 'Value': 'true'},
                    {'Key': 'sky-app', 'Value': 'demo-app'}]}]})
        with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                   side_effect=aws), \
                patch('application.postgres_operations.AwsPostgresProvisioner.inspect_current') as inspect:
            result = self.manager.reconcile('demo-app')
        self.assertEqual(result['status'], 'needs_attention')
        self.assertIn('ROLLBACK_COMPLETE', result['message'])
        self.assertIsNone(result['database_id'])
        inspect.assert_not_called()

    def _failed_operation(self):
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.threading.Thread.start'):
            planned = self.manager.plan(self.request)
            self.manager.start('demo-app', planned['plan_id'])
        self.manager.operations['demo-app']['status'] = 'needs_attention'
        self.manager._save(self.manager.operations['demo-app'])

    def _rollback_aws(self, *, residue=False, database=False):
        stack_id = ('arn:aws:cloudformation:ap-northeast-2:123456789012:'
                    'stack/sky-db-demo-app/stack-id')
        state = {'status': 'ROLLBACK_COMPLETE', 'calls': []}
        def aws(_adapter, args, **_kwargs):
            state['calls'].append(args)
            if args[:2] == ['sts', 'get-caller-identity']:
                return json.dumps({'Account': '123456789012'})
            if args[:2] == ['cloudformation', 'describe-stacks']:
                return json.dumps({'Stacks': [{'StackId': stack_id,
                    'StackStatus': state['status'], 'Tags': [
                        {'Key': 'sky-managed', 'Value': 'true'},
                        {'Key': 'sky-app', 'Value': 'demo-app'}]}]})
            if args[:2] == ['cloudformation', 'list-stack-resources']:
                return json.dumps({'StackResourceSummaries': [{
                    'LogicalResourceId': 'Database', 'ResourceStatus':
                    'DELETE_SKIPPED' if residue else 'CREATE_FAILED'}]})
            if args[:2] == ['rds', 'describe-db-instances']:
                return json.dumps({'DBInstances': [
                    {'DBInstanceIdentifier': 'sky-demo-app'}] if database else []})
            if args[:2] == ['rds', 'describe-db-snapshots']:
                return json.dumps({'DBSnapshots': []})
            if args[:2] == ['cloudformation', 'wait']:
                state['status'] = 'DELETE_COMPLETE'
            return '{}'
        return stack_id, state, aws

    def test_failed_create_cleanup_preserves_attempt_and_allows_same_id_retry(self):
        self._failed_operation()
        stack_id, state, aws = self._rollback_aws()
        with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                   side_effect=aws), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.cleanup_plan('demo-app')
            with self.assertRaisesRegex(ValueError, '확인 값'):
                self.manager.cleanup_start('demo-app', plan['plan_id'], stack_id + '-wrong')
            operation = self.manager.cleanup_start('demo-app', plan['plan_id'], stack_id)
            self.assertEqual(operation['status'], 'recovering')
            self.manager._cleanup_run('demo-app')
            self.assertEqual(self.manager.get('demo-app')['status'], 'failed_cleaned')
            self.assertTrue(list((self.root / 'cleaned-attempts').glob('demo-app-*.json')))
            self.assertFalse((self.root / 'demo-app.json').exists())
            self.assertEqual(state['status'], 'DELETE_COMPLETE')
            with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                       return_value=self.quote), \
                    patch('application.postgres_operations.AwsPostgresProvisioner.assert_stack_available') as guard:
                self.manager.plan(self.request)
            guard.assert_called_once_with(allow_deleted=True)
            restored = PostgresOperations(self.root, self.settings)
            self.assertEqual(restored.get('demo-app')['status'], 'failed_cleaned')

    def test_failed_create_cleanup_rejects_retained_resources_and_existing_db(self):
        self._failed_operation()
        for residue, database in [(True, False), (False, True)]:
            _, state, aws = self._rollback_aws(residue=residue, database=database)
            with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                       side_effect=aws):
                with self.assertRaises(AwsConfigurationError):
                    self.manager.cleanup_plan('demo-app')
            self.assertFalse(any(args[:2] == ['cloudformation', 'delete-stack']
                                 for args in state['calls']))

    def test_interrupted_cleanup_requires_fresh_explicit_plan(self):
        self._failed_operation()
        stack_id, state, aws = self._rollback_aws()
        with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                   side_effect=aws), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.cleanup_plan('demo-app')
            self.manager.cleanup_start('demo-app', plan['plan_id'], stack_id)
        restored = PostgresOperations(self.root, self.settings)
        self.assertEqual(restored.get('demo-app')['status'], 'needs_attention')
        with self.assertRaisesRegex(ValueError, '계획'):
            restored.cleanup_start('demo-app', plan['plan_id'], stack_id)
        self.assertEqual(state['status'], 'ROLLBACK_COMPLETE')
        self.assertFalse(any(args[:2] == ['cloudformation', 'delete-stack']
                             for args in state['calls']))

    def test_interrupted_after_stack_deletion_finalizes_without_repeating_delete(self):
        self._failed_operation()
        stack_id, state, aws = self._rollback_aws()
        with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                   side_effect=aws), \
                patch('application.postgres_operations.threading.Thread.start'):
            plan = self.manager.cleanup_plan('demo-app')
            self.manager.cleanup_start('demo-app', plan['plan_id'], stack_id)
            state['status'] = 'DELETE_COMPLETE'
            restored = PostgresOperations(self.root, self.settings)
            self.assertEqual(restored.get('demo-app')['status'], 'needs_attention')
            new_plan = restored.cleanup_plan('demo-app')
            self.assertEqual(new_plan['stack_status'], 'DELETE_COMPLETE')
            restored.cleanup_start('demo-app', new_plan['plan_id'], stack_id)
            restored._cleanup_run('demo-app')
        self.assertEqual(restored.get('demo-app')['status'], 'failed_cleaned')
        self.assertFalse(any(args[:2] in [
            ['cloudformation', 'delete-stack'],
            ['cloudformation', 'update-termination-protection']]
            for args in state['calls']))

    def test_cleaned_app_replan_rechecks_for_orphan_db_at_plan_and_start(self):
        self.manager.cleaned.add('demo-app')
        _, _, safe_aws = self._rollback_aws()
        _, _, orphan_aws = self._rollback_aws(database=True)
        with patch('application.postgres_operations.AwsPostgresProvisioner.preflight',
                   return_value=self.quote), \
                patch('application.postgres_operations.AwsPostgresProvisioner.assert_stack_available') as guard:
            with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                       side_effect=orphan_aws):
                with self.assertRaisesRegex(AwsConfigurationError, 'DB가 없음을'):
                    self.manager.plan(self.request)
            guard.assert_not_called()
            with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                       side_effect=safe_aws):
                plan = self.manager.plan(self.request)
            with patch('adapters.aws.postgres.AwsExpressAdapter.aws', autospec=True,
                       side_effect=orphan_aws), \
                    patch('application.postgres_operations.threading.Thread.start') as worker:
                with self.assertRaisesRegex(AwsConfigurationError, 'DB가 없음을'):
                    self.manager.start('demo-app', plan['plan_id'])
            worker.assert_not_called()
        self.assertFalse((self.root / 'demo-app.json').exists())


if __name__ == '__main__':
    unittest.main()
