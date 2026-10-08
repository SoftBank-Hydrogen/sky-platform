import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from application.agent import DeploymentTools
from adapters.local.docker import LocalDockerAdapter
from application.analysis import AISettings
from adapters.aws.ecs import AwsSettings
from application.infrastructure import (InfrastructureProfile, deployment_access_mode,
                                      explicit_infrastructure_plan,
                                      infrastructure_compatibility, inspect_infrastructure,
                                      plan_infrastructure,
                                      validate_infrastructure,
                                      validate_infrastructure_proposal, OpenAIInfrastructurePlanner)
from interfaces.http.server import App, handler_for
from application.deployment_core import analyze
from adapters.database.migrations import collect_sql_migrations


class InfrastructureTests(unittest.TestCase):
    def test_access_mode_matches_actual_target_exposure(self):
        self.assertEqual(deployment_access_mode('local-docker', True), 'loopback')
        self.assertEqual(deployment_access_mode('cloud-run', False), 'authenticated')
        self.assertEqual(deployment_access_mode('cloud-run', True), 'public')
        self.assertIsNone(deployment_access_mode('aws-ecs-express', False))
        self.assertEqual(deployment_access_mode('aws-ecs-express', True), 'public')

    def test_python_postgres_probe_is_supported_only_with_aws_database_binding(self):
        project = Path('tests/fixtures/apps/postgres-probe-python')
        profile = inspect_infrastructure(project)
        self.assertEqual(profile.database_engines, ('postgresql',))
        self.assertEqual(analyze(project).runtime, 'python-wsgi')
        self.assertEqual(len(collect_sql_migrations(project).migrations), 1)
        validate_infrastructure(profile, 'aws-ecs-express', postgres=True)
        with self.assertRaisesRegex(ValueError, 'postgresql'):
            validate_infrastructure(profile, 'local-docker')

    def test_target_compatibility_requires_real_database_binding_and_records_evidence(self):
        profile = inspect_infrastructure(Path('tests/fixtures/apps/postgres-probe-python'))
        missing = infrastructure_compatibility(profile, 'aws-ecs-express')
        supported = infrastructure_compatibility(profile, 'aws-ecs-express', postgres=True)
        other_target = infrastructure_compatibility(profile, 'cloud-run', postgres=True)
        self.assertFalse(missing['compatible'])
        self.assertTrue(supported['compatible'])
        self.assertFalse(other_target['compatible'])
        self.assertEqual(supported['database_engines'], ['postgresql'])
        self.assertTrue(supported['evidence'])
        self.assertFalse(other_target['adapter_capabilities']['postgresql_binding'])

    def test_final_dockerfile_platform_is_checked_for_cloud_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            dockerfile = project / 'Dockerfile'
            dockerfile.write_text('FROM --platform=linux/arm64 node:22 AS build\n'
                                  'FROM --platform=linux/amd64 node:22\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.final_image_platform, 'linux/amd64')
            validate_infrastructure(profile, 'aws-ecs-express')
            dockerfile.write_text('FROM --platform=linux/amd64 node:22 AS build\n'
                                  'FROM --platform=linux/arm64 node:22\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.final_image_platform, 'linux/arm64')
            validate_infrastructure(profile, 'local-docker')
            with self.assertRaisesRegex(ValueError, 'linux/arm64'):
                validate_infrastructure(profile, 'aws-ecs-express')
            with self.assertRaisesRegex(ValueError, 'linux/arm64'):
                validate_infrastructure(profile, 'cloud-run')
            dockerfile.write_text('FROM --platform=$TARGETPLATFORM node:22\n')
            validate_infrastructure(inspect_infrastructure(project), 'aws-ecs-express')

    def test_aws_upload_rejects_pinned_arm_image_before_creating_job(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('Dockerfile', 'FROM --platform=linux/arm64 node:22\nCMD ["node", "server.js"]\n')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                      aws_settings=AwsSettings('ap-northeast-2'), monitor_interval=0)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'aws-ecs-express', 'X-Public-Access': 'true'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.AwsSettings.unavailable_reason', return_value=None):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertIn('linux/arm64', handler.json_response.call_args.args[1]['error'])
            self.assertFalse(app.jobs)

    def test_local_upload_stays_loopback_even_with_public_permission(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'local-docker', 'X-Public-Access': 'true'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.threading.Thread'):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 202)
            job = app.jobs[handler.json_response.call_args.args[1]['id']]
            self.assertFalse(job['public'])
            self.assertEqual(job['infrastructure_plan']['compatibility']['access_mode'], 'loopback')
            self.assertEqual(job['application_ir']['topology_status'], 'unresolved')
            access = next(item for item in job['infrastructure_plan']['compatibility']['constraint_results']
                          if item['rule_id'] == 'ACCESS-01')
            self.assertEqual(access['status'], 'satisfied')

    def test_agent_edit_cannot_change_final_image_to_arm_for_aws(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            project.mkdir()
            (project / 'Dockerfile').write_text('FROM node:22\nCMD ["node", "server.js"]\n')
            (project / 'server.js').write_text('console.log("ready")')
            tools = DeploymentTools(project, root / 'work', 'a' * 16, {}, lambda *_: None,
                                    lambda **_: None, target='aws-ecs-express', adapter_factory=LocalDockerAdapter)
            before = tools.read_project_files(['Dockerfile'])['files']['Dockerfile']
            tools.apply_project_patch('Dockerfile', before,
                                      before.replace('FROM node:22',
                                                     'FROM --platform=linux/arm64 node:22'))
            tools.configure_deployment('dockerfile', None, 3000, '/', [])
            with self.assertRaisesRegex(ValueError, 'linux/arm64'):
                tools.deploy_application()
            self.assertEqual(tools.attempts, 0)

    def test_explicit_postgres_plan_requires_detected_engine_and_existing_binding(self):
        profile = InfrastructureProfile('database', ('package.json',), 1,
                                        ('database',), ('postgresql',))
        plan = explicit_infrastructure_plan('aws-ecs-express', profile,
                                            existing_postgres_id='sky-demo-app')
        self.assertEqual(plan['workload'], 'postgresql-http')
        self.assertEqual(plan['database']['binding'], 'existing')
        self.assertIn('existing RDS PostgreSQL', plan['resources'])
        with self.assertRaisesRegex(ValueError, '다릅니다'):
            explicit_infrastructure_plan('cloud-run', profile,
                                         existing_postgres_id='sky-demo-app')
        with self.assertRaisesRegex(ValueError, '다릅니다'):
            explicit_infrastructure_plan('aws-ecs-express',
                InfrastructureProfile('database', ('package.json',), 1,
                                      ('database',), ('mysql',)),
                existing_postgres_id='sky-demo-app')

    def test_planner_uses_bounded_structured_api_request(self):
        proposal = {'target': 'local-docker', 'workload': 'stateless-http',
                    'rationale': '로컬 검증', 'evidence': [{'file': 'package.json', 'quote': 'start'}]}
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(proposal)}]}]}
        with patch('application.infrastructure.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            result = OpenAIInfrastructurePlanner(AISettings('fixture-key', 'fixture-model')).propose(
                {'package.json': 'start'}, ['local-docker'], False)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result, proposal)
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        self.assertEqual(json.loads(payload['input'])['available_targets'], ['local-docker'])

    def test_planner_keeps_response_size_limit_error(self):
        with patch('application.infrastructure.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'x' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, '응답 크기 제한'):
                OpenAIInfrastructurePlanner(AISettings('fixture-key', 'fixture-model')).propose(
                    {'package.json': 'start'}, ['local-docker'], False)

    def test_planner_retries_temporary_rejection_once(self):
        proposal = {'target': 'local-docker', 'workload': 'stateless-http',
                    'rationale': '로컬 검증', 'evidence': [{'file': 'package.json', 'quote': 'start'}]}
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(proposal)}]}]}
        failure = urllib.error.HTTPError('https://api.openai.com/v1/responses', 500,
            'server error', {}, io.BytesIO(b'{}'))
        with patch('application.infrastructure.urllib.request.build_opener') as opener, \
                patch('adapters.ai.openai_http.time.sleep'), \
                patch('adapters.ai.openai_http.random.uniform', return_value=0):
            opener.return_value.open.side_effect = [failure, io.BytesIO(json.dumps(response).encode())]
            result = OpenAIInfrastructurePlanner(AISettings('fixture-key', 'fixture-model')).propose(
                {'package.json': 'start'}, ['local-docker'], False)
        self.assertEqual(result, proposal)
        self.assertEqual(opener.return_value.open.call_count, 2)

    def test_ai_plan_requires_real_source_evidence_and_available_target(self):
        files = {'package.json': '{"scripts":{"start":"node server.js"}}'}
        proposal = {'target': 'aws-ecs-express', 'workload': 'stateless-http',
                    'rationale': 'HTTP 컨테이너를 공개 배포합니다.',
                    'evidence': [{'file': 'package.json', 'quote': '"start":"node server.js"'}]}
        plan = validate_infrastructure_proposal(proposal, files, ['aws-ecs-express'])
        self.assertEqual(plan['resources'][-1], 'ECS Express service')
        with self.assertRaisesRegex(ValueError, '사용할 수 없는'):
            validate_infrastructure_proposal(proposal, files, ['local-docker'])
        with self.assertRaisesRegex(ValueError, '근거'):
            validate_infrastructure_proposal({**proposal, 'evidence': [{'file': 'package.json', 'quote': 'invented'}]},
                                             files, ['aws-ecs-express'])
        with self.assertRaisesRegex(ValueError, '지원하지 않는'):
            validate_infrastructure_proposal({**proposal, 'workload': 'requires-unsupported-resources'},
                                             files, ['aws-ecs-express'])

    def test_auto_plan_retries_invalid_quote_once_and_still_requires_source_evidence(self):
        class Planner:
            def __init__(self, second_quote):
                self.second_quote = second_quote
                self.calls = []

            def propose(self, files, available_targets, public_access, *, retry_invalid_evidence=False):
                self.calls.append(retry_invalid_evidence)
                return {'target': 'local-docker', 'workload': 'stateless-http',
                        'rationale': 'HTTP 서버', 'evidence': [{'file': 'server.js',
                        'quote': self.second_quote if retry_invalid_evidence else 'invented quote'}]}

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'server.js').write_text('http.createServer(handler).listen(3000);')
            planner = Planner('http.createServer(handler)')
            result = plan_infrastructure(project, ['local-docker'], False, planner)
            self.assertEqual(result['evidence'][0]['quote'], 'http.createServer(handler)')
            self.assertEqual(planner.calls, [False, True])

            planner = Planner('still invented')
            with self.assertRaisesRegex(ValueError, '근거를 소스에서 확인'):
                plan_infrastructure(project, ['local-docker'], False, planner)
            self.assertEqual(planner.calls, [False, True])

    def test_detects_embedded_database_without_scanning_docs_or_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('const db = require("better-sqlite3")("app.db");')
            (project / 'docs').mkdir()
            (project / 'docs' / 'example.py').write_text('import sqlite3')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.storage, 'sqlite')
            self.assertEqual(profile.evidence, ('server.js',))
            with self.assertRaisesRegex(ValueError, '데이터 손실'):
                validate_infrastructure(profile, 'aws-ecs-express')

    def test_detects_prisma_sqlite_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'schema.prisma').write_text('datasource db { provider = "sqlite" url = env("DATABASE_URL") }')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.storage, 'sqlite')
            self.assertEqual(profile.evidence, ('schema.prisma',))

    def test_detects_external_database_from_runtime_dependencies_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'pg': '^8.0.0'}, 'devDependencies': {'typescript': '^5.0.0'}}))
            (project / 'schema.prisma').write_text(
                'datasource db { provider = "postgresql" url = env("DATABASE_URL") }')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.storage, 'database')
            self.assertEqual(profile.requirements, ('database',))
            self.assertEqual(profile.database_engines, ('postgresql',))
            self.assertEqual(profile.evidence, ('package.json', 'schema.prisma'))
            with self.assertRaisesRegex(ValueError, '마이그레이션'):
                validate_infrastructure(profile, 'aws-ecs-express')

    def test_detects_database_from_python_manifest_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'requirements.txt').write_text('psycopg[binary]>=3.1\n')
            (project / 'app.py').write_text('import pymongo\n')
            (project / 'docs').mkdir()
            (project / 'docs' / 'example.py').write_text('import sqlite3\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('database',))
            self.assertEqual(profile.evidence, ('app.py', 'requirements.txt'))
            self.assertEqual(profile.database_engines, ('mongodb', 'postgresql'))

    def test_detects_database_from_pyproject_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'pyproject.toml').write_text(
                '[project]\ndependencies = ["psycopg[binary]>=3.1"]\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('database',))
            self.assertEqual(profile.database_engines, ('postgresql',))
            self.assertEqual(profile.evidence, ('pyproject.toml',))

    def test_mixed_or_unknown_database_engine_is_not_mistaken_for_postgres(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'pg': '8.23.0', 'mysql2': '3.0.0'}}))
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.database_engines, ('mysql', 'postgresql'))
            with self.assertRaisesRegex(ValueError, 'mysql'):
                validate_infrastructure(profile, 'aws-ecs-express', postgres=True)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'@prisma/client': '6.0.0'}}))
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.database_engines, ('unknown',))
            with self.assertRaisesRegex(ValueError, 'unknown'):
                validate_infrastructure(profile, 'aws-ecs-express', postgres=True)

    def test_database_dependency_only_in_dev_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'dependencies': {'express': '^5.0.0'}, 'devDependencies': {'pg': '^8.0.0'}}))
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ())

    def test_detects_uploaded_sqlite_database_without_source_import(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'data').mkdir()
            (project / 'data' / 'users.sqlite3').write_bytes(b'SQLite format 3\x00')
            profile = inspect_infrastructure(project)
            self.assertIn('sqlite', profile.requirements)
            self.assertEqual(profile.evidence, ('data/users.sqlite3',))

    def test_scans_dependency_beyond_old_per_file_read_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'server.js').write_text(' ' * 25000 + '\nrequire("better-sqlite3")("app.db")')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('sqlite',))
            with self.assertRaisesRegex(ValueError, 'SQLite'):
                validate_infrastructure(profile, 'aws-ecs-express')

    def test_incomplete_source_inspection_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for number in range(1001):
                (project / f'module{number:04d}.js').write_text('const ready = true;')
            (project / 'z_database.js').write_text('require("better-sqlite3")("app.db")')
            with self.assertRaisesRegex(ValueError, '끝까지 검사'):
                inspect_infrastructure(project)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'server.js').write_text(' ' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, '끝까지 검사'):
                inspect_infrastructure(project)

    def test_detects_required_worker_and_local_file_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'package.json').write_text(json.dumps({
                'scripts': {'start': 'node server.js', 'worker': 'node worker.js'},
                'dependencies': {'bullmq': '^5.0.0'}}))
            (project / 'server.js').write_text('fs.writeFileSync("uploads/photo.jpg", image);')
            profile = inspect_infrastructure(project)
            self.assertEqual(set(profile.requirements), {'background-worker', 'local-files'})
            with self.assertRaisesRegex(ValueError, '워커') as raised:
                validate_infrastructure(profile, 'aws-ecs-express')
            self.assertIn('영속 저장소', str(raised.exception))

    def test_detects_procfile_worker_without_runtime_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'Procfile').write_text('web: node server.js\nworker: node worker.js\n')
            profile = inspect_infrastructure(project)
            self.assertEqual(profile.requirements, ('background-worker',))

    def test_upload_blocks_sqlite_before_any_deployment_resource(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', json.dumps({
                'scripts': {'start': 'node server.js'},
                'dependencies': {'better-sqlite3': '^11.0.0'}}))
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertIn('SQLite', handler.json_response.call_args.args[1]['error'])
            self.assertIn('SQLite 파일을 PostgreSQL로 이전하기',
                          handler.json_response.call_args.args[1]['error'])
            self.assertFalse(app.jobs)

    def test_ai_planner_failure_removes_uncommitted_upload(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token, 'X-Deploy-Target': 'auto',
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.uuid.uuid4') as uuid4, \
                    patch('interfaces.http.server.plan_infrastructure', side_effect=ValueError('AI 계획 실패')):
                uuid4.return_value.hex = 'a' * 32
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertFalse(app.jobs)
            self.assertFalse((root / ('a' * 16)).exists())

    def test_initial_job_save_failure_removes_upload_and_memory_entry(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.uuid.uuid4') as uuid4, \
                    patch.object(app, 'save', side_effect=RuntimeError('기록 저장 실패')), \
                    patch('interfaces.http.server.threading.Thread.start') as worker:
                uuid4.return_value.hex = 'b' * 32
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertFalse(app.jobs)
            self.assertFalse((root / ('b' * 16)).exists())
            worker.assert_not_called()

    def test_worker_start_failure_persists_interrupted_upload(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.threading.Thread.start', side_effect=RuntimeError('no thread')):
                handler.do_POST()
            status, payload = handler.json_response.call_args.args
            self.assertEqual((status, payload['status']), (202, 'interrupted'))
            stored = json.loads((root / payload['id'] / 'job.json').read_text())
            self.assertEqual(stored['status'], 'interrupted')
            self.assertEqual(stored['attempts'], 0)
            self.assertTrue(any(event['stage'] == 'interrupted' for event in stored['events']))
            self.assertEqual(len(stored['source_digest']), 64)
            restored = App(root, AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            source = Path(stored['project']) / 'server.js'
            original = source.read_text()
            source.write_text('changed after upload')
            with patch('interfaces.http.server.threading.Thread.start') as worker:
                with self.assertRaisesRegex(ValueError, '소스가 변경'):
                    restored.resume_unstarted_deployment(payload['id'])
                worker.assert_not_called()
                source.write_text(original)
                self.assertEqual(restored.resume_unstarted_deployment(payload['id']),
                                 {'id': payload['id'], 'status': 'running'})
                worker.assert_called_once()
            self.assertEqual(json.loads((root / payload['id'] / 'job.json').read_text())['status'], 'running')
            with self.assertRaisesRegex(ValueError, '안전하게 재개'):
                restored.resume_unstarted_deployment(payload['id'])
            restored.jobs[payload['id']].update(status='interrupted', steps=1)
            restored.save(payload['id'])
            with patch('interfaces.http.server.threading.Thread.start') as worker:
                with self.assertRaisesRegex(ValueError, '안전하게 재개'):
                    restored.resume_unstarted_deployment(payload['id'])
                worker.assert_not_called()

    def test_unstarted_resume_route_requires_session_and_empty_body(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            job_id = 'a' * 16
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = f'/api/deployments/{job_id}/resume-unstarted'
            handler.headers = {'Content-Length': '0'}
            handler.rfile = io.BytesIO()
            handler.json_response = Mock()
            with patch.object(app, 'resume_unstarted_deployment',
                              return_value={'id': job_id, 'status': 'running'}) as resume:
                handler.do_POST()
                self.assertEqual(handler.json_response.call_args.args[0], 403)
                resume.assert_not_called()
                handler.headers['X-Sky-Token'] = app.token
                handler.headers['Content-Length'] = '1'
                handler.json_response.reset_mock()
                handler.do_POST()
                self.assertEqual(handler.json_response.call_args.args[0], 400)
                resume.assert_not_called()
                handler.headers['Content-Length'] = '0'
                handler.json_response.reset_mock()
                handler.do_POST()
                handler.json_response.assert_called_once_with(
                    202, {'id': job_id, 'status': 'running'})
                resume.assert_called_once_with(job_id)

    def test_analyze_failure_removes_uncommitted_upload(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts":{"start":"node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root, AISettings(), monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/analyze'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.uuid.uuid4') as uuid4, \
                    patch('interfaces.http.server.analyze_project', side_effect=ValueError('분석 실패')):
                uuid4.return_value.hex = 'e' * 32
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertFalse(app.jobs)
            self.assertFalse((root / ('e' * 16)).exists())

    def test_upload_blocks_postgres_before_any_deployment_resource(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', json.dumps({
                'scripts': {'start': 'node server.js'}, 'dependencies': {'pg': '^8.0.0'}}))
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'), monitor_interval=0)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue()))}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 400)
            self.assertIn('데이터베이스', handler.json_response.call_args.args[1]['error'])
            self.assertFalse(app.jobs)

    def test_agent_cannot_add_sqlite_after_upload_and_provision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            project.mkdir()
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('console.log("ready")')
            tools = DeploymentTools(project, root / 'work', 'a' * 16, {}, lambda *_: None, lambda **_: None, adapter_factory=LocalDockerAdapter)
            tools.configure_deployment('start', None, 3000, '/', [])
            (tools.work / 'server.js').write_text('const sqlite = require("node:sqlite");')
            with self.assertRaisesRegex(ValueError, 'SQLite'):
                tools.deploy_application()
            self.assertEqual(tools.attempts, 0)

    def test_agent_cannot_add_database_after_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            project.mkdir()
            (project / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (project / 'server.js').write_text('console.log("ready")')
            tools = DeploymentTools(project, root / 'work', 'a' * 16, {}, lambda *_: None, lambda **_: None, adapter_factory=LocalDockerAdapter)
            tools.configure_deployment('start', None, 3000, '/', [])
            (tools.work / 'server.js').write_text('const db = require("pg");')
            with self.assertRaisesRegex(ValueError, '데이터베이스'):
                tools.deploy_application()
            self.assertEqual(tools.attempts, 0)

    def test_auto_target_resolves_before_job_and_keeps_infrastructure_plan(self):
        class Planner:
            def __init__(self, _settings):
                pass
            def propose(self, files, available_targets, public_access):
                return {'target': 'aws-ecs-express', 'workload': 'stateless-http',
                        'rationale': '공개 HTTP 서비스',
                        'evidence': [{'file': 'package.json', 'quote': '"start": "node server.js"'}]}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package.json', '{"scripts": {"start": "node server.js"}}')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                      aws_settings=AwsSettings('ap-northeast-2'), monitor_interval=0,
                      infrastructure_planner_factory=Planner)
            handler_class = handler_for(app)
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'auto', 'X-Public-Access': 'true'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.AwsSettings.unavailable_reason', return_value=None), \
                    patch('interfaces.http.server.threading.Thread'):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 202)
            job = app.jobs[handler.json_response.call_args.args[1]['id']]
            self.assertEqual(job['target'], 'aws-ecs-express')
            self.assertEqual(job['requested_target'], 'auto')
            self.assertEqual(job['infrastructure_plan']['planner'], 'openai')
            self.assertEqual(job['infrastructure_profile']['storage'], 'unconfirmed')
            compatibility = job['infrastructure_plan']['compatibility']
            self.assertTrue(compatibility['compatible'])
            self.assertEqual(compatibility['target'], 'aws-ecs-express')
            self.assertFalse(compatibility['postgres_binding'])
            self.assertEqual(compatibility['access_mode'], 'public')
            private_handler = handler_class.__new__(handler_class)
            private_handler.path = '/api/deployments'
            private_handler.headers = {**handler.headers, 'X-Public-Access': 'false'}
            private_handler.rfile = io.BytesIO(archive.getvalue())
            private_handler.json_response = Mock()
            with patch('interfaces.http.server.AwsSettings.unavailable_reason', return_value=None):
                private_handler.do_POST()
            self.assertEqual(private_handler.json_response.call_args.args[0], 400)
            self.assertIn('사용할 수 없는', private_handler.json_response.call_args.args[1]['error'])
            self.assertEqual(len(app.jobs), 1)

    def test_auto_target_filters_platform_conflicts_before_ai_proposal(self):
        offered_targets = []

        class Planner:
            def __init__(self, _settings):
                pass

            def propose(self, files, available_targets, public_access):
                offered_targets.extend(available_targets)
                return {'target': 'local-docker', 'workload': 'stateless-http',
                        'rationale': '최종 이미지는 ARM 플랫폼입니다.',
                        'evidence': [{'file': 'Dockerfile', 'quote': 'linux/arm64'}]}

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('Dockerfile', 'FROM --platform=linux/arm64 node:22\n')
            bundle.writestr('server.js', 'console.log("ready")')
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory), AISettings('fixture-key', 'fixture-model'),
                      infrastructure_planner_factory=Planner, monitor_interval=0)
            handler = handler_for(app).__new__(handler_for(app))
            handler.path = '/api/deployments'
            handler.headers = {'X-Sky-Token': app.token,
                               'Content-Length': str(len(archive.getvalue())),
                               'X-Deploy-Target': 'auto', 'X-Public-Access': 'true'}
            handler.rfile = io.BytesIO(archive.getvalue())
            handler.json_response = Mock()
            with patch('interfaces.http.server.AwsSettings.unavailable_reason', return_value=None), \
                    patch('interfaces.http.server.CloudRunSettings.unavailable_reason', return_value=None), \
                    patch('interfaces.http.server.threading.Thread'):
                handler.do_POST()
            self.assertEqual(handler.json_response.call_args.args[0], 202)
            self.assertEqual(offered_targets, ['local-docker'])
            job = app.jobs[handler.json_response.call_args.args[1]['id']]
            candidates = {item['id']: item for item in job['infrastructure_plan']['candidates']}
            self.assertEqual(job['target'], 'local-docker')
            self.assertEqual(candidates['aws-ecs-express']['status'], 'rejected')
            self.assertEqual(candidates['cloud-run']['status'], 'rejected')
            self.assertEqual(candidates['local-docker']['status'], 'eligible')
            self.assertTrue(candidates['local-docker']['selected'])


if __name__ == '__main__':
    unittest.main()
