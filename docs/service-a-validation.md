# A-phase validation — 2026-10-09

Initial baseline: `27ffe75712d292539b4fdd6879af7215438e4b51` plus branch
`feat/sky-service-container-ci` changes. The initial checks below happened before
GitHub publication; PR preparation results are recorded separately.

## Environment

- VM: `kt_proj_ro1`, hostname `kt-proj-rocky`, Rocky Linux 8.10 x86_64.
- Workspace: `/root/sky-service-work/sky-platform` (separate from existing projects).
- Docker Engine 26.1.3 and Compose 2.27.0 installed and Docker enabled.
- Existing containerd 1.6.32 was retained; kubelet/containerd remained active.
- Runtime Python 3.12 is provided by the image; host Python was not upgraded.
- Final service image ID: `sha256:15d18373a959e4d05ecceea8c26b97f2f88989cb0a265130c361c4e8d154d421`.
- AWS CLI 2.37.10 runs inside the service image.
- All three base image manifests are pinned in Dockerfile to the validated digests.

## Passed

| Check | Result |
| --- | --- |
| Service and Linux test image builds | Passed |
| Python default suite, excluding live tests | 541 passed, 51.56 seconds |
| Existing JavaScript suites, Windows Node | 27 passed |
| Ruff lint/format, workflow YAML, Linux shell syntax | Passed |
| Service HTTP/UI and `/health` | Passed |
| API without session token | Rejected with 403 |
| Runtime AWS/Docker/Compose tools | Passed |
| Service container: ZIP -> static analysis -> host Docker build -> local deployment -> HTTP | Passed |
| Service restart -> persisted deployment record recovery | Passed |

The ZIP service smoke uses the hello-node fixture and no OpenAI/AWS credentials.
It exercises a real containerized Sky HTTP service and host Docker, not a real
model decision or cloud deployment.

## Separate game validation

The reviewed `demo-game/TUG-Sky-almostfinaltest.zip` was built and run in an
isolated container with its own named volume. It received no host Docker socket,
host directory mounts, or host networking.

- HTTP health/scoreboard passed.
- App-internal `sky.probe` nonce exchange passed, before and after restart.
- SQLite records went from 13 to 14 and remained 14 after restart.
- Randomly published host port changed on Docker restart in this VM; the probe
  refreshed the actual binding. Do not use a remembered random port as a stable
  external endpoint.

This game check does **not** validate Sky intake, real OpenAI, AWS deployment,
ALB/WSS ingress, browser multiplayer, or the Compose adapter end to end. The
existing full game test, with host Docker access, was rejected by automatic
approval review; the isolated check is a narrower alternative.

## Still needed

- Run the amended workflow in GitHub Actions after review/push.
- Configure ECR and GitHub `dev` environment/OIDC variables documented in
  [service-a.md](service-a.md); real ECR publication has not run.
- Receive ECS cluster/service/task/container names, host network/task role,
  shared temporary path and EBS mount settings from sky-infra.
- Implement/validate ECS update and rollback with desired count 1 and
  stop-before-start configuration; no ECS/CD operation ran here.
- Validate real-model game deployment and external HTTP/WebSocket access in AWS.

No AWS resources were created and no keys were copied into the source/image.
Smoke containers and their named resources were removed; service/test images and
build cache remain available for continued development.

## PR preparation after integrating main

Integrated upstream `8a7f1d7` (five new deployment/recovery commits) without
conflicts. Rechecked the resulting code on the same Linux VM:

- Python default suite: **554 passed**, 66.54 seconds.
- JavaScript suites: **27 passed** (22 UI, 2 migrator, 3 restore verifier).
- Ruff lint/format and workflow YAML parsing passed.
- Linux service/test image builds passed.
- Restricted service container: HTTP/UI, public health, protected API and runtime
  CLI checks passed. It had no host Docker socket, host networking, or host path
  mounts; its port was published only on loopback. The container was removed.

The full host-Docker ZIP smoke was **not rerun after integrating main**: automatic
approval review rejected the broad host-control surface. The narrower HTTP check
above does not replace that end-to-end check. The earlier full ZIP result belongs
to the initial baseline; GitHub Actions must still verify the updated workflow.
Real OpenAI, ECR publication, ECS rollout/rollback and external game access remain
unverified.

## Review follow-up

Integrated upstream `2b34205`, including deployment policy, versioned evidence
and architecture-decision changes requested by the review. The service image now
uses `sky-service` and requires a persistent-state marker before opening HTTP.
Initialization is a one-off operation for an existing empty mounted directory;
it cannot overwrite existing state or start the server.

The new persistent-state regression tests passed **18/18** in the Linux test
container. They cover missing state, restart with preserved records, rejection of
reinitialization, corrupt/oversized markers, unknown versions, symlinks and FIFOs.
The smoke additionally tests missing mounts and uninitialized state, explicit
initialization, rejected repeated initialization and the normal deploy/restart
flow. Latest complete CI results are linked from the PR description/checks.

The access-boundary section is an infrastructure prerequisite, not proof that AWS
has been configured: internal HTTPS ALB/private access (or front-end authentication)
and workload-bridge isolation from the host API and credential endpoints must be
configured and tested by sky-infra before same-host Local deployment is enabled.
