"""Default API stays read-only; preparation must be explicit and hosted."""

from unittest.mock import patch

import pytest

from interfaces.b_runtime import main


@pytest.mark.parametrize(
    "options",
    [
        ["--origin", "https://example.com"],
        ["--enable-preparation"],
        ["--enable-preparation", "--origin", "https://example.com", "--auth-mode", "local"],
    ],
)
def test_incomplete_preparation_configuration_fails_without_runtime(options):
    with patch("interfaces.b_preparation_runtime.run_preparation_api") as run, pytest.raises(SystemExit):
        main(["api", *options])
    run.assert_not_called()


def test_explicit_preparation_dispatch_preserves_default_api_behavior():
    with patch("interfaces.b_preparation_runtime.run_preparation_api") as run:
        main(
            [
                "api",
                "--enable-preparation",
                "--origin",
                "https://example.com",
                "--alb-trusts-file",
                "trust.json",
                "--memberships-file",
                "members.json",
            ]
        )
    assert run.call_args.args[0].enable_preparation
    assert run.call_args.args[0].origin == "https://example.com"


def test_preparation_failure_is_sanitized(capsys):
    with (
        patch(
            "interfaces.b_preparation_runtime.run_preparation_api", side_effect=OSError("private password")
        ),
        pytest.raises(SystemExit),
    ):
        main(
            [
                "api",
                "--enable-preparation",
                "--origin",
                "https://example.com",
                "--alb-trusts-file",
                "trust.json",
                "--memberships-file",
                "members.json",
            ]
        )
    assert "private" not in capsys.readouterr().err
