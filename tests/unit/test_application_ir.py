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
