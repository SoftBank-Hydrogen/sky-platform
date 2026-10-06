import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from application.analysis import AISettings
from application.deployment_core import extract_project, folder_upload_to_zip
from interfaces.http.server import App, handler_for


BOUNDARY = 'sky-test-boundary'
CONTENT_TYPE = 'multipart/form-data; boundary=' + BOUNDARY


def multipart(files):
    body = bytearray()
    for path, data in files:
        for name, content, filename in [('path', path.encode(), None), ('file', data, path.rsplit('/', 1)[-1])]:
            body.extend(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"'.encode())
            if filename:
                body.extend(f'; filename="{filename}"'.encode())
            body.extend(b'\r\n\r\n' + content + b'\r\n')
    body.extend(f'--{BOUNDARY}--\r\n'.encode())
    return bytes(body)


class FolderUploadTests(unittest.TestCase):
    def test_folder_upload_reuses_zip_extraction_and_excludes_secrets(self):
        body = multipart([('my-app/package.json', b'{"scripts":{"start":"node server.js"}}'),
                          ('my-app/assets/logo.bin', b'\x00\xff\r\n'),
                          ('my-app/.env', b'SECRET=bad'),
                          ('my-app/.git/config', b'private'),
                          ('my-app/node_modules/dependency.js', b'unneeded')])
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'source.zip'
            folder_upload_to_zip(body, CONTENT_TYPE, archive)
            project = extract_project(archive, Path(directory) / 'source')
            self.assertEqual(project.name, 'my-app')
            self.assertEqual((project / 'assets/logo.bin').read_bytes(), b'\x00\xff\r\n')
            self.assertFalse((project / '.env').exists())
            self.assertFalse((project / '.git').exists())
            self.assertFalse((project / 'node_modules').exists())

    def test_rejects_traversal_and_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'source.zip'
            for files in [[('../bad', b'x')], [('app/a', b'1'), ('app/a', b'2')],
                          [('app\\bad', b'x')], [('app//bad', b'x')], [('app/bad\x00.txt', b'x')]]:
                with self.subTest(files=files), self.assertRaises(ValueError):
                    folder_upload_to_zip(multipart(files), CONTENT_TYPE, archive)

    def test_api_accepts_folder_and_starts_same_deployment_job(self):
        body = multipart([('app/package.json', b'{"scripts":{"start":"node server.js"}}'),
                          ('app/server.js', b'console.log("ready")')])
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token, 'Content-Type': CONTENT_TYPE,
                               'Content-Length': str(len(body)), 'X-Application-Id': 'my-app'}
            handler.rfile = io.BytesIO(body)
            handler.json_response = Mock()
            with patch('interfaces.http.server.threading.Thread'):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 202)
            job_id = handler.json_response.call_args.args[1]['id']
            self.assertEqual((Path(app.jobs[job_id]['project']) / 'server.js').read_bytes(), b'console.log("ready")')


if __name__ == '__main__':
    unittest.main()
