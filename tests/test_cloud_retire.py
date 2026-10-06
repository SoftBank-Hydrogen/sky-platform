import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sky_platform.deployment.cloud import CloudRunAdapter, CloudRunSettings
from sky_platform.api.server import App, handler_for


JOB_ID = 'e' * 16
ATTEMPT = JOB_ID + '-a1'
SERVICE = 'sky-' + ATTEMPT
SETTINGS = CloudRunSettings('test-project', 'asia-northeast3')
IMAGE = f'asia-northeast3-docker.pkg.dev/test-project/sky/{SERVICE}:latest'
RESULT = {'target': 'cloud-run', 'service': SERVICE, 'image': IMAGE,
          'project': SETTINGS.project, 'region': SETTINGS.region,
          'url': 'https://example.a.run.app'}


class CloudRetireTests(unittest.TestCase):
    def test_retire_checks_owner_and_deletes_service_before_image(self):
        adapter = CloudRunAdapter(lambda *_: None, SETTINGS)
        service_exists, image_exists, commands = True, True, []

        def gcloud(args, **_kwargs):
            nonlocal service_exists, image_exists
            commands.append(args)
            if args[:3] == ['run', 'services', 'list']:
                return json.dumps([{'metadata': {'name': SERVICE}}] if service_exists else [])
            if args[:3] == ['run', 'services', 'describe']:
                return json.dumps({'metadata': {'name': SERVICE,
                    'labels': {'sky-managed': 'true', 'sky-attempt': ATTEMPT}},
                    'status': {'url': RESULT['url']},
                    'spec': {'template': {'spec': {'containers': [{'image': IMAGE}]}}}})
            if args[:3] == ['run', 'services', 'delete']:
                service_exists = False
                return ''
            if args[:4] == ['artifacts', 'docker', 'images', 'list']:
                return json.dumps([{'package': IMAGE.removesuffix(':latest'), 'tags': ['latest']}]
                                  if image_exists else [])
            if args[:4] == ['artifacts', 'docker', 'images', 'delete']:
                self.assertFalse(service_exists)
                self.assertNotIn('--delete-tags', args)
                image_exists = False
                return ''
            raise AssertionError(args)

        with patch.object(adapter, 'gcloud', side_effect=gcloud):
            adapter.retire(RESULT, ATTEMPT)
        self.assertFalse(service_exists)
        self.assertFalse(image_exists)
        self.assertLess(next(i for i, args in enumerate(commands) if args[:3] == ['run', 'services', 'delete']),
                        next(i for i, args in enumerate(commands) if args[:4] == ['artifacts', 'docker', 'images', 'delete']))

    def test_foreign_service_is_never_deleted(self):
        adapter = CloudRunAdapter(lambda *_: None, SETTINGS)
        calls = []
        def gcloud(args, **_kwargs):
            calls.append(args)
            if args[:3] == ['run', 'services', 'list']:
                return json.dumps([{'metadata': {'name': SERVICE}}])
            if args[:3] == ['run', 'services', 'describe']:
                return json.dumps({'metadata': {'name': SERVICE,
                    'labels': {'sky-managed': 'true', 'sky-attempt': 'other'}},
                    'status': {'url': RESULT['url']},
                    'spec': {'template': {'spec': {'containers': [{'image': IMAGE}]}}}})
            raise AssertionError(args)
        with patch.object(adapter, 'gcloud', side_effect=gcloud):
            with self.assertRaisesRegex(ValueError, 'ownership changed'):
                adapter.retire(RESULT, ATTEMPT)
        self.assertFalse(any('delete' in args for args in calls))

    def test_retry_after_service_disappears_removes_remaining_image(self):
        adapter = CloudRunAdapter(lambda *_: None, SETTINGS)
        calls = []
        image_exists = True
        def gcloud(args, **_kwargs):
            nonlocal image_exists
            calls.append(args)
            if args[:3] == ['run', 'services', 'list']:
                return '[]'
            if args[:4] == ['artifacts', 'docker', 'images', 'list']:
                return json.dumps([{'package': IMAGE.removesuffix(':latest'), 'tags': ['latest']}]
                                  if image_exists else [])
            if args[:4] == ['artifacts', 'docker', 'images', 'delete']:
                image_exists = False
                return ''
            raise AssertionError(args)
        with patch.object(adapter, 'gcloud', side_effect=gcloud):
            adapter.retire(RESULT, ATTEMPT)
        self.assertFalse(image_exists)
        self.assertFalse(any(args[:3] == ['run', 'services', 'delete'] for args in calls))

    def test_retire_api_dispatches_and_records_failure_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = App(Path(temporary))
            source = app.root / JOB_ID / 'source'
            source.mkdir(parents=True)
            app.jobs[JOB_ID] = {'id': JOB_ID, 'mode': 'agent', 'project': str(source),
                                'status': 'succeeded', 'target': 'cloud-run',
                                'deployment_state': 'active', 'cloud': SETTINGS.__dict__,
                                'result': RESULT, 'events': []}
            app.save(JOB_ID)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = f'/api/jobs/{JOB_ID}/retire'
            handler.headers = {'X-Sky-Token': app.token, 'Content-Length': '0'}
            handler.json_response = Mock()
            with patch('sky_platform.api.server.threading.Thread') as thread:
                handler.do_POST()
            self.assertEqual(app.jobs[JOB_ID]['deployment_state'], 'deleting')
            self.assertEqual(thread.call_args.kwargs['target'], app.retire_cloud)
            with patch('sky_platform.api.server.CloudRunAdapter.retire', side_effect=RuntimeError('gcloud failed')):
                app.retire_cloud(JOB_ID)
            self.assertEqual(app.jobs[JOB_ID]['deployment_state'], 'delete_failed')
            with patch('sky_platform.api.server.CloudRunAdapter.retire'):
                app.retire_cloud(JOB_ID)
            self.assertEqual(app.jobs[JOB_ID]['deployment_state'], 'deleted')


if __name__ == '__main__':
    unittest.main()
