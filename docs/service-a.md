# Sky service: A phase (ECS on EC2)

This is the Sky control-plane image, not a user application's image. User images
continue to use `sky-managed`; this service uses a separate `sky-platform` ECR.

## Runtime contract for sky-infra

| Setting | A-phase requirement |
| --- | --- |
| Platform | Linux x86_64, ECS on EC2, one Sky process |
| Command | `sky-service --host 0.0.0.0 --port 8080 --state-dir /.sky` (image entry point; persistent state guard required) |
| ALB target | HTTP 8080, health check `GET /health` (liveness only) |
| Docker | Bind mount `/var/run/docker.sock` at the same path |
| Network | Host network for same-host loopback rehearsal; confirm ECS task-role endpoint access in EC2 agent configuration |
| State | Initialized EBS-backed host path `/.sky` mounted at container path `/.sky`; missing/invalid state marker blocks startup; preserve across task updates |
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

## Required access boundary before ECS deployment

The HTML page contains the API token. Anyone who can load it can invoke deployment
and database-management APIs; the token is not user authentication. Do not expose
the unauthenticated Sky control plane through a public ALB or tunnel.

Use an **internal ALB with an HTTPS listener** and restrict its clients to the
team's private/VPN access boundary. Permit host port 8080 only from the ALB security
group. If public control-plane access is required later, put ALB
`authenticate-oidc` / Cognito (or equivalent authentication) in front of **both**
the page and API routes over HTTPS, and prevent direct access to the target.
The public demo-game URL is a separate workload endpoint.

Security groups alone do not isolate containers on the same EC2 host. Before
enabling Local deployment/rehearsal in A phase, sky-infra must configure and verify:

- EC2 metadata: `HttpTokens=required` (IMDSv2 only) and
  `HttpPutResponseHopLimit=1`. Keep IMDS IPv6 disabled, or protect it as well.
- Host firewall: deny untrusted traffic from `docker0` **and every custom workload
  bridge** to host TCP 8080 and IMDS `169.254.169.254` (IPv6 `fd00:ec2::254` if
  enabled). Cover the appropriate host-input/forward paths and bridge gateways.
- Also deny workload bridges access to ECS task credential endpoints such as
  `169.254.170.2`; preserve the host-mode Sky task's own credential access.
- User workloads must not receive host networking, privileged mode, the Docker
  socket, or Sky's state/temporary directories.

Test denial from an actual workload container as well as permitted HTTPS access
from an operator. Persist firewall rules across reboot and bridge creation. A hop
limit of 1 is an additional restriction, not a replacement for the firewall.
Do not enable same-host Local workloads until these checks pass. This PR documents
the deployment prerequisite; it does not provision the ALB, authentication,
metadata settings or host firewall.

References: [ALB authentication](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html),
[EC2 metadata options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html).

## Persistent state initialization and recovery

The service image uses `sky-service`, which checks `/.sky/.sky-service-state.json`
before starting the HTTP server. A missing directory, missing/corrupt marker or
symlink fails startup. It does not silently create or repair persistent state.
The local developer command `sky-platform` retains its previous behavior; do not
override the ECS image entry point to bypass the service guard.

For the **first installation only**, verify that the intended EBS volume is mounted,
create an empty state subdirectory on that volume, and run a one-off initialization
task using the same state mount. Initialization exits without opening the server:

```sh
docker run --rm --network none \
  --mount type=bind,source=/.sky,target=/.sky \
  sky-platform:SERVICE_SHA --initialize-state --state-dir /.sky
```

In ECS, override the one-off task command with
`["--initialize-state", "--state-dir", "/.sky"]`; do not add initialization to the
long-running service command or restart automation. Initialization rejects a
nonempty directory, including an already initialized one, and never overwrites
job/ownership records. Use a state subdirectory rather than a filesystem root
containing `lost+found`.

On replacement/recovery, reattach the original EBS volume or restore its complete
backup, including the marker, then use the normal service command. Do not initialize
a new empty directory to get past a missing-volume error. The marker detects
missing/uninitialized state; it does not prove EBS identity or restore lost data.
The infrastructure runbook must still verify the intended EBS mount and volume.

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
