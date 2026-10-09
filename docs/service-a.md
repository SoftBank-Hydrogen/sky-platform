# Sky service: A phase (ECS on EC2)

This is the Sky control-plane image, not a user application's image. User images
continue to use `sky-managed`; this service uses a separate `sky-platform` ECR.

## Runtime contract for sky-infra

| Setting | A-phase requirement |
| --- | --- |
| Platform | Linux x86_64, ECS on EC2, one Sky process |
| Command | `sky-platform --host 0.0.0.0 --port 8080 --state-dir /.sky` |
| ALB target | HTTP 8080, health check `GET /health` (liveness only) |
| Docker | Bind mount `/var/run/docker.sock` at the same path |
| Network | Host network for same-host loopback rehearsal; confirm ECS task-role endpoint access in EC2 agent configuration |
| State | EBS-backed host path `/.sky` mounted at the same container path `/.sky`; preserve across task updates |
| Temporary files | Dedicated host directory mounted at exactly the same absolute container path; set `TMPDIR` to it |
| Tools | Python 3.12, Docker CLI + Compose, AWS CLI v2, Git and DNS tools are in this image |
| AWS | Task role for user-app resources; task execution role for service image/secrets/logs |
| Credentials | Inject `OPENAI_API_KEY` via Secrets Manager. Use task credentials, not keys baked into the image |
| Configuration | `SKY_AWS_REGION`, `SKY_AWS_ACCOUNT_ID`; optional `SKY_AI_MODEL`, `SKY_AWS_SERVICE_SECURITY_GROUP`, `SKY_MAX_RDS_730H_USD` |
| GCP | Not packaged in A-phase image; requires a separate gcloud/credential setup before enabling |

The same-path temporary mount matters: Docker interprets bind mounts on the EC2
host, not inside Sky. Compose files and private rehearsal env files must be visible
at those paths on the host. Set `TMPDIR` before starting Python.

The Docker socket grants control over the dedicated EC2 host. A phase is for a
bounded internal/demo environment. A public multi-tenant build service requires
isolation beyond this arrangement; keep B phase as separate work.

## Deployment behavior

Desired count is **1**. The current state-directory lock forbids two live Sky
processes on the same directory. Use ECS deployment configuration
`minimumHealthyPercent=0`, `maximumPercent=100` and a deployment circuit breaker
with rollback. This is **stop-before-start**, with a brief outage, not a zero-downtime
rolling deployment. Avoid updating while user-app deployment jobs are active.
EC2/EBS replacement recovery is an infra runbook requirement; a host bind mount
does not by itself attach the old EBS volume to a replacement instance.

Allow service port 8080 from the ALB security group only. Enable task-role access
for host-mode tasks through the EC2 agent settings and validate credential access
without exposing credentials. ALB health does not certify Docker/AWS/OpenAI or
any deployed application's availability.

## CI and ECR handoff

PR/main runs retain the existing Python/JS checks, then build `linux/amd64` and
run an offline ZIP deployment smoke. The smoke also checks API authentication,
installed tools, and persisted job recovery after service restart. It uses no real
OpenAI or AWS credentials. It does not prove a real-model game deployment.

On main, the exact verified image is transferred to an OIDC publishing job.
Publishing is enabled only when the infra owner configures:

- `AWS_SERVICE_CI_ROLE_ARN`: service publishing role trusted for this repository's
  `dev` environment. Match the repository's actual OIDC `sub`: newer repositories
  can include immutable owner/repository IDs. Do not copy a name-only example
  without checking the current subject format.
- `AWS_REGION`, `AWS_ACCOUNT_ID`: final destination, checked with STS before push.
- `SKY_SERVICE_ECR_REPOSITORY`: existing service repository, e.g. `sky-platform`.
- GitHub `dev` environment protection/approval as agreed by the team.

Set the role ARN and repository name as repository-level Actions variables: the
job-level condition is evaluated before environment variables are available.
The role needs ECR authorization, upload, and `ecr:DescribeRepositories`
permissions for the service repository. It is separate from Terraform plan/apply
roles and from the runtime task role used by Sky to deploy user applications.
The workflow does not create a repository or change ECS. Tags are full commit
SHAs; the ECR repository should enforce immutable tags. A rerun of an already
published SHA needs an agreed handling policy instead of silently overwriting it.

CD is the next connection: register a task revision using the published image,
update the agreed ECS service, wait for stabilization, and check the ALB URL.
Cluster/service/task/container names, roles, EBS mounts, and network settings
must come from sky-infra; no placeholder AWS resources are created here.

## Validation commands

```sh
docker build --target test -t sky-platform:test .
docker run --rm sky-platform:test
docker build --target service -t sky-platform:smoke .
# Portable HTTP/UI/tools smoke:
python scripts/smoke_service_container.py
# Linux Docker host: full offline build/deploy/recovery:
python scripts/smoke_service_container.py --full
```

The full smoke binds the host Docker socket and uses host networking. It removes
only the containers/images it created. Never run against a production Sky state
directory. GCP, real AWS deployment, ECS rollout/rollback, and actual OpenAI calls
require separate live verification after infrastructure is ready.
