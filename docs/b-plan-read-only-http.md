# Read-only PostgreSQL HTTP integration

## Activation and isolation

`sky-platform --read-only-database --host 127.0.0.1 --port 8080` or
`sky-service --read-only-database` starts a separate read app. It never constructs
legacy App, acquires StateDirectoryLock, restores/mutates local jobs, initializes
schemas or starts monitor/GitHub/deployment workers. `sky-service` skips its local
state-volume requirement in this mode. `--initialize-state` cannot be combined.
Existing local mode and its array response remain unchanged.

PostgresStateSettings reads SKY_DATABASE_HOST/PORT/NAME/SECRET_ARN, SKY_AWS_REGION,
and SKY_DATABASE_SSLROOTCERT. TLS verification and rotating Secrets Manager
credentials reuse PR #7. The CA bundle and previously initialized schema are required.
SKY_STATE_WORKSPACE defaults to team. Reading records does not migrate legacy owners.

The CLI currently permits loopback only because auth remains LocalTokenAuthenticator
(local_operator, local_workspace, admin). This mode reads records owned by that
organization; it does not impersonate production organizations. It is a local integration
preview, not the hosted Fargate API. A composition root may inject a verified identity
provider into DatabaseReadApp; no ALB JWT verifier or organization membership lookup
is implemented in this PR. Do not expose this token-based mode over the public ALB.

## HTTP contract

Authenticated GET routes:
- /api/config: read_only=true and no deployment targets/background intervals.
- /api/jobs?limit=50&cursor=TOKEN: {items: [...], next_cursor: string|null}.
- /api/applications/{application_id}/releases: the same page envelope.
- /api/jobs/{job_id}: the existing detail fields, diagnosis and persisted health history.
- /api/jobs/{job_id}/history: persisted history only, no active probe.
- /api/jobs/{job_id}/certificate: certificate from the same detail snapshot.

Job IDs remain 16 lowercase hexadecimal characters. Each page has max 100 rows.
Cursors are bounded base64 JSON, scoped to workspace, organization and application.
They are not authentication tokens and are deliberately not signed by a process-local
secret so independent API instances can accept them. Authorization is reapplied on
every query; callers may alter page positions but cannot bypass ownership checks.
Invalid/duplicate/unknown query fields return 400. Missing and foreign jobs return the
same 404. Database failure returns a redacted 503; corrupt owned records return 500.
There is no fallback to local memory. /health is liveness only and does not certify DB readiness.

Authenticated POST/PUT/PATCH/DELETE return 405. The legacy GET health probe, AWS
lookup/operation/group routes and GitHub routes are absent (404), so reads cannot
accidentally trigger probes or bypass the DB ownership boundary.

## Browser behavior and remaining work

The existing page accepts both legacy array lists and DB page envelopes. More buttons
append jobs/releases. Failed pages retain visible rows/cursor for retry; stale release
responses are ignored after app selection changes. Read-only mode hides mutation
controls and shows that health results are persisted observations. Metrics count only
currently displayed records, not all DB records. Automatic refresh does not reset DB
pages; explicit refresh reloads the first page and selected detail refreshes periodically.

Hosted authentication, DB readiness, shared writes/CAS integration, monitor-error
persistence, S3 context storage and distributed workers remain separate work. Do not
use this read-mode API alongside the legacy write API as one production service.
Depends on PR #9, transitively #6/#7/#8; no dependency PR is modified or merged here.
