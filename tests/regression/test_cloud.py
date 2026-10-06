import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adapters.gcp.cloud_run import CloudConfigurationError, CloudRunAdapter, CloudRunSettings
from application.analysis import AISettings
from application.deployment_core import analyze
from interfaces.http.server import App
from tests.support.agent_fixture import RepairFixture


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
        self.plan = replace(analyze(self.project), target='cloud-run')
        self.settings = CloudRunSettings('test-project', 'asia-northeast3')
        self.attempt = 'a' * 16 + '-a1'
        self.events, self.commands, self.private_files = [], [], []
        self.adapter = CloudRunAdapter(lambda s, m: self.events.append((s, m)), self.settings)
        self.deployed = False
        self.fail_deploy = False
        self.existing_service = False
        self.existing_infrastructure = False
        self.owner = self.attempt

    def execute(self, args, **kwargs):
        self.commands.append((args, kwargs))
        output = ''
        if args[0] == 'docker':
            if args[1:3] == ['image', 'inspect']:
                output = 'linux/amd64'
            elif 'inspect' in args:
                output = 'unix:///test/docker.sock'
            if 'login' in args:
                self.assertEqual(kwargs['input'], 'private-access-token')
                directory = Path(args[args.index('--config') + 1])
                self.assertTrue(directory.is_dir())
                self.private_files.append(directory)
            return subprocess.CompletedProcess(args, 0, output, '')
        self.assertEqual(args[0], 'gcloud')
        self.assertEqual(args[args.index('--project') + 1], self.settings.project)
        self.assertIn('--quiet', args)
        if args[1:4] == ['artifacts', 'repositories', 'list']:
            output = json.dumps([{'name': 'projects/test-project/locations/asia-northeast3/repositories/sky', 'format': 'DOCKER'}] if self.existing_infrastructure else [])
        elif args[1:4] == ['iam', 'service-accounts', 'list']:
            output = json.dumps([{'email': self.settings.runtime_identity}] if self.existing_infrastructure else [])
        elif args[1:4] == ['run', 'services', 'list']:
            output = json.dumps([{'metadata': {'name': 'sky-' + self.attempt,
                'labels': {'sky-managed': 'true', 'sky-attempt': self.owner}}}] if self.deployed or self.existing_service else [])
        elif args[1:3] == ['auth', 'print-access-token']:
            output = 'private-access-token'
        elif args[1:3] == ['auth', 'print-identity-token']:
            output = 'private-identity-token'
        elif args[1:3] == ['run', 'deploy']:
            path = Path(args[args.index('--env-vars-file') + 1])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {'APP_SECRET': 'private-app-value'})
            self.private_files.append(path)
            self.deployed = True
            if self.fail_deploy:
                return subprocess.CompletedProcess(args, 1, '', 'container failed to start')
            output = json.dumps({'status': {'url': 'https://sky-example.a.run.app', 'latestReadyRevisionName': 'revision-1'},
                                 'spec': {'sensitive': 'private-app-value'}})
        elif args[1:4] == ['run', 'services', 'delete']:
            self.deployed = False
        elif args[1:3] == ['logging', 'read']:
            output = 'application error private-app-value'
        return subprocess.CompletedProcess(args, 0, output, '')

    def deploy(self):
        with patch('adapters.gcp.cloud_run.shutil.which', return_value='/bin/gcloud'), patch('adapters.gcp.cloud_run.subprocess.run', side_effect=self.execute), patch.object(self.adapter, 'verify') as verify:
            result = self.adapter.deploy(self.project, self.plan, self.attempt, {'APP_SECRET': 'private-app-value'})
            return result, verify

    def test_full_private_deployment_build_push_infrastructure_and_probe(self):
        result, verify = self.deploy()
        self.assertEqual(result['target'], 'cloud-run')
        self.assertFalse(result['public'])
        self.assertEqual(result['revision'], 'revision-1')
        verify.assert_called_once_with('https://sky-example.a.run.app/', 'private-identity-token')
        build = next(args for args, _ in self.commands if args[:2] == ['docker', 'build'])
        self.assertEqual(build[build.index('--platform') + 1], 'linux/amd64')
        run = next(args for args, _ in self.commands if args[:3] == ['gcloud', 'run', 'deploy'])
        self.assertIn('--no-allow-unauthenticated', run)
        self.assertIn('--max-instances=1', run)
        self.assertIn(self.settings.runtime_identity, run)
        self.assertTrue(any(args[1:4] == ['artifacts', 'repositories', 'create'] for args, _ in self.commands))
        self.assertTrue(any(args[1:4] == ['iam', 'service-accounts', 'create'] for args, _ in self.commands))
        self.assertTrue(all(not p.exists() for p in self.private_files))
        logged = json.dumps(self.events)
        for value in ('private-access-token', 'private-identity-token', 'private-app-value'):
            self.assertNotIn(value, logged)
            self.assertFalse(any(value in ' '.join(args) for args, _ in self.commands))

    def test_public_requires_explicit_selection(self):
        self.adapter.public = True
        result, verify = self.deploy()
        self.assertTrue(result['public'])
        self.assertFalse(any('print-identity-token' in args for args, _ in self.commands))
        self.assertTrue(any('--allow-unauthenticated' in args for args, _ in self.commands))
        verify.assert_called_once_with('https://sky-example.a.run.app/', None)

    def test_existing_infrastructure_is_reused(self):
        self.existing_infrastructure = True
        self.deploy()
        self.assertFalse(any('create' in args for args, _ in self.commands))

    def test_existing_service_is_never_overwritten_or_deleted(self):
        self.existing_service = True
        with self.assertRaises(CloudConfigurationError):
            self.deploy()
        with patch('adapters.gcp.cloud_run.subprocess.run', side_effect=self.execute):
            self.adapter.cleanup_failure(self.attempt)
        self.assertFalse(any(args[1:3] == ['run', 'deploy'] or 'delete' in args for args, _ in self.commands))

    def test_failed_attempt_collects_logs_and_cleans_owned_resources(self):
        self.fail_deploy = True
        with self.assertRaises(RuntimeError):
            self.deploy()
        with patch('adapters.gcp.cloud_run.subprocess.run', side_effect=self.execute):
            self.adapter.cleanup_failure(self.attempt)
        self.assertTrue(any(args[1:4] == ['run', 'services', 'delete'] for args, _ in self.commands))
        self.assertTrue(any(args[1:5] == ['artifacts', 'docker', 'images', 'delete'] for args, _ in self.commands))
        self.assertIn('[REDACTED]', str(self.events))
        self.assertNotIn('private-app-value', str(self.events))
        self.assertTrue(all(not p.exists() for p in self.private_files))

    def test_cleanup_does_not_delete_foreign_service(self):
        self.adapter.service_attempted = True
        self.adapter.pushed = True
        self.adapter.image = f'{self.settings.region}-docker.pkg.dev/{self.settings.project}/{self.settings.repository}/sky-{self.attempt}:latest'
        self.deployed = True
        self.owner = 'other-attempt'
        with patch('adapters.gcp.cloud_run.subprocess.run', side_effect=self.execute):
            self.adapter.cleanup_failure(self.attempt)
        self.assertFalse(any('delete' in args for args, _ in self.commands))

    def test_invalid_configuration_and_urls(self):
        for settings in (CloudRunSettings(), replace(self.settings, project='--other'),
                         replace(self.settings, region='../region'), replace(self.settings, repository='bad/repo'),
                         replace(self.settings, service_account='a@another-project.iam.gserviceaccount.com')):
            with self.subTest(settings=settings), self.assertRaises(CloudConfigurationError):
                settings.validate()
        for url in ('http://x.run.app', 'https://example.com', 'https://x.run.app.evil.com',
                    'https://user@x.run.app', 'https://x.run.app/path', 'https://x.run.app?token=secret'):
            with self.subTest(url=url), self.assertRaises(CloudConfigurationError):
                self.adapter.validate_url(url)

    def test_missing_cli_disables_cloud(self):
        with patch('adapters.gcp.cloud_run.shutil.which', return_value=None):
            self.assertIn('gcloud', self.settings.unavailable_reason())
            with self.assertRaises(CloudConfigurationError):
                self.adapter.prepare_infrastructure()

    def test_iam_failure_is_not_an_application_retry(self):
        with patch('adapters.gcp.cloud_run.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'PERMISSION_DENIED')):
            with self.assertRaises(CloudConfigurationError) as raised:
                self.adapter.gcloud(['services', 'enable', 'run.googleapis.com'], private=True)
        self.assertFalse(raised.exception.retryable)

    def test_probe_does_not_follow_redirect_with_identity_token(self):
        error = urllib.error.HTTPError('https://app.run.app/', 302, 'redirect', {'Location': 'https://attacker.example'}, io.BytesIO())
        with patch('adapters.gcp.cloud_run.urllib.request.build_opener') as opener, patch('adapters.gcp.cloud_run.time.sleep'):
            opener.return_value.open.side_effect = lambda *a, **kw: (_ for _ in ()).throw(error)
            with self.assertRaisesRegex(RuntimeError, 'HTTP 200'):
                self.adapter.verify('https://app.run.app/', 'private-id')
            handler = opener.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, None, None, None, None))
            self.assertTrue(all(call.args[0].full_url == 'https://app.run.app/' for call in opener.return_value.open.call_args_list))

    def test_private_probe_rejects_authorization_failure(self):
        with patch('adapters.gcp.cloud_run.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError('https://app.run.app/', 403, 'denied', {}, io.BytesIO())
            with self.assertRaises(CloudConfigurationError):
                self.adapter.verify('https://app.run.app/', 'private-id')

    def test_job_dispatches_agent_to_cloud_target_and_pins_settings(self):
        state = self.project / 'state'
        job_id = 'b' * 16
        source = state / job_id / 'source'
        source.mkdir(parents=True)
        (source / 'package.json').write_text('''{
  "name": "unready-node",
  "version": "1.0.0",
  "private": true,
  "scripts": {}
}
''')
        (source / 'server.js').write_text("http.createServer(() => {}).listen(4321, '127.0.0.1', () => {});")
        # This test exercises dispatch and the agent-to-adapter boundary without cloud credentials.
        observed = []
        class CloudFixture:
            def __init__(self, event, settings, public=False):
                observed.append((settings, public))
                self.event = event
            def deploy(self, project, plan, attempt_id, environment):
                self.event('output', 'fixture cloud execution')
                if len([x for x in observed if isinstance(x, str)]) == 0:
                    observed.append('failed-once')
                    raise RuntimeError('App did not respond on loopback-only binding')
                assert plan.target == 'cloud-run'
                assert "'0.0.0.0'" in (project / 'server.js').read_text()
                return {'url': 'https://fixture.a.run.app', 'health_url': 'https://fixture.a.run.app/',
                        'target': 'cloud-run', 'public': True}
            def cleanup_failure(self, attempt_id):
                pass
        app = App(state, AISettings('fixture', 'fixture-model'), agent_factory=RepairFixture,
                  cloud_settings=self.settings)
        app.jobs[job_id] = {'id': job_id, 'mode': 'agent', 'target': 'cloud-run', 'public': True,
                            'cloud': self.settings.__dict__, 'plan': None, 'status': 'running',
                            'project': str(source), 'events': [], 'changes': [], 'attempts': 0, 'steps': 0}
        app.save(job_id)
        with patch('interfaces.http.server.CloudRunAdapter', CloudFixture):
            app.run_agent(job_id)
        result = app.jobs[job_id]
        self.assertEqual(result['status'], 'succeeded', result['events'][-1])
        self.assertEqual(result['result']['target'], 'cloud-run')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(observed[0], (self.settings, True))
        self.assertEqual(result['cloud'], self.settings.__dict__)


if __name__ == '__main__':
    unittest.main()
