from unittest.mock import Mock, patch

import pytest

from domain.access import Role
from interfaces.b_runtime import main
from interfaces.operating_review import OperatingReviewController
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW
from tests.unit.test_promotion_policy import policy_with_criteria, samples

pytest_plugins = ["tests.unit.test_operating_review", "tests.unit.test_dedicated_database_worker"]


def test_bounded_review_resolves_consent_actor_and_is_idempotent(setup):
    records, policy, service = setup
    policy = policy_with_criteria(policy)
    samples(records)
    service.grant_automation(
        principal(),
        ID,
        policy,
        max_incremental_monthly_cents=3000,
        expires_at="2026-10-12T00:00:00+00:00",
        now=NOW,
    )
    controller = OperatingReviewController(records, {ID: policy}, clock=lambda: NOW)
    resolver = Mock(return_value=principal())
    report = controller.review_automations_once(resolver)
    assert report[0]["status"] == "approved"
    assert controller.review_automations_once(resolver) == report
    resolver.assert_called_with("user1", "org1")
    assert (
        controller.review_automations_once(lambda *args: principal(role=Role.VIEWER))[0]["status"]
        == "review_blocked"
    )


def test_no_consent_means_no_collection_or_review(setup):
    records, policy, _ = setup
    controller = OperatingReviewController(records, {ID: policy}, clock=lambda: NOW)
    collect = Mock(side_effect=AssertionError("Must not collect"))
    assert controller.review_automations_once(Mock(), collect=collect)[0]["status"] == "no_automation_consent"
    assert records.save_calls == 0


def test_cli_configuration_check_makes_no_aws_or_db_calls(registered, capsys):
    with (
        patch("interfaces.operating_review_worker.load_hosted_identity", return_value=Mock()),
        patch(
            "interfaces.operating_review_worker.RotatingDatabaseConnection",
            side_effect=AssertionError("DB call"),
        ),
        patch("boto3.client", side_effect=AssertionError("AWS call")),
    ):
        main(
            ["worker", "--mode", "operating-review", "--once", "--collect-capacity-metrics", "--check-config"]
        )
    assert "no AWS/DB calls" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra", [[], ["--once", "--initialize-pool"], ["--once", "--operation-id", "arbitrary"]]
)
def test_cli_rejects_unbounded_or_mutating_review(registered, extra):
    with pytest.raises(SystemExit) as error:
        main(["worker", "--mode", "operating-review", "--check-config", *extra])
    assert error.value.code == 2


def test_runtime_once_uses_metadata_only_without_allocator(registered, setup, capsys):
    records, _policy, _service = setup
    with (
        patch("interfaces.operating_review_worker.load_hosted_identity", return_value=Mock()),
        patch("interfaces.operating_review_worker.RotatingDatabaseConnection", return_value=Mock()),
        patch("interfaces.operating_review_worker.check_database_ready") as ready,
        patch("interfaces.operating_review_worker.PostgresDeploymentRecordStore", return_value=records),
        patch(
            "adapters.aws.shared_database.AwsSharedDatabaseAllocator",
            side_effect=AssertionError("Workload access"),
        ),
    ):
        main(["worker", "--mode", "operating-review", "--once"])
    ready.assert_called_once()
    assert "no_automation_consent" in capsys.readouterr().out
