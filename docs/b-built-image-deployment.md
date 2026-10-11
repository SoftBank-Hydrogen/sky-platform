# B worker: verified image deployment

`worker --mode build --deploy-built-image` opts in to an initial ECS Express deployment after a verified remote build. Without the flag the existing build-only behavior is unchanged. Outbox mode rejects this flag.

The worker rechecks the consumed approval and job binding, ECR digest, AWS account and managed `sky-core` outputs. It reads the existing core stack; it does not apply CloudFormation or use Docker/AWS CLI. This initial path supports one replica and an HTTP health path without user environment bindings. Preparation continues to block unsupported infrastructure and SQLite conversion requests. A previous successful deployment for the same application requires the future update executor; this path does not silently create a replacement.

Before create, the worker records an immutable AWS intent under its live DB lease, while refreshing the lease, SQS visibility and task protection. ECS Express does not expose a create clientToken, so create uses one SDK attempt. An uncertain response, timeout or ownership loss never causes an automatic create retry. The operation and app lock remain for reconciliation. Do not manually requeue a pending AWS intent.

Success requires owned service tags, the approved container configuration, matching task-definition image digest and public HTTPS HTTP 200. Job result, operation success and app lock release commit atomically. A crash after verified AWS observation can resume DB completion without another create. If this fails, no deployment success is synthesized.

No schema migration is added. Existing parked build_ready operations are not automatically resumed, since they are needs_attention and hold application locks. Activation affects new admitted jobs only; operators must reconcile older jobs separately. WebSocket/gameplay verification, update/rollback and retirement integration remain follow-up work.

Activation requires a reviewed service image and adding `--deploy-built-image` to the infra worker command. The image-only release does not change this command. Validate a dedicated app before enabling the production path; do not bypass preparation/approval or inject operational DB rows.

AWS API: https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_CreateExpressGatewayService.html
