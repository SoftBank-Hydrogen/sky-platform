"""Bounded SQL migration bundle for the opt-in PostgreSQL data path."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from sky_platform.assets import ASSET_ROOT


MIGRATION_NAME = re.compile(r'([0-9]{4})_[a-z0-9_]+\.sql')
TRANSACTION_CONTROL = re.compile(r'\b(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b', re.I)
MAX_FILES = 32
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
RDS_CA_SHA256 = 'fe45bbebf92ad3e27a583bbb2ddd1553c521ed4d49af5514dc0a40372ea5395c'


def trusted_rds_ca_bundle() -> Path:
    bundle = ASSET_ROOT / 'infra' / 'rds-global-bundle.pem'
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != RDS_CA_SHA256:
        raise ValueError('RDS CA 번들 체크섬이 예상과 다릅니다.')
    return bundle


@dataclass(frozen=True)
class SqlMigration:
    name: str
    sha256: str


@dataclass(frozen=True)
class MigrationBundle:
    directory: Path
    migrations: tuple[SqlMigration, ...]
    digest: str

    def manifest(self) -> str:
        return json.dumps({'migrations': [{'name': item.name, 'sha256': item.sha256}
                                          for item in self.migrations]}, separators=(',', ':'))


def collect_sql_migrations(project: Path) -> MigrationBundle:
    """Accept ordered, immutable SQL files; execution owns each transaction."""
    directory = project / 'migrations'
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('PostgreSQL 앱에는 migrations/ 디렉터리가 필요합니다.')
    paths = sorted(directory.iterdir())
    if not 1 <= len(paths) <= MAX_FILES:
        raise ValueError('SQL 마이그레이션 파일을 1–32개 지정하세요.')
    migrations = []
    total = 0
    versions = set()
    digest = hashlib.sha256()
    for path in paths:
        match = MIGRATION_NAME.fullmatch(path.name)
        if (not match or path.is_symlink() or not path.is_file()
                or match.group(1) in versions):
            raise ValueError('마이그레이션은 중복 없는 0001_name.sql 형식의 일반 파일이어야 합니다.')
        versions.add(match.group(1))
        size = path.stat().st_size
        total += size
        if not 1 <= size <= MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError('SQL 마이그레이션 파일 크기 제한을 초과했습니다.')
        data = path.read_bytes()
        if len(data) != size:
            raise ValueError('SQL 마이그레이션 파일이 검사 도중 변경됐습니다.')
        try:
            sql = data.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('SQL 마이그레이션은 UTF-8이어야 합니다.') from None
        if '\x00' in sql or not sql.strip() or TRANSACTION_CONTROL.search(sql):
            raise ValueError('빈 SQL·NUL·트랜잭션 제어문은 마이그레이션에 사용할 수 없습니다.')
        checksum = hashlib.sha256(data).hexdigest()
        digest.update(path.name.encode() + b'\0' + bytes.fromhex(checksum))
        migrations.append(SqlMigration(path.name, checksum))
    return MigrationBundle(directory, tuple(migrations), digest.hexdigest())


def stage_migrator_context(bundle: MigrationBundle, destination: Path) -> Path:
    """Copy a checked bundle into a dedicated Docker build context without app source."""
    fresh = collect_sql_migrations(bundle.directory.parent)
    if fresh.digest != bundle.digest:
        raise ValueError('SQL 마이그레이션 파일이 계획 뒤 변경됐습니다.')
    if destination.exists():
        raise ValueError('마이그레이션 빌드 디렉터리는 새 경로여야 합니다.')
    destination.mkdir(mode=0o700, parents=True)
    source = ASSET_ROOT / 'infra'
    shutil.copyfile(source / 'postgres-migrator.Dockerfile', destination / 'Dockerfile')
    shutil.copyfile(source / 'postgres-migrator-package.json', destination / 'package.json')
    shutil.copyfile(source / 'postgres-migrator-package-lock.json', destination / 'package-lock.json')
    shutil.copyfile(source / 'postgres-migrator.js', destination / 'postgres-migrator.js')
    shutil.copyfile(trusted_rds_ca_bundle(), destination / 'rds-global-bundle.pem')
    migration_dir = destination / 'migrations'
    migration_dir.mkdir(mode=0o700)
    for item in fresh.migrations:
        shutil.copyfile(fresh.directory / item.name, migration_dir / item.name)
    if collect_sql_migrations(destination).digest != bundle.digest:
        raise ValueError('SQL 마이그레이션 파일이 복사 도중 변경됐습니다.')
    (migration_dir / 'manifest.json').write_text(fresh.manifest(), encoding='utf-8')
    return destination
