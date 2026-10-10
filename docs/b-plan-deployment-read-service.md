# PostgreSQL deployment reads (foundation only)

DeploymentReadService provides summaries, detail, releases and health history through
PostgresDeploymentReads. Each query joins job and health records in a single PostgreSQL
statement snapshot, inside a read-only transaction. Independent instances see committed
changes without an in-memory restore or cache. No initializer, recovery, health probe,
operation admission, SQS send or cloud change runs during a read.

## Authorization and responses

The caller supplies an authenticated domain.access.Principal. This reuses main's
organization/role rules; it does not verify ALB/Cognito signatures or membership.
Workspace and organization filters apply in SQL before LIMIT. Invalid/missing owners
are hidden, including from administrators. Other-organization and absent details both
raise FileNotFoundError. The service checks ownership again at the port boundary.
Owned malformed records fail with a bounded error instead of inventing healthy state.

Each projection preserves the existing response item fields. Detail includes diagnosis,
health_history, last_health and monitor_error. Health history is a pure persisted read;
it is deliberately different from the current GET health endpoint that performs a probe.
monitor_error is read only when persisted in the job; old process-local monitor errors
are unavailable. No new metadata revision is inserted into the legacy response payload.

Summaries/releases return DeploymentPage(items, next_cursor), default 50/max 100,
with descending created_at text and record_id as a stable tie-breaker. A cursor is
scoped to organization and application filter. It is an internal typed value, not an
HTTP token. The HTTP adapter must decide its versioned pagination envelope/serialization
before activation: no existing endpoint response is silently changed here.
Pages are separate snapshots; concurrent edits to created_at can affect traversal.
The single-query snapshot prevents mixed database commit points, but does not make
separately committed job and health updates one logical deployment generation.
Large tables will need measured query plans and appropriate indexes before rollout.

## Integration still required

Existing App/HTTP handlers continue using the current local implementation. Do not
activate PostgreSQL by injecting the versioned write adapter into legacy startup:
recovery still mutates state and writes without expected_revision. The next integration
must supply verified identity, connect read routes, choose pagination compatibility,
move monitor error persistence, and replace shared writers/unsafe startup recovery.
This PR does not implement the S3 source store, hosted auth, worker or builder workflow.

The branch is based on PR #8 and also merges current main to reuse its auth model.
PR #6/#7/#8 are not modified; retarget once prerequisites have merged.
