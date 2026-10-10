# Persisted approval and opt-in submission API

This follows the atomic admission foundation. It adds persisted approval decisions,
consumption/revocation, and an explicitly composed authenticated HTTP boundary.
The default `sky-service api` remains read-only: no production write routes, DB
migrations, AWS deployments or queue consumption are enabled by this change.

## Trust and approval creation

`PostgresDeploymentApprovals.approve(verified_principal, prepared_artifact,
validated_plan, seconds=900)` records the approver, organization, prepared source,
canonical plan digest, source tree digest, server-side expiration, and configured
AWS account/region. Lifetime is 1 second to 24 hours. It establishes application
ownership using the same admission rules (existing apps require explicit import).
Approval creation therefore reserves ownership even before a job is admitted.

This internal method is not an arbitrary client plan endpoint. The future upload,
analysis and preview flow must validate plan semantics, secret references, policy
and artifact provenance, then invoke it only after an authenticated explicit
approval action. A Principal must come from verified current membership; digest
matching alone does not authorize approval. No automatic decision or second-person
review policy is introduced. The requesting deployer approves their own deployment.

Approval creation is not idempotent yet. If its commit acknowledgement is lost,
its generated ID may be unknown to the caller; do not silently repeat creation.
A recoverable preview/draft identity is needed with the future preparation flow.

## Atomic submission

`submit(principal, approval_id, request_key)` reads the stored plan/source under a
row lock. Clients cannot replace either. Only the recorded approver with current
DEPLOY permission in the same organization can submit. Same-organization admins
may revoke another user's unused approval; they cannot impersonate its approver.
Foreign resources are hidden with not-found errors.

Source/plan digests and execution account/region are checked. Expiry/revocation
are checked both before admission and again when consuming the approval: a request
which expires while waiting for an application lock rolls back. Consumption and
job/operation/scope/event/outbox creation share the same DB transaction. A busy app
or any failure leaves the approval unconsumed. S3 remains outside this transaction.

An identical request replays the committed receipt, including after expiry or
operation completion, without queueing another deployment. A different request
cannot consume the same approval twice. Receipt replay still requires current
membership and deploy permission. Revocation and submission serialize on the same
row: either revocation prevents admission or admission prevents revocation.
An already-admitted job needs a separate cancellation workflow; revoking its
approval does not cancel an operation or release its scope.

The immutable operation command and job projection also record approval_id for
worker verification and audit. Request keys remain organization/user scoped, but
the exact approval ID is part of the immutable command: a different approval
cannot reuse that key, even if its source and plan are identical. Do not reuse
request keys for different intended deployments.

## HTTP boundary

`DatabaseApprovalApp` + `handler_for_approvals` are explicitly composed with the
read service, approval service, hosted request authenticator, a trusted canonical
origin and `approvals.check_ready`. Use the same database/workspace across services.
Production origin must use HTTPS; HTTP is accepted only for loopback development.

- `POST /api/deployment-approvals/{uuid}/submit`: JSON `{"request_key":"..."}`.
  Returns 202 with job_id and operation_id (also for identical receipt replay).
- `POST /api/deployment-approvals/{uuid}/revoke`: JSON `{}`. Returns 200.
- Existing owned GET reads remain available. Legacy upload/mutation methods remain
  unavailable. The legacy UI read_only flag stays true; approval_submission is an
  additional capability and does not enable its old ZIP upload forms.

Every POST authenticates first and requires exactly the configured Origin to
prevent cookie-based cross-origin submission. Require Content-Type application/json,
one Content-Length and a body of at most 1024 bytes; transfer encoding, duplicate
JSON fields, extra identity/source/plan claims and invalid keys are rejected.
Do not allow ingress paths that bypass trusted ALB authentication validation.

403 means denied identity/role/origin, 404 hides missing or foreign approvals,
409 covers unavailable approval, busy application or idempotency/record conflicts,
503 means temporary state DB failure (including uncertain commit). Retry a 503
using the same approval ID/request key; do not create a new request blindly.
Stored configuration corruption maps to a sanitized 500.

## Migration, readiness and remaining activation

Explicit `initialize()` adds deployment_approvals and approval_schema_versions
version 1 under the shared migration lock. No runtime DDL occurs. Existing metadata,
operation and admission ledger versions remain unchanged. Foundation initializers
have separate transactions; only the new approval migration is one transaction.
`check_ready()` performs read-only checks of the foundations, ownership and approval
ledgers and required columns. It never creates tables or consumes approvals.

Before production activation: coordinate DB migration/import with teammates, connect
recoverable upload/preparation/preview and explicit approval actions, wire the hosted
API with current membership checks, implement the SQS consumer/builder/effect recovery
and lease-fenced status projection, then test real AWS end-to-end. The existing API
runtime does not yet compose this handler. Default-read-only behavior is preserved
until the actual deployment path is ready.
