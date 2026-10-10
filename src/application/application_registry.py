"""Durable application ownership for the local single-writer state directory."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from domain.access import ResourceOwner, owner_from_record

_APPLICATION_ID = re.compile(r"[a-z][a-z0-9-]{2,30}\Z")


class ApplicationRegistry:
    def __init__(self, root: Path):
        self.path = root / "applications.json"
        self.records = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid application registry")
            for application_id, record in data.items():
                if (
                    not isinstance(application_id, str)
                    or not _APPLICATION_ID.fullmatch(application_id)
                    or not isinstance(record, dict)
                    or set(record) != {"organization_id", "created_by"}
                    or owner_from_record(record) is None
                ):
                    raise ValueError("Invalid application owner")
            return data
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("Application ownership registry is invalid; refusing to start") from exc

    def register(self, application_id: str, owner: ResourceOwner, existing: list[dict]) -> bool:
        """Return whether a new claim was saved; caller holds the App lock."""
        if not isinstance(application_id, str) or not _APPLICATION_ID.fullmatch(application_id):
            raise ValueError("Invalid application ID")
        if not isinstance(owner, ResourceOwner):
            raise TypeError("Invalid application owner")
        existing_owners = [owner_from_record(record) for record in existing]
        if any(item is None or item.organization_id != owner.organization_id for item in existing_owners):
            raise ValueError("Application ID is unavailable")
        current = self.records.get(application_id)
        if current is not None:
            if current["organization_id"] != owner.organization_id:
                raise ValueError("Application ID is unavailable")
            return False
        updated = {**self.records, application_id: owner.record()}
        self._save(updated)
        self.records = updated
        return True

    def _save(self, records: dict[str, dict[str, str]]) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".applications-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(records, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
