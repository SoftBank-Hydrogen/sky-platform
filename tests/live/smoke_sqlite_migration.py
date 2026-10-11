"""Execute Sky's SQLite snapshot SQL against an isolated real PostgreSQL container."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from adapters.database.migrations import collect_sql_migrations, stage_migrator_context
from adapters.database.sqlite_snapshot import compile_sqlite_snapshot


def docker(*args: str, input: str | None = None, timeout: int = 30) -> str:
    result = subprocess.run(
        ['docker', *args], input=input, text=True, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()[-2000:]
        raise RuntimeError(f'Docker {args[0]} 실패: {detail}')
    return result.stdout.strip()


def smoke(image: str) -> None:
    name = 'sky-sqlite-migration-' + uuid.uuid4().hex[:12]
    migrator_image = name + '-migrator'
    with tempfile.TemporaryDirectory(prefix='sky-sqlite-migration-') as directory:
        root = Path(directory)
        source = root / 'source.db'
        with sqlite3.connect(source) as connection:
            connection.execute('CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL)')
            connection.executemany('INSERT INTO posts (title) VALUES (?)',
                                   [("O'Reilly",), ('한글',), ('deleted',)])
            connection.execute('DELETE FROM posts WHERE id = 3')
            connection.execute('CREATE TABLE readings (value INTEGER, note TEXT)')
            connection.executemany('INSERT INTO readings VALUES (?, ?)',
                                   [(None, None), (7, 'same'), (7, 'same')])
            connection.execute('CREATE TABLE empty_table (value TEXT)')
        source_before = source.read_bytes()
        snapshot = compile_sqlite_snapshot(source)
        migrations = root / 'project' / 'migrations'
        migrations.mkdir(parents=True)
        (migrations / '0000_sky_sqlite_import.sql').write_text(snapshot.sql, encoding='utf-8')
        bundle = collect_sql_migrations(migrations.parent)
        stage_migrator_context(bundle, root / 'migrator')
        docker('run', '--rm', '-d', '--network', 'none', '--name', name,
               '-e', 'POSTGRES_PASSWORD=sky-sqlite-smoke', image, timeout=90)
        try:
            for _ in range(30):
                ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'],
                                       capture_output=True, timeout=10, check=False)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('PostgreSQL 컨테이너가 준비되지 않았습니다.')
            # Keep the verification VALUES intact while corrupting INSERTs.
            # Same row counts must not allow changed values or multiplicities.
            corruptions = [
                snapshot.sql.replace("VALUES (2, '한글');", "VALUES (2, 'changed');", 1),
                snapshot.sql.replace('VALUES (NULL, NULL);', "VALUES (7, 'same');", 1),
            ]
            for corrupted in corruptions:
                if corrupted == snapshot.sql:
                    raise AssertionError('Negative fixture did not change a source value')
                rejected = subprocess.run(
                    ['docker', 'exec', '-i', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                     '-U', 'postgres', '-d', 'postgres'],
                    input='BEGIN;\n' + corrupted + '\nCOMMIT;\n', text=True,
                    capture_output=True, timeout=30, check=False,
                )
                if rejected.returncode == 0 or 'division by zero' not in rejected.stderr:
                    raise AssertionError('Changed values were not rejected by the integrity guard')
                remaining = docker('exec', name, 'psql', '-X', '-A', '-t', '-U', 'postgres',
                                   '-c', "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
                if remaining != '0':
                    raise AssertionError('Rejected snapshot did not roll back all tables')
            docker('build', '--network=host', '-t', migrator_image, str(root / 'migrator'), timeout=300)
            migrated = docker('run', '--rm', '--network', 'container:' + name,
                              '-e', 'PGHOST=127.0.0.1', '-e', 'PGDATABASE=postgres',
                              '-e', 'PGUSER=postgres', '-e', 'PGPASSWORD=sky-sqlite-smoke',
                              migrator_image, timeout=90)
            if json.loads(migrated) != {'applied': 1}:
                raise AssertionError('Sky PostgreSQL 마이그레이터가 실행 결과를 보고하지 않았습니다.')
            replayed = docker('run', '--rm', '--network', 'container:' + name,
                              '-e', 'PGHOST=127.0.0.1', '-e', 'PGDATABASE=postgres',
                              '-e', 'PGUSER=postgres', '-e', 'PGPASSWORD=sky-sqlite-smoke',
                              migrator_image, timeout=90)
            if json.loads(replayed) != {'applied': 0}:
                raise AssertionError('Sky PostgreSQL 마이그레이터가 동일 SQL을 다시 실행했습니다.')
            rows = docker('exec', name, 'psql', '-X', '-A', '-t', '-U', 'postgres', '-d', 'postgres',
                          '-c', 'SELECT row_to_json(p) FROM (SELECT id, title FROM posts ORDER BY id) p')
            actual = [json.loads(line) for line in rows.splitlines()]
            expected = [{'id': 1, 'title': "O'Reilly"}, {'id': 2, 'title': '한글'}]
            if actual != expected:
                raise AssertionError(f'이전된 행이 SQLite 원본과 다릅니다: {actual!r}')
            next_id = docker('exec', name, 'psql', '-X', '-A', '-t', '-U', 'postgres', '-d', 'postgres',
                             '-c', "INSERT INTO posts (title) VALUES ('new') RETURNING id")
            if next_id.splitlines()[0] != '4':
                raise AssertionError(f'SQLite AUTOINCREMENT 고수위 값이 보존되지 않았습니다: {next_id!r}')
            if source.read_bytes() != source_before:
                raise AssertionError('SQLite 원본이 변경됐습니다.')
            print('PASS: production migrator applied SQLite snapshot once and skipped it on replay')
            print('PASS: SQLite rows, Unicode, apostrophe and identity sequence migrated to PostgreSQL')
            print('PASS: uploaded SQLite source remained unchanged')
            print('PASS: same-count value and duplicate changes rejected with complete transaction rollback')
            print('PASS: NULLs, duplicate rows and empty tables migrated through the production migrator')
        finally:
            docker('rm', '-f', name, timeout=30)
            subprocess.run(['docker', 'image', 'rm', migrator_image], capture_output=True,
                           text=True, timeout=30, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True, help='Local PostgreSQL Docker image tag or digest')
    args = parser.parse_args()
    smoke(args.image)
