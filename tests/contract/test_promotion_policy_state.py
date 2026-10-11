from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from adapters.state.postgres import PostgresDeploymentRecordStore
from application.database_promotion_workflow import approved_snapshot
from application.operating_review import OperatingReview
from interfaces.operating_review import OperatingReviewController
from tests.contract.test_operation_store import database as database  # noqa: PLC0414
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW
from tests.unit.test_operating_review import operating_review_setup as review_setup
from tests.unit.test_promotion_policy import policy_with_criteria, samples

operating_setup = pytest.fixture(name="operating_setup")(review_setup.__wrapped__)

pytest_plugins = ["tests.unit.test_operating_review"]


def test_concurrent_timers_persist_one_approval_and_revocation_blocks_execution(database, operating_setup):
    fake, policy, _service = operating_setup
    policy = policy_with_criteria(policy)
    samples(fake)
    workspace = uuid4().hex
    records = PostgresDeploymentRecordStore(database, workspace=workspace)
    records.save_job(ID, fake.job, expected_revision=None)
    records.save_health(ID, fake.health, expected_revision=None)
    service = OperatingReview(records)
    service.grant_automation(
        principal(),
        ID,
        policy,
        max_incremental_monthly_cents=3000,
        expires_at="2026-10-12T00:00:00+00:00",
        now=NOW,
    )

    def tick(_):
        return OperatingReviewController(
            PostgresDeploymentRecordStore(database, workspace=workspace), {ID: policy}, clock=lambda: NOW
        ).review_automations_once(lambda *args: principal())

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(tick, range(2)))
    approved = records.load_job(ID).record["operating_proposal"]
    assert approved["status"] == "approved"
    assert records.load_job(ID).revision == 3
    assert tick(None)[0]["proposal_id"] == approved["proposal_id"]
    assert approved_snapshot(records, principal(), ID, approved["proposal_id"], policy, NOW)[1] == approved
    service.revoke_automation(principal(), ID)
    with pytest.raises(ValueError):
        approved_snapshot(records, principal(), ID, approved["proposal_id"], policy, NOW)
