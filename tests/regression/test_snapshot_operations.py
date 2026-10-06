import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.aws import AwsSettings
from database.snapshot_operations import SnapshotOperations


APP = 'demo-app'
SNAPSHOT = 'sky-demo-app-before-migration'
PLAN = {'application_id': APP, 'database_id': 'sky-demo-app',
        'snapshot_id': SNAPSHOT, 'snapshot_arn':
        'arn:aws:rds:ap-northeast-2:123456789012:snapshot:' + SNAPSHOT,
        'account': '123456789012', 'region': 'ap-northeast-2',
        'manual_snapshot_count': 0, 'storage_cost_warning': 'cost'}


class SnapshotOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'snapshots'
        self.settings = AwsSettings('ap-northeast-2', expected_account='123456789012',
                                    service_security_group='sg-33333333')
        self.manager = SnapshotOperations(self.root, self.settings)

    def _start(self):
        with patch('database.snapshot_operations.plan_snapshot', return_value=PLAN), \
                patch('database.snapshot_operations.threading.Thread.start') as thread:
            planned = self.manager.plan(APP, SNAPSHOT)
            operation = self.manager.start(APP, planned['plan_id'])
        thread.assert_called_once()
        return operation

    def test_acceptance_is_journaled_before_worker_and_restart_requires_reconcile(self):
        self.assertEqual(self._start()['status'], 'running')
        saved = json.loads((self.root / (SNAPSHOT + '.json')).read_text())
        self.assertEqual(saved['snapshot_id'], SNAPSHOT)
        restored = SnapshotOperations(self.root, self.settings)
        self.assertEqual(restored.get(APP, SNAPSHOT)['status'], 'needs_attention')
        with patch('database.snapshot_operations.plan_snapshot') as plan:
            with self.assertRaisesRegex(ValueError, '이전'):
                restored.plan(APP, 'sky-demo-app-another')
        plan.assert_not_called()

    def test_changed_plan_rejects_without_journal_or_worker(self):
        with patch('database.snapshot_operations.plan_snapshot',
                   side_effect=[PLAN, {**PLAN, 'manual_snapshot_count': 1}]), \
                patch('database.snapshot_operations.threading.Thread.start') as thread:
            planned = self.manager.plan(APP, SNAPSHOT)
            with self.assertRaisesRegex(ValueError, '변경'):
                self.manager.start(APP, planned['plan_id'])
        thread.assert_not_called()
        self.assertFalse((self.root / (SNAPSHOT + '.json')).exists())

    def test_worker_and_reconcile_do_not_repeat_create(self):
        self._start()
        with patch('database.snapshot_operations.create_snapshot', return_value={
                'status': 'creating'}) as create:
            self.manager._run(SNAPSHOT)
        create.assert_called_once_with(APP, SNAPSHOT, self.settings)
        self.assertEqual(self.manager.get(APP, SNAPSHOT)['status'], 'pending')
        with patch('database.snapshot_operations.inspect_snapshot',
                   return_value={'status': 'available'}) as inspect:
            operation = self.manager.reconcile(APP, SNAPSHOT)
        inspect.assert_called_once_with(APP, SNAPSHOT, self.settings)
        self.assertEqual(operation['status'], 'succeeded')
        self.assertEqual(SnapshotOperations(self.root, self.settings).get(APP, SNAPSHOT)['status'],
                         'succeeded')

    def test_uncertain_worker_can_be_reconciled_without_retry(self):
        self._start()
        with patch('database.snapshot_operations.create_snapshot',
                   side_effect=RuntimeError('timeout')):
            self.manager._run(SNAPSHOT)
        self.assertEqual(self.manager.get(APP, SNAPSHOT)['status'], 'needs_attention')
        with patch('database.snapshot_operations.inspect_snapshot',
                   return_value={'status': 'creating'}):
            self.assertEqual(self.manager.reconcile(APP, SNAPSHOT)['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
