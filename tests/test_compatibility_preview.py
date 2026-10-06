import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

from sky_platform.runtime.analysis import AISettings
from sky_platform.runtime.server import App, handler_for


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return output.getvalue()


class CompatibilityPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name), AISettings(), monitor_interval=0)
        handler_type = handler_for(self.app)
        self.handler = handler_type.__new__(handler_type)
        self.handler.path = '/api/compatibility'
        self.handler.json_response = Mock()

    def preview(self, content, public='true'):
        self.handler.headers = {'X-Sky-Token': self.app.token,
                                'X-Public-Access': public,
                                'Content-Length': str(len(content)),
                                'Content-Type': 'application/zip'}
        self.handler.rfile = io.BytesIO(content)
        self.handler.do_POST()
        return self.handler.json_response.call_args.args

    def test_preview_compares_all_targets_without_ai_or_job_creation(self):
        status, payload = self.preview(archive({
            'package.json': '{"scripts":{"start":"node server.js"}}',
            'server.js': 'require("node:http").createServer((q,r)=>r.end("ok"))'
        }))
        self.assertEqual(status, 200)
        reports = {item['target']: item for item in payload['reports']}
        self.assertEqual(set(reports), {'local-docker', 'aws-ecs-express', 'cloud-run'})
        self.assertTrue(all(item['compatible'] for item in reports.values()))
        self.assertEqual(reports['local-docker']['access_mode'], 'loopback')
        self.assertEqual(reports['aws-ecs-express']['access_mode'], 'public')
        self.assertTrue(all(item['cost']['estimate'] is None for item in reports.values()))
        self.assertEqual(payload['inspection']['requirements'], [])
        self.assertEqual(payload['inspection']['scanned_files'], 2)
        self.assertEqual(self.app.jobs, {})
        self.assertFalse(list(Path(self.temp.name).glob('*/job.json')))

    def test_preview_explains_unsupported_sqlite_on_every_target(self):
        status, payload = self.preview(archive({
            'package.json': '{"dependencies":{"better-sqlite3":"11.0.0"}}',
            'server.js': 'require("better-sqlite3")("app.db")'
        }))
        self.assertEqual(status, 200)
        self.assertTrue(all(not item['compatible'] for item in payload['reports']))
        self.assertTrue(all(any('SQLite' in issue for issue in item['problems'])
                            for item in payload['reports']))
        self.assertIn('sqlite', payload['inspection']['requirements'])
        self.assertIn('package.json', payload['inspection']['evidence_files'])


if __name__ == '__main__':
    unittest.main()
