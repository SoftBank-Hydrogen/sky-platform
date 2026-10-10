"""A required public URL is a deployment constraint, not just a disclosure choice."""

from engine.candidates import compare_targets
from engine.compatibility import InfrastructureProfile


def test_public_url_requirement_rejects_loopback_and_private_candidates():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    reports, candidates = compare_targets(
        profile,
        {"local-docker": None, "aws-ecs-express": None, "cloud-run": None},
        public_access=True,
        public_url_required=True,
    )
    by_id = {item["id"]: item for item in candidates}
    assert by_id["local-docker"]["status"] == "rejected"
    assert "ACCESS-PUBLIC-REQUIRED" in by_id["local-docker"]["violated_rule_ids"]
    assert by_id["aws-ecs-express"]["status"] == "eligible"
    assert by_id["cloud-run"]["status"] == "eligible"
    assert not next(item for item in reports if item["target"] == "local-docker")["preview_eligible"]
