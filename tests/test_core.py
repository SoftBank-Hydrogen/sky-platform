import json
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from sky_platform.runtime.core import ImageBuilder, LocalDockerAdapter, analyze, extract_project, make_plan, read_package, source_digest
from sky_platform.runtime.migrations import trusted_rds_ca_bundle


class CoreTests(unittest.TestCase):
    def test_nested_archive_and_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / "app.zip", "w") as z:
                z.writestr("app/package.json", '{"scripts":{"start":"node app.js"}}')
                z.writestr("app/.env", "SECRET=private")
                z.writestr("app/.env.production", "SECRET=private")
            project = extract_project(root / "app.zip", root / "out")
            self.assertEqual(project.name, "app")
            self.assertFalse((project / ".env").exists())
            self.assertFalse((project / ".env.production").exists())
            self.assertEqual(analyze(project).start_command, "npm start")

    def test_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / "app.zip", "w") as z:
                z.writestr("../escape", "bad")
            with self.assertRaises(ValueError):
                extract_project(root / "app.zip", root / "out")
            self.assertFalse((root / "escape").exists())

    def test_ambiguous_zip_paths_are_rejected_before_extraction(self):
        cases = [
            [('package.json', '{}'), ('./package.json', '{"scripts":{}}')],
            [('app', 'file'), ('app/package.json', '{"scripts":{}}')],
            [('app/package.json', '{"scripts":{}}'), ('app', 'file')],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with zipfile.ZipFile(root / 'app.zip', 'w') as bundle:
                    for name, data in entries:
                        bundle.writestr(name, data)
                with self.assertRaisesRegex(ValueError, 'duplicate or conflicting'):
                    extract_project(root / 'app.zip', root / 'out')
                self.assertFalse((root / 'out').exists())

    def test_start_required_and_lockfile_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{}')
            with self.assertRaises(ValueError):
                analyze(root)
            (root / "package.json").write_text(json.dumps({"scripts": {"start": "node x.js", "build": "tsc"}}))
            (root / "package-lock.json").write_text('{}')
            plan = analyze(root)
            self.assertIn("RUN npm ci", plan.dockerfile)
            self.assertIn("RUN npm run build", plan.dockerfile)

    def test_large_manifest_is_rejected_and_large_source_digest_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package.json').write_text('{"scripts":{},"padding":"' + 'x' * (1024 * 1024) + '"}')
            with self.assertRaisesRegex(ValueError, '1 MiB'):
                read_package(root)
            (root / 'package.json').unlink()
            content = b'z' * (2 * 1024 * 1024 + 17)
            (root / 'large.bin').write_bytes(content)
            expected = hashlib.sha256()
            name = b'large.bin'
            expected.update(len(name).to_bytes(8, 'big') + name)
            expected.update(len(content).to_bytes(8, 'big') + content)
            self.assertEqual(source_digest(root), expected.hexdigest())

    def test_large_dockerfile_is_rejected_before_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Dockerfile').write_text('FROM node:22\n' + '#' * 40000)
            with self.assertRaisesRegex(ValueError, '40,000'):
                make_plan(root, 'dockerfile', None)

    def test_existing_dockerfile_is_built_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{}}')
            dockerfile = 'FROM node:22-alpine\nCOPY . /app\nCMD ["node", "/app/server.js"]\n'
            (root / "Dockerfile").write_text(dockerfile)
            (root / ".dockerignore").write_text("coverage\n")
            plan = analyze(root)
            self.assertEqual(plan.dockerfile_source, "existing")
            self.assertEqual(plan.dockerfile, dockerfile)
            with self.assertRaisesRegex(ValueError, 'start_script=dockerfile'):
                make_plan(root, 'start', None)
            commands = []
            def inspect(args):
                context = Path(args[-1])
                commands.append((args, (context / 'Dockerfile').read_text(),
                                 (context / '.dockerignore').read_text()))
            ImageBuilder(inspect, lambda *_: None).build(root, plan, "test:latest")
            self.assertEqual((root / "Dockerfile").read_text(), dockerfile)
            self.assertEqual((root / '.dockerignore').read_text(), 'coverage\n')
            self.assertEqual(commands[0][1], dockerfile)
            self.assertIn('coverage\n', commands[0][2])
            self.assertIn('.env.*\n', commands[0][2])
            self.assertEqual(commands[0][0][-3:-1], ['-t', 'test:latest'])
            self.assertNotEqual(commands[0][0][-1], str(root))
            self.assertFalse(Path(commands[0][0][-1]).exists())
            self.assertEqual(source_digest(root), plan.source_digest)

    def test_cloud_image_platform_is_verified_after_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'Dockerfile').write_text('FROM node:22\n')
            plan = analyze(project)
            commands = []
            def command(args, **_kwargs):
                commands.append(args)
                return 'linux/amd64' if args[1:3] == ['image', 'inspect'] else ''
            ImageBuilder(command, lambda *_: None).build(
                project, plan, 'example:verified', platform='linux/amd64')
            self.assertEqual(commands[1], ['docker', 'image', 'inspect', '--platform',
                                            'linux/amd64', '--format',
                                            '{{.Os}}/{{.Architecture}}', 'example:verified'])
            self.assertFalse(any(args[1:3] == ['image', 'rm'] for args in commands))

    def test_mismatched_cloud_image_is_removed_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'Dockerfile').write_text('FROM --platform=$TARGETPLATFORM node:22\n')
            plan = analyze(project)
            commands = []
            def command(args, **_kwargs):
                commands.append(args)
                return 'linux/arm64' if args[1:3] == ['image', 'inspect'] else ''
            with self.assertRaisesRegex(ValueError, 'linux/amd64.*linux/arm64'):
                ImageBuilder(command, lambda *_: None).build(
                    project, plan, 'example:wrong-platform', platform='linux/amd64')
            self.assertEqual(commands[-1], ['docker', 'image', 'rm', 'example:wrong-platform'])

    def test_uninspectable_cloud_image_is_removed_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'Dockerfile').write_text('FROM node:22\n')
            plan = analyze(project)
            commands = []
            def command(args, **_kwargs):
                commands.append(args)
                if args[1:3] == ['image', 'inspect']:
                    raise RuntimeError('platform not found')
                return ''
            with self.assertRaisesRegex(ValueError, '플랫폼을 Docker에서 확인할 수 없습니다'):
                ImageBuilder(command, lambda *_: None).build(
                    project, plan, 'example:uninspectable', platform='linux/amd64')
            self.assertEqual(commands[-1], ['docker', 'image', 'rm', 'example:uninspectable'])

    def test_postgres_build_adds_verified_ca_to_existing_dockerfile_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Dockerfile').write_text('FROM node:22-alpine\nCMD ["node", "app.js"]\n')
            (root / '.dockerignore').write_text('*.pem\n')
            contexts = []
            def inspect(args):
                context = Path(args[-1])
                contexts.append(((context / 'Dockerfile').read_text(),
                                 (context / '.sky-rds-ca.pem').read_bytes(),
                                 (context / '.dockerignore').read_text()))
            builder = ImageBuilder(inspect, lambda *_: None)
            bundle = trusted_rds_ca_bundle()
            plan = analyze(root)
            builder.build(root, plan, 'db:test', extra_ca_bundle=bundle)
            builder.build(root, plan, 'db:second', extra_ca_bundle=bundle)
            self.assertEqual(contexts[0], contexts[1])
            self.assertIn('NODE_EXTRA_CA_CERTS=/app/.sky-rds-ca.pem', contexts[0][0])
            self.assertEqual(contexts[0][1], bundle.read_bytes())
            self.assertIn('!.sky-rds-ca.pem', contexts[0][2])
            self.assertEqual((root / 'Dockerfile').read_text(),
                             'FROM node:22-alpine\nCMD ["node", "app.js"]\n')
            self.assertEqual((root / '.dockerignore').read_text(), '*.pem\n')
            self.assertFalse((root / '.sky-rds-ca.pem').exists())
            self.assertEqual(source_digest(root), plan.source_digest)

    def test_generated_python_postgres_image_sets_libpq_ca_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('print("hello")\n')
            plan = analyze(root)
            bundle = trusted_rds_ca_bundle()
            contexts = []
            def inspect(args):
                context = Path(args[-1])
                contexts.append(((context / 'Dockerfile').read_text(),
                                 (context / '.sky-rds-ca.pem').read_bytes()))
            builder = ImageBuilder(inspect, lambda *_: None)
            builder.build(root, plan, 'db:python', extra_ca_bundle=bundle)
            builder.build(root, plan, 'db:second', extra_ca_bundle=bundle)
            self.assertEqual(contexts[0], contexts[1])
            self.assertIn('ENV PGSSLROOTCERT=/app/.sky-rds-ca.pem', contexts[0][0])
            self.assertEqual(contexts[0][1], bundle.read_bytes())
            self.assertFalse((root / 'Dockerfile').exists())
            self.assertFalse((root / '.sky-rds-ca.pem').exists())
            self.assertFalse((root / '.dockerignore').exists())
            self.assertEqual(analyze(root).runtime, 'python')
            self.assertEqual(source_digest(root), plan.source_digest)

    def test_failed_build_leaves_generated_python_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('print("hello")\n')
            plan = analyze(root)
            contexts = []
            def fail_build(args):
                contexts.append(Path(args[-1]))
                self.assertTrue((contexts[-1] / 'Dockerfile').is_file())
                raise RuntimeError('docker build failed')
            with self.assertRaisesRegex(RuntimeError, 'docker build failed'):
                ImageBuilder(fail_build, lambda *_: None).build(root, plan, 'failed:python')
            self.assertFalse(contexts[0].exists())
            self.assertFalse((root / 'Dockerfile').exists())
            self.assertFalse((root / '.dockerignore').exists())
            self.assertEqual(source_digest(root), plan.source_digest)

    def test_dockerfile_only_archive_is_valid_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / 'app.zip', 'w') as bundle:
                bundle.writestr('app/Dockerfile', 'FROM python:3.13-alpine\nCMD ["python", "server.py"]\n')
                bundle.writestr('app/server.py', 'print("hello")')
                bundle.writestr('app/.venv/secret.txt', 'excluded')
            project = extract_project(root / 'app.zip', root / 'out')
            self.assertEqual(project, root / 'out' / 'app')
            self.assertFalse((project / '.venv').exists())
            plan = analyze(project)
            self.assertEqual(plan.runtime, 'custom-dockerfile')
            self.assertEqual(plan.framework, 'container')
            self.assertEqual(plan.dockerfile_source, 'existing')

    def test_python_entrypoint_archive_gets_safe_generated_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / 'app.zip', 'w') as bundle:
                bundle.writestr('app/server.py', 'from http.server import HTTPServer\n')
                bundle.writestr('app/requirements.txt', 'flask==3.1.0\n')
            project = extract_project(root / 'app.zip', root / 'out')
            self.assertEqual(project, root / 'out' / 'app')
            plan = make_plan(project, 'server.py', None, 4321)
            self.assertEqual(plan.runtime, 'python')
            self.assertEqual(plan.start_command, 'python server.py')
            self.assertIn('RUN pip install --no-cache-dir -r requirements.txt', plan.dockerfile)
            self.assertIn('USER app', plan.dockerfile)
            self.assertIn('CMD ["python", "server.py"]', plan.dockerfile)
            self.assertEqual(analyze(project).runtime, 'python')
            with self.assertRaisesRegex(ValueError, 'Python app requires'):
                make_plan(project, '../server.py', None)
            with self.assertRaisesRegex(ValueError, 'build_script=null'):
                make_plan(project, 'server.py', 'build')

    def test_python_requirements_limit_and_missing_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('print("hello")')
            self.assertEqual(make_plan(root, 'app.py', None).runtime, 'python')
            with self.assertRaisesRegex(ValueError, 'Python app requires'):
                make_plan(root, 'server.py', None)
            (root / 'requirements.txt').write_text('x' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, '1 MiB'):
                make_plan(root, 'app.py', None)

    def test_asgi_entrypoint_requires_existing_module_and_uvicorn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / 'app.zip', 'w') as bundle:
                bundle.writestr('site/main.py', 'app = object()\n')
                bundle.writestr('site/requirements.txt', 'uvicorn==0.38.0\n')
            project = extract_project(root / 'app.zip', root / 'out')
            plan = make_plan(project, 'asgi:main.py', None, 4321)
            self.assertEqual(plan.runtime, 'python-asgi')
            self.assertIn('CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4321"]',
                          plan.dockerfile)
            self.assertEqual(analyze(project).runtime, 'python-asgi')
            for invalid in ('asgi:../main.py', 'asgi:other.py', 'asgi:main.py;echo bad'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    make_plan(project, invalid, None)
            (project / 'requirements.txt').write_text('fastapi==0.119.0\n')
            with self.assertRaisesRegex(ValueError, 'uvicorn'):
                make_plan(project, 'asgi:main.py', None)

    def test_wsgi_entrypoint_requires_existing_module_and_gunicorn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('from flask import Flask\napp = Flask(__name__)\n')
            (root / 'requirements.txt').write_text('flask==3.1.1\ngunicorn==23.0.0\n')
            plan = make_plan(root, 'wsgi:app.py', None, 4321)
            self.assertEqual(plan.runtime, 'python-wsgi')
            self.assertIn('CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:4321",',
                          plan.dockerfile)
            self.assertEqual(analyze(root).runtime, 'python-wsgi')
            for invalid in ('wsgi:../app.py', 'wsgi:other.py', 'wsgi:app.py;echo bad'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    make_plan(root, invalid, None)
            (root / 'requirements.txt').write_text('flask==3.1.1\n')
            with self.assertRaisesRegex(ValueError, 'gunicorn'):
                make_plan(root, 'wsgi:app.py', None)

    def test_static_python_analysis_selects_unique_app_object_across_root_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server.py').write_text('from main import app\n')
            (root / 'main.py').write_text('from fastapi import FastAPI\napp = FastAPI()\n')
            (root / 'requirements.txt').write_text('fastapi==0.119.0\nuvicorn==0.38.0\n')
            self.assertIn('main:app', analyze(root).start_command)
            (root / 'app.py').write_text('from fastapi import FastAPI\napp = FastAPI()\n')
            with self.assertRaisesRegex(ValueError, 'Multiple Python app objects'):
                analyze(root)

    def test_static_python_analysis_identifies_flask_when_uvicorn_is_also_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server.py').write_text('from app import app\n')
            (root / 'app.py').write_text('from flask import Flask\napp = Flask(__name__)\n')
            (root / 'requirements.txt').write_text('flask==3.1.1\ngunicorn==23.0.0\nuvicorn==0.38.0\n')
            plan = analyze(root)
            self.assertEqual(plan.runtime, 'python-wsgi')
            self.assertIn('app:app', plan.start_command)
            (root / 'app.py').write_text('app = create_app()\n')
            with self.assertRaisesRegex(ValueError, 'server type is ambiguous'):
                analyze(root)

    def test_static_python_analysis_does_not_select_the_wrong_server_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('from fastapi import FastAPI\napp = FastAPI()\n')
            (root / 'requirements.txt').write_text('fastapi==0.119.0\ngunicorn==23.0.0\n')
            with self.assertRaisesRegex(ValueError, 'requires uvicorn'):
                analyze(root)
            (root / 'app.py').write_text('from flask import Flask\napp = Flask(__name__)\n')
            (root / 'requirements.txt').write_text('flask==3.1.1\nuvicorn==0.38.0\n')
            with self.assertRaisesRegex(ValueError, 'requires gunicorn'):
                analyze(root)

    def test_static_python_analysis_ignores_text_and_local_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server.py').write_text('print("direct server")\n')
            (root / 'app.py').write_text('''"""\napp = FastAPI()\n"""\ndef helper():\n    app = FastAPI()\n''')
            (root / 'requirements.txt').write_text('uvicorn==0.38.0\n')
            self.assertEqual(analyze(root).start_command, 'python server.py')
            (root / 'app.py').write_text('app = 42\n')
            self.assertEqual(analyze(root).start_command, 'python server.py')
            (root / 'app.py').write_text('from fastapi import FastAPI\napp: FastAPI = FastAPI()\n')
            self.assertIn('app:app', analyze(root).start_command)

    def test_static_python_analysis_rejects_invalid_or_oversized_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'main.py').write_text('def broken(:\n')
            with self.assertRaisesRegex(ValueError, 'invalid Python syntax'):
                analyze(root)
            (root / 'main.py').write_text('#' + 'x' * (1024 * 1024) + '\n')
            with self.assertRaisesRegex(ValueError, '1 MiB static analysis limit'):
                analyze(root)

    def test_static_python_analysis_uses_declared_source_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_bytes(
                b'# coding: latin-1\n# caf\xe9\nfrom flask import Flask\napp = Flask(__name__)\n')
            (root / 'requirements.txt').write_text('flask==3.1.1\ngunicorn==23.0.0\n')
            self.assertEqual(analyze(root).runtime, 'python-wsgi')

    def test_failed_readiness_cleans_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"start":"node x.js"}}')
            adapter = LocalDockerAdapter(lambda *_: None)
            with patch.object(adapter, "command") as command, patch('sky_platform.runtime.core.time.sleep'), patch('sky_platform.runtime.core.urllib.request.build_opener') as opener:
                command.side_effect = ["", "container", "127.0.0.1:12345", "logs", ""]
                opener.return_value.open.side_effect = OSError("not ready")
                with self.assertRaisesRegex(RuntimeError, "HTTP 200"):
                    adapter.deploy(root, analyze(root), "test")
                self.assertEqual(command.call_args.args[0], ["docker", "rm", "-f", "sky-test"])


if __name__ == '__main__':
    unittest.main()
