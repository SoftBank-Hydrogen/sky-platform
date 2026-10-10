"""Evidence-bound promotion criteria; elapsed time alone is not capacity evidence."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise ValueError("Timezone-aware policy time required") from None


@dataclass(frozen=True)
class CapacityPromotionCriteria:
    collector_id: str
    sustained_seconds: int = 300
    minimum_samples: int = 3
    maximum_gap_seconds: int = 180
    latency_p95_ms: int | None = None
    connections_percent: int | None = None
    storage_percent: int | None = None
    storage_quota_bytes: int | None = None

    def __post_init__(self):
        if not isinstance(self.collector_id, str) or not self.collector_id or len(self.collector_id) > 128:
            raise ValueError("Registered metric collector required")
        for value, lower, upper in (
            (self.sustained_seconds, 60, 86400),
            (self.minimum_samples, 2, 100),
            (self.maximum_gap_seconds, 1, 3600),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Invalid sustained observation bounds")
        if self.storage_quota_bytes is not None and (
            type(self.storage_quota_bytes) is not int or not 1 <= self.storage_quota_bytes < 2**63
        ):
            raise ValueError("Invalid app storage quota")
        if self.storage_percent is not None and self.storage_quota_bytes is None:
            raise ValueError("Storage criterion requires an explicit app quota")
        if all(
            value is None for value in (self.latency_p95_ms, self.connections_percent, self.storage_percent)
        ):
            raise ValueError("At least one capacity threshold required")
        for value, upper in (
            (self.latency_p95_ms, 600000),
            (self.connections_percent, 100),
            (self.storage_percent, 100),
        ):
            if value is not None and (type(value) is not int or not 1 <= value <= upper):
                raise ValueError("Invalid capacity threshold")


@dataclass(frozen=True)
class PromotionCostQuote:
    incremental_monthly_cents: int
    evidence_ref: str
    valid_until: str
    currency: str = "USD"

    def __post_init__(self):
        if (
            type(self.incremental_monthly_cents) is not int
            or not 0 <= self.incremental_monthly_cents <= 100000000
        ):
            raise ValueError("Invalid incremental cost estimate")
        if (
            self.currency != "USD"
            or not isinstance(self.evidence_ref, str)
            or not self.evidence_ref
            or len(self.evidence_ref) > 512
        ):
            raise ValueError("Referenced USD cost estimate required")
        timestamp(self.valid_until)


def _measurement(metric, values):
    if not isinstance(values, dict):
        return None
    if metric == "latency_p95_ms":
        value = values.get(metric)
        return (
            value
            if type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value) and value >= 0
            else None
        )
    count, limit = (
        ("connections", "connection_limit")
        if metric == "connections_percent"
        else ("storage_bytes", "storage_limit_bytes")
    )
    value, budget = values.get(count), values.get(limit)
    if type(value) is not int or not 0 <= value < 2**63 or type(budget) is not int or not 0 < budget < 2**63:
        return None
    return 100 * value / budget


def evaluate_capacity(criteria, observations, *, database_binding, deployment_binding, now, max_age_seconds):
    """Only trusted recorded, app-scoped samples from the registered collector.

    Callers must control telemetry ingestion. A collector_id string by itself
    does not authenticate data. Pool-wide CPU/latency cannot be attributed to
    an individual app by this evaluator. No browser-submitted metrics accepted.
    """
    current = timestamp(now)
    if not isinstance(observations, list) or len(observations) > 100:
        raise ValueError("Bounded recorded observation history required")
    samples = []
    for observation in observations:
        if observation.get("deployment_binding") != deployment_binding:
            continue
        data = observation.get("database_metrics")
        if (
            not isinstance(data, dict)
            or type(data.get("schema_version")) is not int
            or data.get("schema_version") != 1
            or data.get("collector_id") != criteria.collector_id
            or data.get("database_binding") != database_binding
        ):
            continue
        checked = timestamp(data.get("observed_at", observation.get("checked_at")))
        if checked > current or samples and checked <= samples[-1][0]:
            raise ValueError("Metric times must be strictly increasing and not future dated")
        samples.append((checked, data.get("values"), checked.isoformat()))
    reasons = []
    for metric in ("latency_p95_ms", "connections_percent", "storage_percent"):
        threshold = getattr(criteria, metric)
        if threshold is None:
            continue
        run = []
        for sample in reversed(samples):
            value = _measurement(metric, sample[1])
            if metric == "storage_percent" and (
                not isinstance(sample[1], dict)
                or sample[1].get("storage_limit_bytes") != criteria.storage_quota_bytes
            ):
                value = None
            if (
                value is None
                or value < threshold
                or run
                and (run[-1][0] - sample[0]).total_seconds() > criteria.maximum_gap_seconds
            ):
                break
            run.append(sample)
        qualified = bool(
            run
            and len(run) >= criteria.minimum_samples
            and (run[0][0] - run[-1][0]).total_seconds() >= criteria.sustained_seconds
            and 0
            <= (current - run[0][0]).total_seconds()
            <= min(max_age_seconds, criteria.maximum_gap_seconds)
        )
        reasons.append(
            {
                "metric": metric,
                "threshold": threshold,
                "qualified": qualified,
                "samples": len(run),
                "first_at": run[-1][2] if run else None,
                "last_at": run[0][2] if run else None,
            }
        )
    evidence = [
        {
            "observed_at": sample[2],
            "values": {
                metric: _measurement(metric, sample[1])
                for metric in ("latency_p95_ms", "connections_percent", "storage_percent")
            },
        }
        for sample in samples
    ]
    return {
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "eligible": any(reason["qualified"] for reason in reasons),
        "reasons": reasons,
        "sample_count": len(samples),
    }
