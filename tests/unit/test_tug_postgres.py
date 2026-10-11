"""Unsupported ZIPs remain unchanged; explicit recipes never silently generalize."""

import pytest

from application.tug_postgres import validate_tug_source


def test_unrecognized_tug_source_rejected_without_editing(tmp_path):
    (tmp_path / "server.js").write_text("different game")
    with pytest.raises(ValueError, match="reviewed ZIP"):
        validate_tug_source(tmp_path)
    assert (tmp_path / "server.js").read_text() == "different game"


def test_postgres_template_numeric_limits_and_async_calls():
    import subprocess

    result = subprocess.run(
        ["node", "tests/regression/test_tug_postgres.cjs"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
