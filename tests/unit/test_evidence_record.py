"""Evidence claims must stay tied to a source and a stated observation scope."""

import pytest

from engine.evidence_record import EvidenceRecord, EvidenceSource


def test_evidence_source_rejects_invalid_revision_path_and_fabricated_line():
    with pytest.raises(ValueError, match="revision"):
        EvidenceSource("short", "server.js")
    with pytest.raises(ValueError, match="relative"):
        EvidenceSource("a" * 64, "../secret")
    with pytest.raises(ValueError, match="line"):
        EvidenceSource("a" * 64, "server.js", 0)


def test_inference_cannot_claim_a_runtime_verifier():
    with pytest.raises(ValueError, match="cannot claim verification"):
        EvidenceRecord(
            "E-" + "a" * 12,
            "source_static",
            EvidenceSource("a" * 64, "server.js"),
            "source_pattern_match",
            "possible_process_state",
            "inferred",
            ("probe-1",),
        )


def test_ai_inference_cannot_confirm_itself():
    with pytest.raises(ValueError, match="independent verifier"):
        EvidenceRecord(
            "E-" + "a" * 12,
            "ai_inference",
            EvidenceSource("a" * 64, "server.js"),
            "model_prediction",
            "process_local_state",
            "confirmed",
        )
