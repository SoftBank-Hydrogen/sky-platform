"""A supplied credential must not be copied into a deployment image."""

import pytest

from application.source_secrets import (
    reject_plaintext_cloud_secret_names,
    reject_plaintext_cloud_secrets,
    reject_supplied_secrets_in_source,
)


def test_supplied_credential_in_source_is_rejected_without_echoing_value(tmp_path):
    secret = "synthetic-private-value"
    (tmp_path / "server.py").write_text(f'API_TOKEN = "{secret}"\n')
    with pytest.raises(ValueError, match="CV-07") as error:
        reject_supplied_secrets_in_source(tmp_path, {"API_TOKEN": secret})
    assert secret not in str(error.value)


def test_non_secret_runtime_configuration_is_not_treated_as_a_credential(tmp_path):
    (tmp_path / "server.py").write_text('MODE = "production"\n')
    reject_supplied_secrets_in_source(tmp_path, {"APP_MODE": "production"})


@pytest.mark.parametrize("name", ["PGPASSWORD", "DATABASE_URL", "REDIS_URL", "APP_API_KEY"])
def test_common_database_and_api_secret_names_are_checked(tmp_path, name):
    (tmp_path / "config.js").write_text('const value = "synthetic-private-value";\n')
    with pytest.raises(ValueError, match="CV-07"):
        reject_supplied_secrets_in_source(tmp_path, {name: "synthetic-private-value"})


def test_supplied_credential_scan_fails_closed_for_symbolic_links(tmp_path):
    (tmp_path / "real.py").write_text("print('ok')\n")
    (tmp_path / "linked.py").symlink_to(tmp_path / "real.py")
    with pytest.raises(ValueError, match="CV-07"):
        reject_supplied_secrets_in_source(tmp_path, {"APP_SECRET": "synthetic-private-value"})


@pytest.mark.parametrize("target", ["aws-ecs-express", "cloud-run"])
def test_cloud_requires_secret_reference_for_supplied_credentials(target):
    value = "synthetic-private-value"
    with pytest.raises(ValueError, match="CV-07.*SecretRef") as error:
        reject_plaintext_cloud_secrets({"APP_SECRET": value}, target)
    assert value not in str(error.value)
    reject_plaintext_cloud_secrets({"APP_MODE": "production"}, target)
    reject_plaintext_cloud_secrets({"APP_SECRET": value}, "local-docker")


def test_cloud_secret_name_is_rejected_before_requesting_its_value():
    with pytest.raises(ValueError, match="CV-07.*SecretRef"):
        reject_plaintext_cloud_secret_names(["APP_SECRET"], "aws-ecs-express")
    reject_plaintext_cloud_secret_names(["APP_MODE"], "aws-ecs-express")
