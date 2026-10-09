"""Source-scoped observations; inference is not runtime verification."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from engine.compatibility import MANIFESTS


@dataclass(frozen=True)
class EvidenceSource:
    revision: str
    path: str
    line: int | None = None

    def __post_init__(self) -> None:
        if type(self.revision) is not str or not re.fullmatch(r"[a-f0-9]{64}", self.revision):
            raise ValueError("Evidence source revision must be a SHA-256 digest")
        if (
            type(self.path) is not str
            or not self.path
            or self.path.startswith("/")
            or ".." in self.path.split("/")
        ):
            raise ValueError("Evidence source path must be relative to the uploaded project")
        if self.line is not None and (type(self.line) is not int or self.line < 1):
            raise ValueError("Evidence source line must be positive when known")


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    origin: str
    source: EvidenceSource
    observation: str
    interpretation: str | None
    status: str
    verified_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.id) is not str or not re.fullmatch(r"E-[a-f0-9]{12}", self.id):
            raise ValueError("Invalid evidence ID")
        if type(self.origin) is not str or self.origin not in {
            "source_static",
            "manifest",
            "ai_inference",
            "runtime_probe",
            "user_assertion",
        }:
            raise ValueError("Invalid evidence origin")
        if not isinstance(self.source, EvidenceSource):
            raise TypeError("Invalid evidence source")
        if type(self.status) is not str or self.status not in {"confirmed", "inferred", "unknown"}:
            raise ValueError("Invalid evidence status")
        if type(self.observation) is not str or not self.observation:
            raise ValueError("Evidence observation is required")
        if self.interpretation is not None and (
            type(self.interpretation) is not str or not self.interpretation
        ):
            raise ValueError("Invalid evidence interpretation")
        if not isinstance(self.verified_by, tuple) or any(
            type(ref) is not str or not ref for ref in self.verified_by
        ):
            raise ValueError("Invalid evidence verification references")
        if self.origin == "ai_inference" and self.status == "confirmed" and not self.verified_by:
            raise ValueError("AI inference cannot be confirmed without an independent verifier")
        if self.status != "confirmed" and self.verified_by:
            raise ValueError("Unconfirmed evidence cannot claim verification")

    @property
    def path(self) -> str:
        return self.source.path

    def as_dict(self) -> dict:
        # The path alias preserves the existing IR/API link used by compatibility reports.
        return {**asdict(self), "path": self.source.path}


def source_evidence(
    identifier: str, path: str, signal: str, revision: str, *, inferred: bool = False
) -> EvidenceRecord:
    return EvidenceRecord(
        id=identifier,
        origin="manifest" if path.rsplit("/", 1)[-1] in MANIFESTS else "source_static",
        source=EvidenceSource(revision, path),
        observation="source_pattern_match" if inferred else signal,
        interpretation=signal if inferred else None,
        status="inferred" if inferred else "confirmed",
    )
