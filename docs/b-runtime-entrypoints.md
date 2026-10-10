# B runtime entry points and service image

This stage prepares the **Sky control-plane image**, not an uploaded application's image.
It provides a read-only PostgreSQL API and an explicitly selected outbox publisher.
It does not provide ZIP upload admission, SQS deployment consumption, remote builds or application deployment.

## API

`sky-service api --auth-mode alb --alb-trusts-file /config/alb-trusts.json --memberships-file /config/memberships.json`

The API binds to 0.0.0.0:8080 by default, requires verified ALB identity configuration, and bypasses the legacy local state directory and its lock. Existing ALB trust and membership file formats still apply. For an isolated local check, use `api --host 127.0.0.1 --auth-mode local`.

This is read-only: `/api/config` reports read_only and mutation requests are rejected. It does not start legacy deployment, monitoring or GitHub polling threads. `/health` is process liveness; `/ready` executes read-only checks of the DB connection, metadata schema versions and query columns. It returns 503 if unavailable without exposing diagnostics. Configure load-balancer readiness against `/ready` for this mode. Liveness remains `/health` so a DB outage does not require a process restart.

Supply SKY_DATABASE_HOST, SKY_DATABASE_PORT, SKY_DATABASE_NAME, SKY_DATABASE_SECRET_ARN and SKY_AWS_REGION; optionally SKY_STATE_WORKSPACE. Credentials are fetched from Secrets Manager through the task role; TLS verify-full is retained. The image installs state-postgres dependencies and the packaged RDS CA at `/etc/ssl/certs/sky-rds-global-bundle.pem`. SKY_DATABASE_SSLROOTCERT can override that path.

Runtime never initializes or migrates the database. Coordinate migrations and database access with the infrastructure owner before readiness can pass.

## Outbox worker

`sky-service worker --mode outbox`

This process publishes previously admitted DB outbox entries to SQS FIFO. It is **not** the consumer that builds or deploys apps. A bare `worker` exits with a configuration error instead of pretending deployment support exists. The infrastructure's current default `["worker"]` must therefore not be considered a working deployment worker configuration.

Also supply SKY_AWS_ACCOUNT_ID and SKY_JOB_QUEUE_URL. Queue URL must match the configured account/region and be FIFO. `--check-config` validates configuration with no DB/AWS calls; `--once` publishes at most one entry and exits. The default loop polls every 5 seconds (`--interval` accepts 1–300). An instance UUID identifies each publisher; database claims support multiple publishers.

Startup checks existing metadata/operation schema versions and outbox query columns without DDL. Publishing uses the existing claim/send/confirm rules and bounded retry policy. Missing schema or infrastructure errors exit nonzero without diagnostic secrets. SIGTERM/SIGINT stops new claims after the current single-entry dispatch; the existing bounded client calls complete or leave claims recoverable. This is cooperative shutdown, not a promise that every remote call completes within an ECS grace period. No automatic execution-lease recovery or reconciliation is activated here.

The outbox process requires its own least-privilege DB writes and SQS SendMessage permission. The current deployment worker task role may only have receive permissions: verify before enabling. Do not blindly replace the intended deployment worker with a publisher. Choose a separate publisher task or integrate it in the later API/worker composition. No live outbox publishing was performed during development.

## Image and rollout

The existing default A-mode command and offline ZIP deployment smoke remain supported. The shared image still includes Docker/Compose/AWS CLI and retains its A-mode root execution; removal and non-root B packaging follow after remote build/deployment integration. No infrastructure defaults or running tasks are changed by this PR. The newly built image needs an explicit API command plus auth configuration to enable B reads.

Local unit tests cover routing, rejected public local auth, wrong-account queues, config-only validation, single-entry dispatch and shutdown. HTTP/DB contract tests use disposable loopback PostgreSQL for readiness and missing-schema failures. Image smoke checks installed libraries, CA parsing and CLI help, then runs a non-root API with a read-only filesystem and no state volume, external network or AWS credentials. It verifies liveness 200, DB readiness 503 and mutation rejection. The existing A-mode ZIP deployment and restart smoke remains in place.
