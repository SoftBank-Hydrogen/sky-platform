# B runtime entry points and service image

This stage prepares the **Sky control-plane image**, not an uploaded application's image.
It provides a read-only PostgreSQL API, an explicitly selected outbox publisher and a one-shot schema migration command.
It does not provide ZIP upload admission, SQS deployment consumption, remote builds or application deployment.

## API

`sky-service api --auth-mode alb --alb-trusts-file /config/alb-trusts.json --memberships-file /config/memberships.json`

The API binds to 0.0.0.0:8080 by default, requires verified ALB identity configuration, and bypasses the legacy local state directory and its lock. Existing ALB trust and membership file formats still apply. For an isolated local check, use `api --host 127.0.0.1 --auth-mode local`.

### ALB identity from environment variables

On Fargate, the same two documents can be supplied without a file mount:

`sky-service api --auth-mode alb` with `SKY_ALB_TRUSTS_JSON` and `SKY_MEMBERSHIPS_JSON`

Each variable holds the full JSON document that the corresponding file would contain. The JSON is validated with exactly the same rules as the files: version, exact fields, duplicate keys, issuer/trust consistency, `user_id`/`organization_id` format, role values and the 1 MiB limit. Invalid documents stop startup with exit code 2. Error output never includes the documents.

The two sources cannot be mixed:

- Both variables are required when either is set.
- Any `--alb-trusts-file`/`--memberships-file` option together with either variable is rejected. Either all identity configuration comes from files or all of it comes from the environment.
- `--auth-mode local` rejects these variables just as it rejects the file options.

The file permission check (not group/world writable) applies only to files. Protect the environment source instead. With ECS, inject the values from Secrets Manager through the container definition's `secrets`. They are then not stored in plain task definition `environment`:

```json
"secrets": [
  {"name": "SKY_ALB_TRUSTS_JSON", "valueFrom": "arn:aws:secretsmanager:ap-northeast-2:<account>:secret:sky/dev/alb-trusts"},
  {"name": "SKY_MEMBERSHIPS_JSON", "valueFrom": "arn:aws:secretsmanager:ap-northeast-2:<account>:secret:sky/dev/memberships"}
]
```

The task **execution** role needs `secretsmanager:GetSecretValue` on these secrets (plus `kms:Decrypt` for a customer-managed key). ECS reads the values only when a task starts. After a membership change, start new tasks, for example with `aws ecs update-service --force-new-deployment`.

This is read-only: `/api/config` reports read_only and mutation requests are rejected. It does not start legacy deployment, monitoring or GitHub polling threads. `/health` is process liveness; `/ready` executes read-only checks of the DB connection, metadata schema versions and query columns. It returns 503 if unavailable without exposing diagnostics. Configure load-balancer readiness against `/ready` for this mode. Liveness remains `/health` so a DB outage does not require a process restart.

Supply SKY_DATABASE_HOST, SKY_DATABASE_PORT, SKY_DATABASE_NAME, SKY_DATABASE_SECRET_ARN and SKY_AWS_REGION; optionally SKY_STATE_WORKSPACE. Credentials are fetched from Secrets Manager through the task role; TLS verify-full is retained. The image installs state-postgres dependencies and the packaged RDS CA at `/etc/ssl/certs/sky-rds-global-bundle.pem`. SKY_DATABASE_SSLROOTCERT can override that path.

Runtime never initializes or migrates the database. Run `sky-service migrate` (below) before readiness can pass, and coordinate database access with the infrastructure owner.

## Migration

`sky-service migrate`

This one-shot command applies pending PostgreSQL state migrations and exits. It reads the same configuration as the API: SKY_DATABASE_HOST, SKY_DATABASE_PORT, SKY_DATABASE_NAME, SKY_DATABASE_SECRET_ARN, SKY_AWS_REGION, SKY_AWS_ACCOUNT_ID, optional SKY_STATE_WORKSPACE and optional SKY_DATABASE_SSLROOTCERT. Credentials, TLS verify-full and timeouts are also the same as the API. SKY_AWS_ACCOUNT_ID (12 digits) is validated by the admission store before any connection; the migrations themselves do not record it. It does not need a state directory, ALB identity or SQS settings.

The command applies all five ledgers in order: metadata, operation, admission, approval, preview. These are the existing `initialize()` code paths of the record, operation, admission, approval and preview stores, so afterwards both the read-only API and the opt-in preparation API (`--enable-preparation`) pass readiness. Each initializer also calls its predecessors first; those find their ledger current and change nothing. Each ledger step runs in its own transaction under the existing `pg_advisory_xact_lock`, so concurrent runs serialize and a failed step rolls back. Already applied versions are skipped, so the command is safe to repeat. After a failure, a rerun continues from the first incomplete ledger. A successful run prints one line per ledger:

```
metadata schema: applied 1,2 (now 1,2)
operation schema: applied 1,2 (now 1,2)
admission schema: applied 1 (now 1)
approval schema: applied 1 (now 1)
preview schema: applied 1 (now 1)
```

A database migrated by the earlier metadata/operation-only command reports those two as `up to date` and applies the other three.

Existing data: migrate only adds schemas and tables; it never changes or deletes existing rows. `application_owners` is created empty and is not populated from existing deployment records, so apps recorded before this migration still need an explicit ownership import before they can be prepared.

Exit codes:

- `0`: the schema is current.
- `2`: invalid configuration. No database connection is attempted.
- `1`: credential, connection or migration failure, or a database with a newer/unsupported schema version. The failing ledger is left unchanged; ledgers before it may already have been applied by this run.

Failure output is a fixed message with no query text, DDL, endpoint diagnostics or secrets.

The database user behind SKY_DATABASE_SECRET_ARN must be able to create the `sky_state` schema and its tables (`CREATE` on the database, or ownership of an existing `sky_state` schema). If the API is later moved to a read-only database user, run migrations with a separate task definition whose secret has DDL rights.

### Running on ECS as a one-off task

Reuse the API task definition, with the same image, network, task role, environment and secrets, and override only the container command. The image `ENTRYPOINT` is `sky-service`, so the command `["migrate"]` runs `sky-service migrate`. `run-task` does not register the task with the load balancer.

```bash
TASK_ARN=$(aws ecs run-task \
  --cluster <cluster> \
  --task-definition <api-task-definition-family>[:<revision>] \
  --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[<private-subnet>],securityGroups=[<api-security-group>],assignPublicIp=DISABLED}' \
  --overrides '{"containerOverrides":[{"name":"<api-container-name>","command":["migrate"]}]}' \
  --started-by sky-migrate \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster <cluster> --tasks "$TASK_ARN"
aws ecs describe-tasks --cluster <cluster> --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].[exitCode,reason]'
```

Use the same subnets and security groups as the API service so the task can reach RDS and Secrets Manager. Confirm exit code `0` and the result lines in the container's CloudWatch log stream. Then `/ready` on the API passes without restarting the API. Run the migration before deploying an API revision that requires a newer schema.

## Outbox worker

`sky-service worker --mode outbox`

This process publishes previously admitted DB outbox entries to SQS FIFO. It is **not** the consumer that builds or deploys apps. A bare `worker` exits with a configuration error instead of pretending deployment support exists. The infrastructure's current default `["worker"]` must therefore not be considered a working deployment worker configuration.

Also supply SKY_AWS_ACCOUNT_ID and SKY_JOB_QUEUE_URL. Queue URL must match the configured account/region and be FIFO. `--check-config` validates configuration with no DB/AWS calls; `--once` publishes at most one entry and exits. The default loop polls every 5 seconds (`--interval` accepts 1–300). An instance UUID identifies each publisher; database claims support multiple publishers.

Startup checks existing metadata/operation schema versions and outbox query columns without DDL. Publishing uses the existing claim/send/confirm rules and bounded retry policy. Missing schema or infrastructure errors exit nonzero without diagnostic secrets. SIGTERM/SIGINT stops new claims after the current single-entry dispatch; the existing bounded client calls complete or leave claims recoverable. This is cooperative shutdown, not a promise that every remote call completes within an ECS grace period. No automatic execution-lease recovery or reconciliation is activated here.

The outbox process requires its own least-privilege DB writes and SQS SendMessage permission. The current deployment worker task role may only have receive permissions: verify before enabling. Do not blindly replace the intended deployment worker with a publisher. Choose a separate publisher task or integrate it in the later API/worker composition. No live outbox publishing was performed during development.

## Image and rollout

The existing default A-mode command and offline ZIP deployment smoke remain supported. The shared image still includes Docker/Compose/AWS CLI and retains its A-mode root execution; removal and non-root B packaging follow after remote build/deployment integration. No infrastructure defaults or running tasks are changed by this PR. The newly built image needs an explicit API command plus auth configuration to enable B reads.

Local unit tests cover routing, rejected public local auth, wrong-account queues, config-only validation, single-entry dispatch and shutdown. Further unit tests cover redacted migrate failures and environment identity sources: parity with the file validation, conflicts and partial configuration. HTTP/DB contract tests use disposable loopback PostgreSQL for readiness and missing-schema failures. They also run `migrate` on a fresh empty database: first application, repeat runs, concurrent runs and refusal of an unsupported newer schema. Image smoke checks installed libraries, CA parsing and CLI help, then runs a non-root API with a read-only filesystem and no state volume, external network or AWS credentials. It verifies liveness 200, DB readiness 503 and mutation rejection. The existing A-mode ZIP deployment and restart smoke remains in place.
