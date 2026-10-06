import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sky_platform.deployment.aws import AwsConfigurationError
from sky_platform.database.postgres import AwsPostgresProvisioner
from tests.smoke_aws_postgres_rollback_drill import (retire_before_create,
                                                      start_after_stable_preflight)


class RollbackDrillCleanupTests(unittest.TestCase):
    def setUp(self):
        self.network = Mock()
        self.created = {'stack_id': 'owned-network', 'service_security_group': 'sg-owned'}
        self.database = Mock(spec=AwsPostgresProvisioner)
        self.database.request = SimpleNamespace(application_id='dbdrill-1234abcd')
        self.manager = SimpleNamespace(operations={}, untrusted_unknown=False,
                                       untrusted_applications=set())

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_retires_only_after_confirming_database_and_stack_absence(self, retire):
        retire_before_create(self.network, self.created, self.database, self.manager)
        self.database.assert_database_absent.assert_called_once_with()
        self.database.assert_stack_available.assert_called_once_with()
        retire.assert_called_once_with(self.network, 'owned-network', 'sg-owned')

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_preserves_network_if_creation_was_recorded(self, retire):
        self.manager.operations['dbdrill-1234abcd'] = {'status': 'needs_attention'}
        with self.assertRaises(AwsConfigurationError):
            retire_before_create(self.network, self.created, self.database, self.manager)
        self.database.assert_database_absent.assert_not_called()
        retire.assert_not_called()

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_preserves_network_if_database_absence_is_uncertain(self, retire):
        self.database.assert_database_absent.side_effect = AwsConfigurationError('unknown')
        with self.assertRaises(AwsConfigurationError):
            retire_before_create(self.network, self.created, self.database, self.manager)
        self.database.assert_stack_available.assert_not_called()
        retire.assert_not_called()

    @patch('tests.smoke_aws_postgres_rollback_drill.retire_probe')
    def test_preserves_network_if_stack_absence_is_uncertain(self, retire):
        self.database.assert_stack_available.side_effect = AwsConfigurationError('unknown')
        with self.assertRaises(AwsConfigurationError):
            retire_before_create(self.network, self.created, self.database, self.manager)
        retire.assert_not_called()

    @patch('tests.smoke_aws_postgres_rollback_drill.time.sleep')
    def test_retries_only_before_creation_is_recorded(self, sleep):
        request = SimpleNamespace(application_id='dbdrill-1234abcd')
        manager = Mock()
        manager.operations = {}
        manager.plan.return_value = {'plan_id': 'reviewed'}
        manager.start.side_effect = [AwsConfigurationError('기본 PostgreSQL 버전의 암호화된 구성 조회 실패'),
                                     {'status': 'running'}]
        self.assertEqual(start_after_stable_preflight(manager, request), {'status': 'running'})
        self.assertEqual(manager.start.call_count, 2)
        sleep.assert_called_once_with(10)

    @patch('tests.smoke_aws_postgres_rollback_drill.time.sleep')
    def test_never_retries_after_creation_is_recorded(self, sleep):
        request = SimpleNamespace(application_id='dbdrill-1234abcd')
        manager = Mock()
        manager.operations = {'dbdrill-1234abcd': {'status': 'running'}}
        manager.plan.return_value = {'plan_id': 'reviewed'}
        manager.start.side_effect = AwsConfigurationError('기본 PostgreSQL 버전의 암호화된 구성 조회 실패')
        with self.assertRaises(AwsConfigurationError):
            start_after_stable_preflight(manager, request)
        manager.start.assert_called_once()
        sleep.assert_not_called()

    @patch('tests.smoke_aws_postgres_rollback_drill.time.sleep')
    def test_does_not_retry_other_configuration_failures(self, sleep):
        request = SimpleNamespace(application_id='dbdrill-1234abcd')
        manager = Mock()
        manager.operations = {}
        manager.plan.side_effect = AwsConfigurationError('AWS 계정 불일치')
        with self.assertRaises(AwsConfigurationError):
            start_after_stable_preflight(manager, request)
        manager.plan.assert_called_once()
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
