"""Immutable source references contain identities, never caller-selected buckets/URLs."""

import re
from dataclasses import asdict, dataclass
from typing import Protocol

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class SourceArtifact:
    organization_id: str
    application_id: str
    upload_id: str
    kind: str
    sha256: str
    size: int
    source_digest: str

    def __post_init__(self):
        for value in (self.organization_id, self.application_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
                raise ValueError("Invalid source artifact identity")
        if not isinstance(self.upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.upload_id):
            raise ValueError("Invalid source upload identity")
        if not isinstance(self.kind, str) or self.kind not in {"original", "prepared"}:
            raise ValueError("Invalid source artifact kind")
        for digest in (self.sha256, self.source_digest):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid source artifact digest")
        if type(self.size) is not int or not 0 < self.size <= MAX_ARTIFACT_BYTES:
            raise ValueError("Invalid source artifact size")

    @property
    def key(self):
        return (
            f"sources/{self.organization_id}/{self.application_id}/{self.upload_id}/"
            f"{self.kind}/{self.sha256}.zip"
        )

    def record(self):
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_record(cls, record):
        fields = {
            "version",
            "organization_id",
            "application_id",
            "upload_id",
            "kind",
            "sha256",
            "size",
            "source_digest",
        }
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or type(record["version"]) is not int
            or record["version"] != 1
        ):
            raise ValueError("Invalid source artifact reference")
        return cls(**{key: value for key, value in record.items() if key != "version"})


class SourceArtifactStore(Protocol):
    def put(self, artifact: SourceArtifact, data: bytes) -> None: ...

    def get(self, artifact: SourceArtifact) -> bytes: ...
