"""A conservative, source-backed Application IR for the current single-bundle analyzer."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from engine.compatibility import InfrastructureProfile, evidence_id
from engine.evidence_record import EvidenceRecord, source_evidence


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Component:
    id: str
    kind: str
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class Hypothesis:
    id: str
    kind: str
    evidence_ids: tuple[str, ...]
    status: str = "inferred"


@dataclass(frozen=True)
class ApplicationIR:
    schema_version: int
    source_revision: str
    components: tuple[Component, ...]
    requirements: tuple[Requirement, ...]
    evidence: tuple[EvidenceRecord, ...]
    database_engines: tuple[str, ...]
    declared_image_platform: str | None
    topology_status: str = "unresolved"
    hypotheses: tuple[Hypothesis, ...] = ()
    unknowns: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {**asdict(self), "evidence": tuple(item.as_dict() for item in self.evidence)}


def application_ir(profile: InfrastructureProfile, source_revision: str) -> ApplicationIR:
    """Preserve observed file→requirement edges without inventing app components."""
    if not re.fullmatch(r"[a-f0-9]{64}", source_revision):
        raise ValueError("Application IR source revision must be a SHA-256 digest")
    evidence = []
    requirements = []
    by_requirement = dict(profile.requirement_evidence)
    names = sorted(
        set(profile.requirements) | ({"image-platform"} if profile.final_image_platform else set())
    )
    for name in names:
        ids = []
        for path in by_requirement.get(name, ()):
            identifier = evidence_id(name, path)
            evidence.append(source_evidence(identifier, path, name, source_revision))
            ids.append(identifier)
        requirements.append(Requirement("R-" + name, name, tuple(ids)))
    hypotheses = []
    for name, paths in profile.source_signals:
        ids = tuple(evidence_id(name, path) for path in paths)
        evidence.extend(
            source_evidence(identifier, path, name, source_revision, inferred=True)
            for identifier, path in zip(ids, paths, strict=True)
        )
        hypotheses.append(Hypothesis("H-" + name, name, ids))
    signal_names = {name for name, _paths in profile.source_signals}
    unknowns = {"component_topology", "statelessness"}
    if "websocket" in signal_names:
        unknowns.add("target_websocket_round_trip")
    if "possible-process-local-state" in signal_names:
        unknowns.add("session_affinity_behavior")
    if "unknown" in profile.database_engines:
        unknowns.add("database_engine")
    return ApplicationIR(
        schema_version=2,
        source_revision=source_revision,
        components=(Component("source-bundle", "unresolved", tuple(item.id for item in requirements)),),
        requirements=tuple(requirements),
        evidence=tuple(evidence),
        database_engines=profile.database_engines,
        declared_image_platform=profile.final_image_platform,
        hypotheses=tuple(hypotheses),
        unknowns=tuple(sorted(unknowns)),
    )
