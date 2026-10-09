"""Application IR keeps source identity and uncertainty separate from observed signals."""

import pytest

from engine.application_ir import application_ir
from engine.compatibility import InfrastructureProfile


def test_rejects_unbound_source_revision():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    with pytest.raises(ValueError, match="source revision"):
        application_ir(profile, "not-a-digest")


def test_unknown_database_engine_is_not_promoted_to_a_known_engine():
    profile = InfrastructureProfile(
        "database",
        ("server.js",),
        1,
        requirements=("database",),
        database_engines=("unknown",),
        requirement_evidence=(("database", ("server.js",)),),
    )
    ir = application_ir(profile, "a" * 64)
    assert ir.schema_version == 2
    assert ir.source_revision == "a" * 64
    assert ir.database_engines == ("unknown",)
    assert "database_engine" in ir.unknowns
    assert ir.evidence[0].status == "confirmed"
    assert ir.evidence[0].origin == "source_static"
    assert ir.evidence[0].source.revision == ir.source_revision
    assert ir.evidence[0].source.line is None
    assert ir.evidence[0].verified_by == ()


def test_manifest_observation_and_source_inference_have_distinct_records():
    profile = InfrastructureProfile(
        "sqlite",
        ("package.json", "server.js"),
        2,
        requirements=("sqlite",),
        requirement_evidence=(("sqlite", ("package.json",)),),
        source_signals=(("possible-process-local-state", ("server.js",)),),
    )
    ir = application_ir(profile, "b" * 64)
    manifest, inference = ir.evidence
    assert manifest.origin == "manifest"
    assert manifest.observation == "sqlite"
    assert manifest.interpretation is None
    assert manifest.status == "confirmed"
    assert inference.origin == "source_static"
    assert inference.observation == "source_pattern_match"
    assert inference.interpretation == "possible-process-local-state"
    assert inference.status == "inferred"
    assert inference.verified_by == ()
    serialized = ir.as_dict()
    assert serialized["evidence"][1]["source"] == {"revision": "b" * 64, "path": "server.js", "line": None}
    assert serialized["evidence"][1]["path"] == "server.js"
