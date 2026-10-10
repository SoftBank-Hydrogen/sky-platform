# B deployment admission foundation

This internal port atomically admits an AWS deployment after source preparation
and approval. It does not activate an upload endpoint, a deployment consumer, or
remote image building. The B API remains read-only. No operational DB migration
is performed by this change.

## Trusted composition requirements

The future application boundary must authenticate the principal, resolve current
organization membership, load the stored approval and prepared SourceArtifact,
and supply the approval's source and canonical plan digests. These are not HTTP
fields which clients may assert as proof of approval. A matching digest detects
changes; it does not authenticate an approver. Rechecking a revoked approval and
atomically claiming approval consumption are still future work before exposing
write routes. Never store credential values in the plan; only validated immutable
execution configuration and secret references belong there. The adapter bounds
JSON size but is not the application plan validator.

The target is AWS only. Account and region come from trusted adapter configuration
and are pinned in the operation command. The worker must verify its actual AWS
identity, validate plan semantics and execution permissions, and restore/hash-check
the S3 source under its lease before any side effect. Original uploads cannot be
admitted; an approved prepared snapshot is required. This stage does not prove
that the S3 object exists or remains available.

## One database commit

`PostgresDeploymentAdmission.admit` uses one connection/transaction for:

1. Creating or locking workspace-wide application ownership.
2. Inserting or replaying the operation with organization/user-scoped request key.
3. Reserving the application's mutation scope, adding a queued event and outbox.
4. Creating the owned deployment job with the same source/plan and operation ID.

All of these roll back together on a failure. No SQS or S3 call occurs in this
transaction. S3 upload happens first; an admission failure can leave an orphan
object. Retention/cleanup must respect DB references; this is not a distributed
S3/DB transaction. Outbox publishing follows the commit using the existing
publisher and its retry policy.

Identical retries return the original IDs and preserve job progress. Different
immutable content under the same request key raises IdempotencyConflict. Keys are
scoped by workspace, organization and requesting user. The deterministic 16-hex
job ID preserves existing HTTP identifier shape; collisions fail closed without
replacing another record. A completed operation is not queued again on replay.
A new request may redeploy after the previous mutation scope is released.
ApplicationBusy rolls back the entire new admission.

Application IDs are globally unique within a workspace, matching existing
operation mutation scopes. Same-organization deployers may redeploy; foreign
organizations, including their admins, cannot claim the ID. Ownerless/existing
legacy metadata or operations are not automatically adopted. The separate
application ownership registry requires explicit import under a coordinated
maintenance window for existing apps. Do not mix the old unowned low-level
OperationStore.admit writer with this intake on the same live workspace: it has
no organization ownership boundary and is retained for internal compatibility.

## Migration and activation

`initialize()` is an explicit maintenance operation. It initializes the existing
metadata/operation foundations and then adds application_owners and a separate
admission_schema_versions ledger (version 1) under the shared advisory migration
lock. Existing operation/metadata schema versions remain unchanged. Admission
never initializes tables at runtime. The existing read-only API readiness check
does not yet check admission tables; write runtime activation must add this check.
The metadata/operation foundation initializers use their own commits; the new
ownership schema migration is transactional, but the whole initialize call is
not one all-or-nothing migration across all three ledgers.

Next steps: persisted approval/plan integration, authenticated write composition,
SQS consumer with lease/heartbeat and visibility handling, remote builder, effect
reconciliation, and lease-fenced job status projection. Operation completion does
not currently update the job projection automatically. Only then should actual
end-to-end deployment be enabled. Coordinate operational DB migration and legacy
ownership import with the infrastructure team.
