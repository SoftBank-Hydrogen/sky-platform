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


@pytest.mark.parametrize(
    "environment,files",
    [
        ({"SKY_ALB_TRUSTS_JSON": "private"}, (None, None)),
        ({"SKY_MEMBERSHIPS_JSON": "private"}, (None, None)),
        (
            {"SKY_ALB_TRUSTS_JSON": "private", "SKY_MEMBERSHIPS_JSON": "private"},
            ("trust.json", "members.json"),
        ),
        ({}, ("trust.json", None)),
    ],
)
def test_preparation_identity_rejects_mixed_or_partial_sources(environment, files):
    from argparse import Namespace

    from interfaces.b_preparation_runtime import preparation_identity

    with patch.dict("os.environ", environment, clear=True), pytest.raises(ValueError):
        preparation_identity(Namespace(alb_trusts_file=files[0], memberships_file=files[1]))


def test_preparation_identity_uses_validated_environment_documents():
    from argparse import Namespace

    from interfaces.b_preparation_runtime import preparation_identity

    environment = {"SKY_ALB_TRUSTS_JSON": "trusts-document", "SKY_MEMBERSHIPS_JSON": "members-document"}
    with (
        patch.dict("os.environ", environment, clear=True),
        patch("interfaces.b_preparation_runtime.AlbRequestAuthenticator.from_json") as validate,
    ):
        assert (
            preparation_identity(Namespace(alb_trusts_file=None, memberships_file=None))
            is validate.return_value
        )
    validate.assert_called_once_with("trusts-document", "members-document")
