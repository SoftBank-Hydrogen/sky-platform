import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from application.analysis import AISettings
from application.diagnosis import deployment_diagnosis
from interfaces.http.server import App, handler_for


class DiagnosisTests(unittest.TestCase):
    def test_failed_build_reports_observed_phase_without_copying_private_output(self):
        job = {'status': 'failed', 'events': [
            {'stage': 'building', 'time': '2026-10-06T00:00:00Z', 'message': 'Building'},
            {'stage': 'output', 'time': '2026-10-06T00:00:01Z',
             'message': 'token=synthetic-private-value'},
            {'stage': 'error', 'time': '2026-10-06T00:00:02Z',
             'message': 'Build failed; token=synthetic-private-value'},
        ]}
        report = deployment_diagnosis(job)
        self.assertEqual(report['last_observed_phase'], 'image_build')
        self.assertEqual(report['issue'], 'deployment_failed')
        self.assertFalse(report['root_cause_verified'])
        self.assertNotIn('synthetic-private-value', str(report))
        self.assertEqual(report['evidence'], [
            {'stage': 'building', 'time': '2026-10-06T00:00:00Z'}])

    def test_uncertain_aws_update_never_recommends_retry(self):
        job = {'status': 'interrupted', 'aws_update_submitted': True,
               'events': [{'stage': 'update_accepted', 'time': 'now', 'message': 'accepted'}]}
        report = deployment_diagnosis(job)
        self.assertEqual(report['issue'], 'cloud_outcome_uncertain')
        self.assertEqual(report['last_observed_phase'], 'service_update')
        self.assertIn('재확인', report['recommended_action'])

    def test_input_wait_and_success_have_distinct_results(self):
        self.assertEqual(deployment_diagnosis({'status': 'waiting_input', 'events': []})['issue'],
                         'input_required')
        self.assertIsNone(deployment_diagnosis({'status': 'succeeded', 'events': []}))

    def test_job_api_includes_read_only_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings(), monitor_interval=0)
            job_id = 'a' * 16
            app.jobs[job_id] = {'id': job_id, 'status': 'failed', 'events': [
                {'stage': 'verifying', 'time': 'now', 'message': 'Checking HTTP'},
                {'stage': 'error', 'time': 'now', 'message': 'synthetic-private-value'}]}
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = f'/api/jobs/{job_id}'
            handler.json_response = Mock()
            handler.headers = {'X-Sky-Token': app.token}
            handler.do_GET()
            status, payload = handler.json_response.call_args.args
            self.assertEqual(status, 200)
            self.assertEqual(payload['diagnosis']['last_observed_phase'], 'http_verification')
            self.assertNotIn('synthetic-private-value', str(payload['diagnosis']))


if __name__ == '__main__':
    unittest.main()
