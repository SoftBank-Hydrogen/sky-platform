from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg import sql

from adapters.database.workload_metrics import PostgresWorkloadMetrics
from domain.database import DatabaseBinding
from tests.contract.test_promotion_copy import databases as databases  # noqa: PLC0414


def test_metrics_read_only_owned_database_with_registered_quota(databases):
    (src, _dst), plan = databases
    # Metrics require real allocation naming, independent from copy-test fixtures.
    suffix = uuid4().hex[:24]
    role, name, marker = "sky_login_" + suffix, "sky_app_" + suffix, "sky-pool-allocation-v1:" + suffix
    with src() as root:
        root.autocommit = True
        root.execute(sql.SQL("CREATE DATABASE {} CONNECTION LIMIT 5").format(sql.Identifier(name)))
        root.execute(sql.SQL("CREATE ROLE {} LOGIN CONNECTION LIMIT 3").format(sql.Identifier(role)))
        root.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(marker))
        )
        root.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(role), sql.Literal(marker)))
    try:
        binding = replace(
            DatabaseBinding(**plan["source"]), database_name=name, owner_ref="allocation-" + suffix
        )
        collector = PostgresWorkloadMetrics(
            src,
            pool_database=plan["source"]["database_name"],
            collector_id="registered",
            clock=lambda: "2026-10-11T00:00:00Z",
        )
        result = collector.collect(binding, storage_quota_bytes=100000000)
        assert result["values"]["connection_limit"] == 3
        assert result["values"]["connections"] == 0 and result["values"]["storage_bytes"] > 0
        assert result["values"]["storage_limit_bytes"] == 100000000
        assert "latency_p95_ms" not in result["values"]
        with pytest.raises(ValueError, match="quotas"):
            collector.collect(
                replace(binding, owner_ref="allocation-foreign", database_name="sky_app_foreign")
            )
    finally:
        with src() as root:
            root.autocommit = True
            root.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            root.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
