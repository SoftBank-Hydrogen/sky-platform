"""Stage a read-only restore verifier image from an immutable migration bundle."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from sky_platform.assets import ASSET_ROOT
from sky_platform.database.migrations import MigrationBundle, collect_sql_migrations, trusted_rds_ca_bundle


def stage_restore_verifier_context(bundle: MigrationBundle, destination: Path) -> Path:
    fresh = collect_sql_migrations(bundle.directory.parent)
    if fresh.digest != bundle.digest:
        raise ValueError('복원 검사 전 SQL 마이그레이션 번들이 변경됐습니다.')
    if destination.exists():
        raise ValueError('복원 검사 빌드 디렉터리는 새 경로여야 합니다.')
    destination.mkdir(mode=0o700, parents=True)
    source = ASSET_ROOT / 'infra'
    shutil.copyfile(source / 'postgres-restore-verifier.Dockerfile', destination / 'Dockerfile')
    shutil.copyfile(source / 'postgres-migrator-package.json', destination / 'package.json')
    shutil.copyfile(source / 'postgres-migrator-package-lock.json', destination / 'package-lock.json')
    shutil.copyfile(source / 'postgres-restore-verifier.js',
                    destination / 'postgres-restore-verifier.js')
    shutil.copyfile(trusted_rds_ca_bundle(), destination / 'rds-global-bundle.pem')
    migrations = destination / 'migrations'
    migrations.mkdir(mode=0o700)
    (migrations / 'manifest.json').write_text(fresh.manifest(), encoding='utf-8')
    if json.loads((migrations / 'manifest.json').read_text())['migrations'] != [
            {'name': item.name, 'sha256': item.sha256} for item in bundle.migrations]:
        raise ValueError('복원 검사 manifest가 계획과 다릅니다.')
    return destination
