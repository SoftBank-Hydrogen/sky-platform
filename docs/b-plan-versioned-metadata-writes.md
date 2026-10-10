# Authorized versioned deployment metadata writes

DeploymentMetadataWriter is an internal application service over
VersionedDeploymentRecordStore. This PR changes code only: no migration, DB connection,
S3 upload, queue send or AWS mutation is performed by constructing it. There is no
HTTP endpoint or automatic runtime activation. The existing read-only mode stays read-only.

## Contract

- snapshot(principal, job_id, action=DEPLOY) returns a detached authorized snapshot with
  its metadata revision. A legacy nonversioned adapter is rejected.
- patch(principal, job_id, expected_revision, changes, action=DEPLOY) reads and authorizes
  the current record, rejects a stale revision and then performs an explicit CAS save.
  CAS also detects a competitor that commits after the initial read. Identity, ownership,
  application, creation time, target, source reference/digest, operation and group fields
  cannot be patched. Retirement states require Action.RETIRE, so a deployer cannot
  mark a record deleted through the default deployment permission.
- health_snapshot returns the existing history and its independent revision.
- record_health appends a checked observation using history CAS; None means create-only.
  healthy must be boolean, reason nonempty and checked_at timezone-aware ISO text.
  Retention defaults to 20 (bounded 1..100). It never changes deployment status.
- Returned WriteReceipt contains the job ID and the revision returned by the save,
  not a subsequent read that might already contain someone else's changes.

The caller supplies an authenticated domain Principal and validates the workflow,
source/approval, probe target and worker lease beforehand. This service does not
accept public arbitrary patches, authenticate workers, enforce lease epochs or
validate deployment state-machine transitions. Those checks belong in specific
command handlers before calling this metadata boundary. New application ownership
claims and initial deployment creation still need an atomic admission contract.

Foreign/unowned records are hidden, including from admins. Unlike local main's
legacy exception, shared DB writes never claim ownerless legacy records.

## Failure and integration rules

RecordConflict must be returned to the specific caller to reload and deliberately
resolve; do not blindly replay an old full document. OSError after save can mean
an uncertain commit. No automatic write retry, failure-marker overwrite or fallback
to local files occurs. A caller must re-read and reconcile before deciding another write.

Each save is atomic only for that record. Job/health saves and operation completion
are separate transactions. Do not finish an operation and patch metadata in two calls
and claim atomic completion: a future operation-and-projection transaction port is
required. A terminal metadata state does not prove the actual AWS outcome.

The existing mutable App still uses legacy save/recovery and must not receive this
versioned adapter as a drop-in. Future handlers must retain revisions, use this service
only after command/lease validation, replace startup recovery and resolve ownership
registration before admitting a new deployment.

## Validation without an operational DB

54 unit tests use an in-memory CAS double: role/organization denial, immutable fields,
stale snapshots, a competing write after load, uncertain commits without retry,
health append/retention and invalid/legacy records. Local regression runs unset
SKY_TEST_POSTGRES_DSN and never create a database or connect to RDS.
Existing GitHub CI still runs its normal disposable PostgreSQL service; it does not
access the team's DB. Database deployment/initialization stays with team coordination.

Based on PR #10 and current main (organization enforcement/application registry).
No prerequisite PR is changed or merged on GitHub. This service is deliberately not
wired into the deployment HTTP path until the admission/transaction contract is ready.
