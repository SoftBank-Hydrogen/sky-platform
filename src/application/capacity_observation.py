"""Internal telemetry ingestion; cannot refresh an old HTTP health timestamp."""

from copy import deepcopy
from dataclasses import asdict

from application.operating_review import _binding, deployment_observation_binding
from ports.state import RecordConflict


class CapacityObservationRecorder:
    def __init__(self, review, collector):
        self.review, self.collector = review, collector

    def collect(self, principal, job_id, policy):
        snapshot = self.review.writer.snapshot(principal, job_id)
        source = _binding(snapshot.record)
        if policy.capacity_criteria is None:
            return None
        health = self.review.writer.health_snapshot(principal, job_id)
        if (
            health is None
            or not health.record
            or health.record[-1].get("deployment_binding") != deployment_observation_binding(snapshot.record)
        ):
            raise ValueError("Current deployment-bound HTTP health required")
        metric = self.collector.collect(
            source, storage_quota_bytes=policy.capacity_criteria.storage_quota_bytes
        )
        if metric.get("collector_id") != policy.capacity_criteria.collector_id or metric.get(
            "database_binding"
        ) != asdict(source):
            raise ValueError("Metric collector scope differs from policy")
        if self.review.writer.snapshot(principal, job_id).revision != snapshot.revision:
            raise RecordConflict("Deployment changed during collection")
        observation = {**deepcopy(health.record[-1]), "database_metrics": metric}
        # checked_at stays the original HTTP probe timestamp, not collection time.
        return self.review.writer.record_health(
            principal, job_id, health.revision, observation, retention=100
        )
