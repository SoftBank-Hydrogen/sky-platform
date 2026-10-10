# B upload → durable preview → approval → admission

This change composes the S3 artifact, atomic admission and stored approval foundations
into one explicit HTTP application and a small `/prepare` page. It does not modify the
legacy API startup, run uploaded code, build images, contact an AI provider, execute
migration SQL or apply changes to an operational DB/AWS resource. Default API mode
continues to be read-only. Explicit hosted preparation startup is available via:

`sky-service api --enable-preparation --origin https://<service-host> --alb-trusts-file <trusts.json> --memberships-file <memberships.json>`

Alternatively, omit both identity file options and provide SKY_ALB_TRUSTS_JSON and
SKY_MEMBERSHIPS_JSON using PR #22's validated environment document format. Mixed or
partial identity sources are rejected without exposing document contents.

This opt-in mode requires configured DB/S3 settings and already-applied foundation,
approval and preview schemas. Startup checks readiness without migrations or local
state, creates no legacy App/poller/outbox publisher, and fails closed if schemas or
hosted authentication are missing. Coordinate enabling with the team: successful
admission only queues work until a compatible deployment consumer is running.

## Flow

1. An authenticated deployer uploads a ZIP with an application ID, idempotency key,
   selected HTTP port and health path. The preparation service reserves a durable
   preview before S3 calls and records the upload/options fingerprint and AWS scope.
2. A 120-second preparation lease fences completion. The source is validated,
   filtered and stored in S3; analysis reads the restored temporary source without
   running npm, Docker, JavaScript, Python app code or shell commands.
3. The existing static analyzer and make_plan validators create a source-bound plan.
   Selected port/health path replace the analyzer's 3000/`/` assumptions. A prepared
   snapshot is stored separately; this phase does not alter uploaded application code.
4. Plan, inspection, original/prepared references, blockers and their combined digest
   are committed to PostgreSQL. A ready preview can be read after API restart.
5. An explicit approval click supplies only preview_digest. Under one transaction,
   the stored preview is locked and verified, and an approval is created and linked.
   Concurrent clicks or an uncertain commit replay the same approval ID.
6. The user submits that stored approval through the previous approval API. Job,
   operation, mutation scope, queued event, outbox and approval consumption commit
   together. Actual building/deployment still requires the SQS consumer/sky-builder.

Source objects are outside DB transactions. Failure can leave an orphan; S3 retention
and DB-aware cleanup remain required. Preparing a preview establishes application
ownership before admission. Existing apps still require explicit ownership import.
An interrupted pending upload must be retried with the same ZIP and options/key;
after lease expiry it can reclaim preparation. There is no autonomous background
recovery or AI transformation queue in this change. Temporary directories are cleaned
by the source service. Credentials/environment values are not accepted by these routes.

## Game ZIP verification and storage blockers

Reference: SoftBank-Hydrogen/demo-game/TUG-Sky-almostfinaltest.zip, Git blob
84ae0701663d010c9d860f7278ffe5677a666b8f, upload SHA256
cad3fed07b45bd01bea4729d16991fcd8041ed24f7a3764e89b6458d372a824f.

The TUG app uses Node 22, port 8080, `/health`, WebSocket `/ws`, a probe acknowledgement,
and SQLite data/scores.db with 13 existing rounds. Preparation retains that DB in the
source and uses the existing read-only SQLite conversion preflight to report its
schema and row counts. It deliberately blocks approval: compiling an import snapshot
does not rewrite db.js, change dependencies/async queries, provision app PostgreSQL,
or validate result storage. These are separate work still required before deployment.
Sky's platform-state PostgreSQL is not the game's future PostgreSQL database.

Other infrastructure requirements and required environment bindings also block
approval until preparation support is implemented. Unknown/unsupported SQLite
snapshots show a blocker rather than an empty-data or stateless deployment success.
The plan has replicas=1; runtime WebSocket/memory behavior and real build/health
verification are not inferred from successful source inspection.

## HTTP composition

DatabasePreparationApp requires the existing owned read service, approval service,
DeploymentPreparationService(SourceArtifactService(S3 store), PostgreSQL previews),
hosted authenticator and configured canonical origin. All DB services must share
workspace/configuration. Its readiness checks all foundation/preview schemas read-only.

- GET `/prepare`: authenticated upload/preview/approval/submission page.
- POST `/api/deployment-previews`: application/zip, <=20 MiB. Headers:
  X-Application-Id (3..31 lower-case app ID), Idempotency-Key,
  X-Container-Port (1024..65535), X-Health-Path.
- GET `/api/deployment-previews/{uuid}`: preview creator's owned snapshot.
- POST `/api/deployment-previews/{uuid}/approve`: application/json
  `{"preview_digest":"<64 lowercase hex>"}` only.
- Previous stored-approval submit/revoke and owned deployment reads remain available.

POSTs authenticate and enforce DEPLOY permission and exact Origin. Upload JSON plan,
source, principal, environment values, target or AI instructions cannot be supplied.
ZIP contents remain untrusted data. Raw ZIP body only: multipart/folder upload and
AI mode are not part of this boundary yet. API 409 covers changed request content,
active preparation lease, expired/changed preview or outstanding blockers; 429 is
per-process preparation overload; 503 is a temporary/uncertain storage outcome.
Retry identical inputs/keys on 429/503. Body parsing is bounded and rejects duplicate
headers/JSON keys and transfer encoding. One preparation slot per process limits
memory; deployment resources/ingress budgets must be validated before production
activation for maximum-size archives.

The page uses textContent for uploaded plan/inspection text. Upload keys/settings
are retained in sessionStorage across page reload so a lost response can be retried
with the same key after selecting the same file again; source bytes/credentials are
not stored in browser storage. A ready preview URL carries its UUID for recovery.
Explicitly starting a new upload clears that key. Approvals expire separately from
previews; replay returns the original ID and never silently renews a revoked/expired
approval. New approval/preview revision semantics remain future work.

## Maintenance and testing

Explicit initialize adds deployment_previews and preview_schema_versions version 1
under the shared advisory lock. Runtime never migrates. Foundation initializers have
separate commits; the new preview DDL itself is transactional. Coordinate maintenance
migrations/ownership import with teammates. `sky-service migrate` applies the
metadata, operation, admission, approval and preview schemas in that order, so run it
once as a coordinated maintenance step before enabling preparation (see
b-runtime-entrypoints.md). It creates an empty `application_owners` table and does not
import ownership for existing apps.

Contract tests use disposable loopback PostgreSQL plus an in-memory S3 double.
CI uses a small SQLite fixture; the full Unity ZIP is not copied into this repository.
A local pinned-artifact rehearsal can additionally set SKY_TEST_GAME_ZIP to its path.
S3 IAM/network and actual game execution/build/AWS performance are not tested here.
