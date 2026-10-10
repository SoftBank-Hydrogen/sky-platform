# Sky

> 앱을 올리면, 코드에서 근거를 찾아 어디서 어떻게 돌릴지 정하고, 배포한 뒤 실제로 동작하는지 증명합니다.

SoftBank Hackathon 2026 **One Action, Infinite Clouds** 팀 SoftBank-Hydrogen의 배포 플랫폼입니다.
로컬 웹 앱 ZIP·폴더·GitHub 저장소를 받아 로컬, 온프레미스, AWS, GCP에 배포합니다.

| 저장소 | 역할 |
| --- | --- |
| **sky-platform** (여기) | 분석, 판단, 코드 변환, 배포 어댑터, API, 워커, UI |
| [sky-infra](https://github.com/SoftBank-Hydrogen/sky-infra) | Sky를 호스팅하는 AWS 인프라 (Terraform), 온프레미스 VM 준비 (Ansible) |
| [demo-game](https://github.com/SoftBank-Hydrogen/demo-game) | 데모 앱: 실시간 줄다리기 게임, 팀 게시판 |

## 무엇을 하나

```
입력 (ZIP · 폴더 · GitHub)
  → 증거 수집   런타임·포트·DB·파일 쓰기·메모리 상태·WebSocket을 규칙과 AST로 찾고, AI가 실행 설정을 정한다
  → 판단        대상별 역량과 대조해 후보를 거르고(eligible / rejected / needs_review), 정책에 따라 고른다
  → 컴파일      코드 변경 계획과 인프라 계획을 같은 결정에서 만들고, 둘이 맞는지 검사한다
  → 빌드·리허설  같은 이미지를 로컬에서 먼저 띄워 확인한다
  → 배포        대상 어댑터가 Sky 소유 리소스만 만든다
  → 검증        HTTP, WebSocket, DB를 실제로 확인한다. 확인 못 한 것은 미검증으로 남긴다
  → 증거 보고서  왜 이 대상인지, 무엇을 바꿨는지, 무엇을 검증했는지 기록한다
```

원칙:

- **근거가 있을 때만 판단합니다.** 모든 판단에 `confirmed` / `inferred` / `unknown` 상태와 근거 파일이 붙습니다.
- **AI가 "완료"라고 해도 성공이 아닙니다.** 실제 응답을 확인해야 성공입니다.
- **AI는 정해진 도구 안에서만 움직입니다.** 셸이나 클라우드 CLI를 직접 쓰지 않고, 인용한 근거가 소스에 그대로 있는지 검사받습니다.
- **Sky는 앱의 요청 경로에 없습니다.** Sky가 멈춰도 배포된 앱은 계속 동작합니다.

## 배포 대상

| 대상 | 실행 모델 | 상태 |
| --- | --- | --- |
| Local Docker | 같은 PC 컨테이너 | 실검증 |
| AWS ECS Express | Fargate + 관리형 ALB + HTTPS 주소 | 실검증 |
| AWS S3 + CloudFront | 정적 호스팅 (자동 선택 시 정적 앱 우선) | 실검증 |
| AWS RDS PostgreSQL | 앱 전용 DB, SQLite → PostgreSQL 코드 변환과 데이터 이전 | 실검증 (게임 데모) |
| 온프레미스 Compose | 같은 PC Compose, SQLite 볼륨 | 구현 |
| 온프레미스 원격 VM | SSH로 Linux VM의 Docker에 배포 ([준비 방법](https://github.com/SoftBank-Hydrogen/sky-infra)) | 로컬 VM에서 실검증 |
| GCP Cloud Run | 서버리스 컨테이너, Cloud SQL 연결 | 구현 |
| AWS Lambda, EC2 | 소스 근거로 구조 적합성 판정, 명시 선택 배포 | PR 진행 중 |

**실검증**은 실제 계정·실제 모델로 확인한 것, **구현**은 테스트는 통과했지만 실계정 전체 경로는 아직 확인하지 않은 것입니다.

자동 선택은 정적 앱이면 S3 + CloudFront, 아니면 적격 후보(Local Docker, ECS Express, Cloud Run) 중에서 고릅니다.
DB가 필요한 앱, WebSocket 앱, 온프레미스는 자동 선택하지 않고 사용자 확인을 거칩니다.

## 빠른 시작 (로컬)

Python 3.12 이상, Docker가 필요합니다. AI 배포에는 OpenAI API 키가 필요합니다.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

export OPENAI_API_KEY=...          # 또는 chmod 600인 .env 파일
.venv/bin/sky-platform             # http://127.0.0.1:8080
```

브라우저에서 앱 폴더나 ZIP을 올리고 대상을 고른 뒤 **배포하기**를 누릅니다.

| 대상 | 서버 환경변수 |
| --- | --- |
| AWS | `SKY_AWS_ACCOUNT_ID` (실제 로그인 계정과 다르면 중단), `SKY_AWS_REGION`, AWS CLI 자격 증명 |
| GCP | `SKY_GCP_PROJECT`, `SKY_GCP_REGION`, `gcloud` |
| 원격 VM | `SKY_VM_SSH_HOST`, `SKY_VM_SSH_USER`, `SKY_VM_PUBLIC_HOST` ([docs/onprem-vm.md](docs/onprem-vm.md)) |

주요 옵션: `--port`, `--state-dir`, `--monitor-interval`, `--github-poll-interval` (`sky-platform --help`).

## 호스팅 모드 (B안)

여러 조직이 쓰는 클라우드 운영형 Sky입니다. 인프라는 sky-infra의 Terraform이 만듭니다.

```
ALB + Cognito → API (Fargate) → PostgreSQL 상태 DB
                  └ ZIP 업로드 → S3 원본 → 미리보기 → 저장된 승인 → 작업 접수 + outbox (한 트랜잭션)
outbox 발행기 → SQS FIFO → 워커 → 격리된 원격 빌더 → ECR 다이제스트 검증 → ECS 배포
```

- 사용자 코드는 API나 워커에서 실행하지 않습니다. 빌드는 자격 증명 없는 별도 러너에서 합니다.
- 모든 요청에서 조직 소유권을 확인합니다.
- 결과가 불확실한 외부 작업은 자동으로 다시 실행하지 않고 재조사합니다.

```sh
sky-service api --auth-mode alb ...   # docs/b-runtime-entrypoints.md
sky-service worker ...
sky-service migrate
```

구성 요소별 설계: [docs/](docs/) 의 `b-*.md`.

## 코드 구조

```
src/
  domain/        모델, 접근 권한
  engine/        순수 판단 로직: 증거, Application IR, 호환성, 후보, 컴파일, 정적 판정
  application/   유스케이스: 분석, AI 에이전트, 배포, DB 이전, 롤백, 모니터링, 증명서
  adapters/      외부 연결: aws, gcp, local, onprem, build, database, state, ai, github
  interfaces/    HTTP 서버, CLI, 호스팅 API·워커 진입점
  assets/        UI(static/index.html), 인프라 템플릿(CloudFormation), DB 마이그레이터
tests/
  unit/ contract/ regression/   CI에서 실행
  integration/                  로컬 Docker 등에서 재현
  live/                         실계정·실모델 검증 (수동, CI 제외)
```

## 개발

```sh
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI(GitHub Actions)는 PostgreSQL을 붙인 전체 테스트, JavaScript UI 테스트, 서비스 이미지 빌드, 오프라인 ZIP 배포 확인을 실행합니다.
main에 머지되면 검증된 서비스 이미지를 ECR에 게시합니다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/onprem-vm.md](docs/onprem-vm.md) | 원격 Linux VM 대상 |
| [docs/rollback-rehearsal.md](docs/rollback-rehearsal.md) | 롤백·복귀 리허설 |
| [docs/service-monitoring.md](docs/service-monitoring.md), [docs/monitoring-integration.md](docs/monitoring-integration.md) | 모니터링, Slack 알림, k6 |
| [docs/infra-image-pr.md](docs/infra-image-pr.md) | 서비스 이미지 → 인프라 배포 연결 |
| `docs/b-*.md` | 호스팅 모드 구성 요소 (접수, 승인, 소스 보관, 원격 빌드, 배포) |
| [docs/state-store-boundary.md](docs/state-store-boundary.md), [docs/postgres-state-foundation.md](docs/postgres-state-foundation.md), [docs/operation-state-foundation.md](docs/operation-state-foundation.md) | 상태 저장소와 작업 상태 |
