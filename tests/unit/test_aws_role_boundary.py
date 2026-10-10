"""The B-phase worker must bind every managed IAM role to its account boundary."""

import pytest

from adapters.aws.role_boundary import managed_role_boundary

ACCOUNT = "123456789012"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/sky-dev-deployed-app-boundary"


def test_shared_service_requires_its_own_account_boundary():
    assert (
        managed_role_boundary(ACCOUNT, {"SKY_ENVIRONMENT": "dev", "SKY_AWS_ROLE_BOUNDARY_ARN": BOUNDARY})
        == BOUNDARY
    )
    with pytest.raises(ValueError, match="필요합니다"):
        managed_role_boundary(ACCOUNT, {"SKY_ENVIRONMENT": "dev"})
    with pytest.raises(ValueError, match="대상 AWS 계정"):
        managed_role_boundary(
            ACCOUNT,
            {
                "SKY_ENVIRONMENT": "dev",
                "SKY_AWS_ROLE_BOUNDARY_ARN": "arn:aws:iam::999999999999:policy/sky-dev-deployed-app-boundary",
            },
        )
    with pytest.raises(ValueError, match="대상 AWS 계정"):
        managed_role_boundary(
            ACCOUNT,
            {
                "SKY_ENVIRONMENT": "dev",
                "SKY_AWS_ROLE_BOUNDARY_ARN": f"arn:aws:iam::{ACCOUNT}:policy/sky-prod-deployed-app-boundary",
            },
        )


def test_local_a_phase_remains_unbound():
    assert managed_role_boundary(ACCOUNT, {}) == ""
