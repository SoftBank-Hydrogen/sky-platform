import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.api.server import App


JOB_ID = 'f' * 16


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / JOB_ID / 'source'
        source.mkdir(parents=True)
        self.app = App(self.root)
        self.app.jobs[JOB_ID] = {'id': JOB_ID, 'mode': 'agent', 'project': str(source),
                                 'status': 'succeeded', 'target': 'local-docker',
                                 'application_id': 'monitor-app', 'deployment_state': 'active',
                                 'result': {'container': f'sky-{JOB_ID}-a1',
                                            'image': f'sky/{JOB_ID}-a1:latest',
                                            'url': 'http://127.0.0.1:49152'}, 'events': []}
        self.app.save(JOB_ID)

    def test_automatic_failure_is_persisted_without_changing_deployment_result(self):
        finding = {'healthy': False, 'checked_at': '2026-09-30T00:00:00+00:00',
                   'reason': 'Service did not return HTTP 200'}
        with patch('sky_platform.api.server.check_deployment', return_value=finding) as check:
            self.app.monitor_once()
            check.assert_called_once()
        self.assertEqual(self.app.jobs[JOB_ID]['status'], 'succeeded')
        self.assertEqual(self.app.summaries()[0]['last_health']['healthy'], False)
        self.assertEqual(self.app.health_history[JOB_ID][0]['source'], 'automatic')
        recovered = App(self.root)
        self.assertEqual(recovered.health_history[JOB_ID], self.app.health_history[JOB_ID])
        self.assertEqual(recovered.jobs[JOB_ID]['status'], 'succeeded')

    def test_monitor_skips_active_release_during_update_and_after_retirement(self):
        other_id = '1' * 16
        source = self.root / other_id / 'source'
        source.mkdir(parents=True)
        self.app.jobs[other_id] = {'id': other_id, 'mode': 'agent', 'project': str(source),
                                   'status': 'running', 'target': 'local-docker',
                                   'application_id': 'monitor-app', 'events': []}
        with patch('sky_platform.api.server.check_deployment') as check:
            self.app.monitor_once()
            check.assert_not_called()
            self.app.jobs.pop(other_id)
            self.app.jobs[JOB_ID]['deployment_state'] = 'deleted'
            self.app.monitor_once()
            check.assert_not_called()

    def test_monitor_record_failure_does_not_downgrade_successful_job(self):
        finding = {'healthy': True, 'checked_at': '2026-09-30T00:00:00+00:00',
                   'reason': 'HTTP 200 confirmed'}
        with patch('sky_platform.api.server.check_deployment', return_value=finding), \
                patch('sky_platform.api.server.tempfile.mkstemp', side_effect=OSError('disk full')):
            self.assertEqual(self.app.check_and_record_health(JOB_ID), finding)
        self.assertEqual(self.app.jobs[JOB_ID]['status'], 'succeeded')
        self.assertNotIn(JOB_ID, self.app.health_history)
        self.assertIn('disk full', self.app.monitor_errors[JOB_ID])

    def test_check_during_retirement_is_not_recorded_as_current(self):
        finding = {'healthy': False, 'checked_at': '2026-09-30T00:00:00+00:00',
                   'reason': 'Container is missing'}
        def check(_snapshot):
            self.app.jobs[JOB_ID]['deployment_state'] = 'deleted'
            return finding
        with patch('sky_platform.api.server.check_deployment', side_effect=check):
            self.assertEqual(self.app.check_and_record_health(JOB_ID), finding)
        self.assertNotIn(JOB_ID, self.app.health_history)

    def test_check_started_before_update_does_not_record_transient_result(self):
        other_id = '2' * 16
        finding = {'healthy': False, 'checked_at': '2026-09-30T00:00:00+00:00',
                   'reason': 'Another revision is starting'}
        def check(_snapshot):
            self.app.jobs[other_id] = {'id': other_id, 'application_id': 'monitor-app',
                                       'target': 'local-docker', 'status': 'running'}
            return finding
        with patch('sky_platform.api.server.check_deployment', side_effect=check):
            self.assertEqual(self.app.check_and_record_health(JOB_ID, 'automatic'), finding)
        self.assertNotIn(JOB_ID, self.app.health_history)


if __name__ == '__main__':
    unittest.main()
