"""A conservative, source-backed Application IR for the current single-bundle analyzer."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from engine.compatibility import InfrastructureProfile, evidence_id


@dataclass(frozen=True)
class SourceEvidence:
    id: str
    path: str
    signal: str
    origin: str = "deterministic-source-inspection"
    status: str = "confirmed"


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
    version: int
    components: tuple[Component, ...]
    requirements: tuple[Requirement, ...]
    evidence: tuple[SourceEvidence, ...]
    database_engines: tuple[str, ...]
    declared_image_platform: str | None
    topology_status: str = "unresolved"
    hypotheses: tuple[Hypothesis, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def application_ir(profile: InfrastructureProfile) -> ApplicationIR:
    """Preserve observed file→requirement edges without inventing app components."""
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
            evidence.append(SourceEvidence(identifier, path, name))
            ids.append(identifier)
        requirements.append(Requirement("R-" + name, name, tuple(ids)))
    hypotheses = []
    for name, paths in profile.source_signals:
        ids = tuple(evidence_id(name, path) for path in paths)
        evidence.extend(
            SourceEvidence(identifier, path, name, status="inferred")
            for identifier, path in zip(ids, paths, strict=True)
        )
        hypotheses.append(Hypothesis("H-" + name, name, ids))
    return ApplicationIR(
        version=1,
        components=(Component("source-bundle", "unresolved", tuple(item.id for item in requirements)),),
        requirements=tuple(requirements),
        evidence=tuple(evidence),
        database_engines=profile.database_engines,
        declared_image_platform=profile.final_image_platform,
        hypotheses=tuple(hypotheses),
    )
