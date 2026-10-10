"""Atomic local JSON persistence using the existing Sky state layout."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ports.state import StoredJob


class DirectoryDeploymentRecordStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _job_directory(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
            raise ValueError('Invalid job identity')
        return self.root / job_id

    def list_job_ids(self) -> tuple[str, ...]:
        return tuple(path.parent.name for path in sorted(self.root.glob('*/job.json')))

    def load_job(self, job_id: str) -> StoredJob:
        path = self._job_directory(job_id) / 'job.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        return StoredJob(record, modified_at)

    def save_job(self, job_id: str, record: dict) -> None:
        self._write(self._job_directory(job_id) / 'job.json', record, '.job-', indent=2)

    def load_health(self, job_id: str) -> object | None:
        path = self._job_directory(job_id) / 'health.json'
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if record is None:
            raise ValueError('Invalid null record')
        return record

    def save_health(self, job_id: str, history: list[dict]) -> None:
        self._write(self._job_directory(job_id) / 'health.json', history, '.health-')

    def load_github_sources(self) -> object | None:
        path = self.root / 'github-sources.json'
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError('unsafe source record')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record is None:
            raise ValueError('Invalid null record')
        return record

    def save_github_sources(self, records: list[dict]) -> None:
        self._write(self.root / 'github-sources.json', records, '.github-sources-', indent=2)

    @staticmethod
    def _write(path: Path, record: object, prefix: str, indent: int | None = None) -> None:
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(record, output, ensure_ascii=False, indent=indent)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
