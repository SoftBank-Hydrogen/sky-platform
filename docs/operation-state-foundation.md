# Durable operation admission and outbox

This stage builds on PR #7's connection/metadata layer. It is not wired into the existing HTTP App or a live queue, and performs no AWS infrastructure apply.

## Admission

A caller supplies a workspace, application identity, operation kind, idempotency request key and an immutable JSON command (maximum 64 KiB). The caller must first validate source digest, compilation/approval, actor authorization and target account/region. This storage port is not an HTTP authorization boundary. Commands/checkpoints should contain artifact and secret references, never credential values or uploaded files.

The request hash binds the app, kind and canonical command. The same key and command return the same operation, including after completion; reusing the key for another command is an IdempotencyConflict. Admission atomically inserts the operation, app mutation reservation, queued event and outbox event. A second unfinished operation on the same app is ApplicationBusy and is rolled back. This first version conservatively serializes all admitted operations per app. Read-only polls attached to a parent and multi-resource/global reservations need their own subsequent contracts.

Operations, reservations and outbox rows have workspace-scoped identities. Composite foreign keys also prevent reservations and outbox envelopes from referring to another app's operation. The operation schema has a separate migration ledger under sky_state, leaving PR #7 metadata reader initialization compatible. All schema changes are additive, explicit and transactionally serialized by the shared advisory lock.

## Execution ownership

Workers claim a specific operation + current attempt. Only queued operations can be claimed. Ownership is the workspace + operation + attempt + worker + monotonically increasing lease epoch, checked against the database clock. Default lease is 90 seconds; the worker will need an independent heartbeat (proposed 20 seconds). A stale or expired token cannot renew, checkpoint, observe an external request or complete work. Locks are rechecked for expiry after contention.

Completion atomically stores the operation result, changes status, releases the app reservation and appends an event. It does not update the existing metadata projection; that integration must be transactional before exposing the B runtime.

## Checkpoints and uncertain external effects

Before an external mutation, begin_external() persists its stable request intent. The executor must commit this intent successfully before making the request and use the AWS operation's supported idempotency/resource identity. observe_external() stores a verified receipt and resume checkpoint in the same transaction before clearing uncertainty. A generic HTTP success or an unverified caller-provided boolean is not sufficient verification; concrete AWS adapters must implement it.

An expired/interrupted worker with a pending external intent moves to needs_attention, retains the app reservation and emits no replacement execution message. This stage has no automatic reconciler or manual resolution endpoint: those must query the external resource and bind the resolution to the original intent/version. Without uncertainty, recovery preserves the checkpoint, creates a new execution attempt and outbox event, and invalidates the old lease. The future executor must honor the checkpoint and verified receipt rather than restart all deployment steps.

The DB lease cannot prevent a paused worker from later calling AWS. This does not provide exactly-once side effects; external idempotency and reconciliation remain required. Only the latest intent/receipt is projected on the operation; per-step external audit history remains future work.

## Outbox and FIFO publishing

Publishers claim pending outbox rows under short DB transactions using SKIP LOCKED, with publisher owner + epoch + expiry. SQS I/O happens outside the transaction. Confirmation, retry release and crashed-publisher recovery are conditional on ownership. Publishing retries keep the same attempt ID; a new execution attempt after worker recovery gets a new ID so a previous FIFO deduplication window does not suppress its wakeup.

The SQS adapter validates a regional HTTPS FIFO URL in the configured AWS account. It uses MessageGroupId=application_id and MessageDeduplicationId=attempt_id, matching sky-infra. MessageBody contains only version, workspace, operation_id, attempt_id and application_id. Commands and secrets are loaded from DB, not trusted from queue payloads. A lost SQS response or failed DB confirmation may cause another delivery; DB claim prevents duplicate concurrent execution. Published outbox rows and stale messages are retained; the future consumer must validate current attempt/app/workspace against the DB before acting.

Queue visibility (300 seconds with 1–2 minute extension) is separate from the DB execution lease. Queue receiving/acknowledgment, heartbeat scheduling, ECS task protection and SIGTERM handling are the next worker stage. No polling/background loop is auto-started by importing these modules.

## Validation

CI uses the disposable PostgreSQL 17 service from PR #7. New tests cover concurrent equal/conflicting admission, transaction rollback, competing workers/publishers/recovery scanners, expired ownership and lock contention, uncertain external requests, atomic receipt/checkpoint persistence, workspace/app isolation, cold migration failure, and lost queue/DB responses. AWS calls are mocked; no real SQS messages are sent. Tests reject non-loopback DB endpoints.

References: [PostgreSQL locking](https://www.postgresql.org/docs/17/sql-select.html), [AWS FIFO terms](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-key-terms.html).
