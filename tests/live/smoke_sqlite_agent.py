"""Live AI SQLite code repair with real Docker and PostgreSQL, without AWS resources."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from adapters.aws.postgres import PostgresRequest
from adapters.build.image import ImageBuilder
from adapters.database.migrations import stage_migrator_context
from application.agent import DeploymentAgent, DeploymentTools, OpenAIDeployAgent
from application.analysis import AISettings
from application.infrastructure import inspect_infrastructure, preflight_sqlite_conversion
from tests.live.smoke_sqlite_migration import docker

SOURCE = '''import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/health", "/posts"):
            self.send_error(404)
            return
        if self.path == "/health":
            body = json.dumps({"ok": True}).encode()
        else:
            with sqlite3.connect("app.db") as database:
                rows = database.execute("SELECT id, title FROM posts ORDER BY id").fetchall()
            body = json.dumps({"posts": [{"id": row[0], "title": row[1]} for row in rows]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
'''


class LocalPostgresAdapter:
    def __init__(self, event, network: str, database_host: str):
        self.event = event
        self.network = network
        self.database_host = database_host
        self.created = []

    def command(self, args: list[str], timeout: int = 300) -> str:
        if args[0] != 'docker':
            raise ValueError('테스트 어댑터는 Docker만 실행합니다.')
        return docker(*args[1:], timeout=timeout)

    def deploy(self, project, plan, attempt_id, environment, postgres=None, migrations=None):
        if postgres is None or migrations is None:
            raise ValueError('PostgreSQL 연결과 이전 SQL이 모두 필요합니다.')
        with tempfile.TemporaryDirectory(prefix='sky-sqlite-agent-migrator-') as folder:
            context = stage_migrator_context(migrations, Path(folder) / 'migrator')
            migrator_image = 'sky-sqlite-agent-migrator:' + attempt_id
            self.created.append(('image', migrator_image))
            self.command(['docker', 'build', '-t', migrator_image, str(context)])
            self.command(['docker', 'run', '--rm', '--network', self.network,
                          '-e', 'PGHOST=' + self.database_host, '-e', 'PGPORT=5432',
                          '-e', 'PGDATABASE=postgres',
                          '-e', 'PGUSER=postgres', '-e', 'PGPASSWORD=sky-sqlite-smoke',
                          migrator_image], timeout=90)
        image = 'sky-sqlite-agent:' + attempt_id
        container = 'sky-sqlite-agent-' + attempt_id
        self.created.append(('image', image))
        ImageBuilder(self.command, self.event).build(project, plan, image)
        self.command(['docker', 'run', '-d', '--name', container, '--network', self.network,
                      '-e', 'PGHOST=' + self.database_host, '-e', 'PGPORT=5432',
                      '-e', 'PGDATABASE=postgres',
                      '-e', 'PGUSER=postgres', '-e', 'PGPASSWORD=sky-sqlite-smoke',
                      '-e', 'PORT=' + str(plan.port), '-p', '127.0.0.1::' + str(plan.port), image])
        self.created.append(('container', container))
        binding = self.command(['docker', 'port', container, str(plan.port) + '/tcp'])
        url = 'http://' + binding.splitlines()[0]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last_response = ''
        for _ in range(30):
            try:
                with opener.open(url + '/posts', timeout=2) as response:
                    payload = json.load(response)
                    posts = payload['posts']
                if posts == [{'id': 1, 'title': "O'Reilly"}, {'id': 2, 'title': '한글'}]:
                    return {'url': url, 'health_url': url + '/posts', 'target': 'local-postgres-test'}
                last_response = 'Unexpected /posts JSON: ' + repr(payload)[:300]
            except (OSError, ValueError, KeyError) as exc:
                last_response = type(exc).__name__ + ': ' + str(exc)[:200]
            time.sleep(1)
        logs = subprocess.run(['docker', 'logs', '--tail', '40', container], capture_output=True,
                              text=True, timeout=15, check=False)
        detail = (logs.stdout + logs.stderr)[-1200:]
        raise RuntimeError('변환된 앱이 PostgreSQL 데이터를 HTTP로 반환하지 못했습니다. '
                           + last_response + ' 컨테이너 로그: ' + detail)

    def cleanup_failure(self, _attempt_id):
        self.cleanup()

    def cleanup(self):
        for kind, name in reversed(self.created):
            args = ['docker', 'rm', '-f', name] if kind == 'container' else ['docker', 'image', 'rm', name]
            subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        self.created.clear()


def smoke(postgres_image: str) -> None:
    settings = AISettings.from_environment()
    if not settings.available:
        raise RuntimeError('실제 모델 검증에는 OPENAI_API_KEY가 필요합니다.')
    suffix = uuid.uuid4().hex[:12]
    network = 'sky-sqlite-agent-' + suffix
    database_name = 'sky-sqlite-agent-db-' + suffix
    adapters: list[LocalPostgresAdapter] = []
    with tempfile.TemporaryDirectory(prefix='sky-sqlite-agent-') as folder:
        root = Path(folder)
        source = root / 'source'
        source.mkdir()
        (source / 'server.py').write_text(SOURCE, encoding='utf-8')
        (source / 'requirements.txt').write_text('', encoding='utf-8')
        database = source / 'app.db'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL)')
            connection.executemany('INSERT INTO posts (title) VALUES (?)',
                                   [("O'Reilly",), ('한글',)])
        original = database.read_bytes()
        conversion, _ = preflight_sqlite_conversion(source, inspect_infrastructure(source))
        request = PostgresRequest('sqlite-smoke', '123456789012', 'ap-northeast-2',
                                  'vpc-12345678', ('subnet-12345678', 'subnet-abcdef12'), 'sg-12345678')
        docker('network', 'create', network)
        try:
            docker('run', '--rm', '-d', '--name', database_name, '--network', network,
                   '--network-alias', 'sky-postgres', '-e', 'POSTGRES_PASSWORD=sky-sqlite-smoke',
                   postgres_image,
                   timeout=90)
            for _ in range(30):
                ready = subprocess.run(['docker', 'exec', database_name, 'pg_isready', '-U', 'postgres'],
                                       capture_output=True, timeout=10, check=False)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('PostgreSQL 컨테이너가 준비되지 않았습니다.')
            events = []
            def factory(event):
                adapter = LocalPostgresAdapter(event, network, 'sky-postgres')
                adapters.append(adapter)
                return adapter
            def record(stage, _message):
                events.append(stage)
                if not events[:-1] or stage != events[-2]:
                    print('Stage:', stage, flush=True)
                if stage == 'attempt_failed':
                    print('Attempt error:', _message[-1500:], flush=True)
            tools = DeploymentTools(source, root / 'work', uuid.uuid4().hex[:16], {},
                                    record, lambda **_: None,
                                    adapter_factory=factory, target='aws-ecs-express',
                                    postgres_request=request, sqlite_conversion=conversion)
            configure = tools.configure_deployment
            def observed_configure(*args, **kwargs):
                try:
                    return configure(*args, **kwargs)
                except Exception as exc:
                    print('Configure failure:', type(exc).__name__, str(exc)[:300], flush=True)
                    raise
            tools.configure_deployment = observed_configure
            try:
                result = DeploymentAgent(OpenAIDeployAgent(settings), tools).run()
            except Exception:
                work_source = tools.work / 'server.py'
                if work_source.exists():
                    print('Working server.py:', work_source.read_text(encoding='utf-8')[:5000], flush=True)
                work_requirements = tools.work / 'requirements.txt'
                if work_requirements.exists():
                    print('Working requirements.txt:', work_requirements.read_text(encoding='utf-8')[:1000], flush=True)
                raise
            if result.get('target') != 'local-postgres-test':
                raise AssertionError('로컬 PostgreSQL 검증 결과가 아닙니다.')
            if database.read_bytes() != original or (tools.work / 'app.db').exists():
                raise AssertionError('원본 보존 또는 작업본 SQLite 제거가 실패했습니다.')
            if 'sqlite3' in (tools.work / 'server.py').read_text():
                raise AssertionError('앱의 SQLite 코드가 남아 있습니다.')
            print('PASS: live AI repaired source; production migrator loaded SQLite data; Docker HTTP returned rows')
            print('PASS: original SQLite file preserved; no AWS resources created')
            print('Deployment attempts:', tools.attempts)
        finally:
            for adapter in adapters:
                adapter.cleanup()
            subprocess.run(['docker', 'rm', '-f', database_name], capture_output=True,
                           text=True, timeout=30, check=False)
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True,
                           text=True, timeout=30, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres-image', required=True)
    arguments = parser.parse_args()
    smoke(arguments.postgres_image)
