"""Cloud Run scaling and request limits follow the app: long-lived WebSocket apps get a long timeout."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.gcp.cloud_run import (WEBSOCKET_CONCURRENCY, WEBSOCKET_TIMEOUT_SECONDS, CloudConfigurationError,
                                     CloudRunSettings, service_limits)


def project(files):
    folder = Path(tempfile.mkdtemp(prefix='sky-limits-'))
    for name, text in files.items():
        (folder / name).write_text(text, encoding='utf-8')
    return folder


class CloudRunLimitTests(unittest.TestCase):
    def test_websocket_app_keeps_connections_open(self):
        app = project({'package.json': '{"dependencies":{"ws":"8.0.0"}}',
                       'server.js': "const { WebSocketServer } = require('ws');\nnew WebSocketServer({ port: 3000 });\n"})
        limits = service_limits(app)
        self.assertIn(f'--timeout={WEBSOCKET_TIMEOUT_SECONDS}s', limits)
        self.assertIn(f'--concurrency={WEBSOCKET_CONCURRENCY}', limits)
        self.assertIn('--session-affinity', limits)
        self.assertEqual(WEBSOCKET_TIMEOUT_SECONDS, 3600)   # Cloud Run maximum
        self.assertIn('--max-instances=1', limits)          # in-memory state stays on one instance

    def test_http_app_keeps_the_previous_limits(self):
        app = project({'package.json': '{}', 'server.js': "require('http').createServer().listen(3000);\n"})
        self.assertEqual(service_limits(app),
                         ['--min-instances=0', '--max-instances=1', '--concurrency=20', '--timeout=60s'])

    def test_warm_instance_is_opt_in_and_validated(self):
        app = project({'package.json': '{}', 'server.js': "require('http').createServer().listen(3000);\n"})
        self.assertIn('--min-instances=1', service_limits(app, 1))
        for value, expected in (('', 0), ('1', 1), ('abc', -1), ('2', 2)):
            with patch.dict('os.environ', {'SKY_GCP_MIN_INSTANCES': value}):
                self.assertEqual(CloudRunSettings.from_environment().min_instances, expected)
        valid = CloudRunSettings('test-project', 'asia-northeast3')
        valid.validate()
        for bad in (-1, 2):
            with self.assertRaisesRegex(CloudConfigurationError, 'SKY_GCP_MIN_INSTANCES'):
                CloudRunSettings('test-project', 'asia-northeast3', min_instances=bad).validate()

    def test_settings_restored_from_older_jobs_default_to_scale_to_zero(self):
        stored = {'project': 'test-project', 'region': 'asia-northeast3', 'repository': 'sky', 'service_account': ''}
        self.assertEqual(CloudRunSettings(**stored).min_instances, 0)


if __name__ == '__main__':
    unittest.main()
