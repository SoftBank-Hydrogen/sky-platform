# PostgreSQL state foundation

This step depends on the state port in PR #6. The adapter implements deployment metadata, health history and GitHub-source settings through the existing port. It is a compatibility projection, not the final operation/lease/outbox schema.

## Infrastructure contract

The connection factory reads SKY_DATABASE_HOST, SKY_DATABASE_PORT (5432 by default), SKY_DATABASE_NAME, SKY_DATABASE_SECRET_ARN and SKY_AWS_REGION. It fetches the RDS-managed username/password with Secrets Manager using the task role. It never trusts host/port fields from the secret. Supply SKY_DATABASE_SSLROOTCERT pointing to the Amazon RDS CA bundle (default /etc/ssl/certs/sky-rds-global-bundle.pem). TLS uses verify-full. The existing service image does not yet install that bundle or the optional state-postgres dependencies.

Credential cache is five minutes. A connection failure refreshes the secret and retries connection once. SQL statements and commits are never retried automatically: their outcome can be uncertain. No credential content or SQL diagnostics are included in adapter errors.

## Schema lifecycle

Explicit initialize() applies migration 1 under a transaction-scoped advisory lock. Unknown schema versions stop startup; errors roll back the migration. Additive, future migrations must preserve old readers during rolling deployment. The current metadata_records JSONB table is namespaced under sky_state and scoped by workspace. It does not create, change or manage the RDS instance; sky-infra owns that resource.

## Activation gate

Do not inject this adapter into the legacy production App yet. App.restore() currently rewrites running jobs on startup and HTTP queries use process-local jobs. PostgreSQL alone does not fix those semantics. Before B activation we still need request-time DB reads, operation admission/idempotency/lease/outbox, SQS FIFO worker, leader election, S3 artifacts, remote builds, non-root Fargate image and runtime wiring. Other local database/network/snapshot operation records are not migrated by this adapter.

The infra contract selects PostgreSQL 17, FIFO message groups by app ID, deduplication by attempt ID, and visibility 300 seconds extended every 1–2 minutes. These replace the earlier Standard-queue/120-second draft defaults. Task protection and SIGTERM checkpoints remain future worker work.

## Validation

Install `pip install -e '.[dev,state-postgres]'`. Default tests do not need AWS. Unit tests cover credential rotation, TLS configuration, bounded connection retries and non-retried write failures. GitHub CI starts a disposable PostgreSQL 17 service and sets SKY_TEST_POSTGRES_DSN; integration tests reject non-loopback endpoints. They check independent adapter instances, workspace isolation, concurrent schema initialization, rollback preservation and future-schema refusal. The local integration DB uses plaintext loopback transport; an actual RDS/TLS connection is not claimed by these tests.

A disposable local PostgreSQL server was additionally configured with a test certificate: the factory connected successfully over TLS with a trusted CA and rejected the mismatching host name. This is a local TLS check, not an AWS RDS connectivity check.
