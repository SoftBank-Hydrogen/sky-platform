"""Operator-configured policies; browser requests cannot choose a DB target."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from application.operating_review import OperatingReview, ScheduledPromotionPolicy
from domain.access import Action, ResourceOwner, owner_from_record, permitted
from domain.database import DatabaseBinding


def load_operating_policies(path, *, account_id, region):
    with Path(path).open("rb") as source:
        raw = source.read(16385)
    if len(raw) > 16384:
        raise ValueError("Operating policies exceed the size limit")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate operating policy field")
            value[key] = item
        return value

    document = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(document, dict) or set(document) != {"schema_version", "policies"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["policies"], list) or not 1 <= len(document["policies"]) <= 50):
        raise ValueError("Invalid operating policy document")
    policies = {}
    for item in document["policies"]:
        if (not isinstance(item, dict)
                or set(item) != {"job_id", "policy_id", "review_after", "target",
                                 "max_health_age_seconds", "websocket_sessions"}
                or not isinstance(item["job_id"], str)
                or not re.fullmatch(r"[a-f0-9]{16}", item["job_id"])
                or item["job_id"] in policies or not isinstance(item["target"], dict)):
            raise ValueError("Invalid or duplicate operating job policy")
        target = DatabaseBinding(**item["target"])
        if (target.account_id, target.region) != (account_id, region):
            raise ValueError("Operating target differs from registered account/region")
        policies[item["job_id"]] = ScheduledPromotionPolicy(
            item["policy_id"], item["review_after"], target,
            item["max_health_age_seconds"], item["websocket_sessions"])
    return policies


class OperatingReviewController:
    def __init__(self, records, policies, *, clock=None):
        self.records = records
        self.service = OperatingReview(records)
        self.policies = dict(policies)
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def _policy(self, principal, job_id):
        # Authorization precedes policy lookup to avoid disclosing foreign jobs.
        self.service.writer.snapshot(principal, job_id)
        if job_id not in self.policies:
            raise ValueError("No registered operating policy")
        return self.policies[job_id]

    def review(self, principal, job_id):
        return self.service.review(principal, job_id, self._policy(principal, job_id), now=self.clock())

    def approve(self, principal, job_id, proposal_id):
        return self.service.approve(principal, job_id, proposal_id,
                                    self._policy(principal, job_id), now=self.clock())

    def detail(self, principal, job_id):
        if not permitted(principal, Action.READ, ResourceOwner(principal.organization_id, principal.user_id)):
            raise PermissionError("Operating read access denied")
        snapshot = self.records.load_job(job_id)
        if not permitted(principal, Action.READ, owner_from_record(snapshot.record)):
            raise FileNotFoundError("Deployment not found")
        proposal = snapshot.record.get("operating_proposal")
        return {"job_id": job_id, "proposal": proposal, "execution_enabled": False,
                "status_basis": "persisted_review_only", "requires_revalidation": True}
