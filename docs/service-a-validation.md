# A-phase validation — 2026-10-09

Baseline: `27ffe75712d292539b4fdd6879af7215438e4b51` plus local branch
`feat/sky-service-container-ci` changes. Nothing has been pushed to GitHub.

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
