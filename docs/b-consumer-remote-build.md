# B 소비 워커와 원격 이미지 빌드

## 이번 구현의 범위

저장된 승인으로 접수된 AWS 작업을 SQS FIFO에서 받아, 별도 sky-builder의 GitHub
Actions로 이미지 빌드를 요청하고, 결과와 ECR 다이제스트를 확인하는 실행 경로입니다.
API/워커 컨테이너에서는 Docker나 사용자 앱 코드를 실행하지 않습니다.
현재 마지막 단계는 `build_ready` / `awaiting_deployment`입니다. ECS 배포 실행기는
별도 다음 작업이며, 빌드를 배포 성공으로 처리하거나 앱 잠금을 풀지 않습니다.
operation은 기존 `needs_attention` 상태로 보관하고 checkpoint.reason을
`deployment_executor_required`로 남깁니다. 미래 배포 실행기에 연결할 명시적 재개
계약이 필요하며 이 상태의 작업을 직접 SQL로 queued로 바꾸지 마세요.

## 실행 경로

1. outbox publisher가 작업 ID와 시도 ID만 담긴 메시지를 전송합니다.
2. build consumer는 같은 workspace/앱/시도인지 확인하고 DB 실행 lease를 획득합니다.
   소비된 승인과 job의 소유권·소스·계획을 다시 확인한 후 진행합니다.
3. worker는 준비된 소스 참조와 canonical 승인 계획을 담은 불변 request를
   `sources/<org>/<app>/<upload>/build-requests/<operation UUID>.json`에 저장합니다.
4. DB에 GitHub 외부 요청 intent를 먼저 저장한 뒤, 고정된 저장소/워크플로/커밋에
   dispatch합니다. 입력에는 build ID, S3 request key, request digest만 담습니다.
5. GitHub prepare job은 OIDC로 S3 소스를 읽습니다. 별도 build job은 AWS 자격 증명과
   OIDC 권한 없이 소스 ZIP와 tree digest를 검증하고 linux/amd64 이미지를 만듭니다.
   publish job은 별도 러너에서 ECR push 및 `builds/<build ID>/result.json` 기록을 합니다.
6. worker는 GitHub 실행의 repository/ref/head SHA/결과, 모든 request/source/plan
   digest, ECR의 실제 tag digest를 확인하고 digest로 고정된 image URI를 기록합니다.
   결과 확인과 job 상태 기록은 lease로 보호합니다. job과 operation 진행 상태는
   한 DB 트랜잭션으로 반영하고 metadata revision도 증가시킵니다.

빌드 tag는 `sky-managed:build-<operation UUID>`입니다. 플랫폼 서비스 이미지의
7자리 SHA tag와 별개입니다. 재실행 시 동일 operation의 외부 빌드를 식별하기 위해
attempt ID 대신 operation UUID를 빌드 ID로 사용합니다. 소스는 기존 S3의 정규화 ZIP
계약을 그대로 사용합니다(설계 문서의 tar.gz 표현과 다른 형식).

## 장애·중복 처리

- DB heartbeat 30초 / lease 90초, SQS visibility 300초를 계속 연장합니다.
  ECS agent task protection은 5분으로 설정하고 heartbeat에서 갱신합니다.
- 이미 실행 중인 중복 메시지는 삭제하지 않고 지연합니다. 완료·확인 필요·오래된
  시도 메시지는 DB에 작업이 남아 있는 것을 확인한 뒤 삭제합니다.
- 잘못된 메시지와 다른 workspace/앱 메시지는 실행하거나 삭제하지 않습니다.
  visibility를 60초로 변경하고 SQS redrive/DLQ 정책에 맡깁니다.
- dispatch 응답 유실, 결과 변조, timeout, 중단 시 외부 intent와 앱 잠금을 유지합니다.
  자동 재dispatch하지 않습니다. 관측 실패는 최대 45분까지 재조회합니다.
- 외부 결과 관측 후 DB 최종 기록 전에 중단되면 저장 checkpoint와 GitHub/ECR
  증거를 다시 확인하여 두 번째 빌드 없이 상태 기록을 재개합니다.
- 확인된 workflow 실패는 failed로 기록하고 앱 잠금을 해제합니다.
- image tag가 이미 있는데 결과가 없으면 덮어쓰거나 새 빌드로 추정하지 않습니다.
  workflow 재실행·중복 dispatch는 명시적 확인 대상입니다.
- GitHub 실행 검색은 해당 workflow의 최근 100개로 제한합니다. 그 안에서 찾을 수
  없거나 복수 실행이면 성공으로 추정하지 않고 확인 필요 상태가 됩니다.

기존 recover_expired/resolve_attention은 불확실한 외부 효과의 조사 계약입니다.
실제 GitHub/ECR 조사 및 안전한 재개용 운영 도구, DLQ 재처리 UI, 저장소/S3 retention
정책과의 통합은 아직 별도 작업입니다. 다른 기존 writer와 섞어 실행하지 마세요.

## sky-builder 설치

비공개 저장소 https://github.com/SoftBank-Hydrogen/sky-builder 를 생성했으며,
초기 workflow commit은 `5d08530c9ed4d143fbedee947fe23effbeb35cd9`입니다.
실행 변수·App·OIDC 설정과 실제 dispatch는 아직 하지 않았습니다.

`ops/sky-builder/.github/workflows/build.yml`을 별도 저장소의
`.github/workflows/build.yml`로 복사합니다. workflow_dispatch만 있어 push 자체로
사용자 앱 빌드나 AWS 작업을 시작하지 않습니다. sky-platform은 공개 저장소여서
고정 commit checkout에 별도 PAT가 필요하지 않습니다.

빌드 저장소 variables:

- SKY_PLATFORM_CODE_SHA: 검토된 sky-platform 코드의 전체 40자리 commit
- AWS_ACCOUNT_ID / AWS_REGION / SKY_ARTIFACTS_BUCKET
- AWS_APP_BUILDER_ROLE_ARN: sky-infra의 app-builder OIDC 역할

main 브랜치 workflow가 게시된 뒤 builder main의 전체 commit SHA를 worker에
SKY_BUILDER_SHA로 설정합니다. 두 코드 pin을 변경할 때는 워커 설정과 builder
variable을 함께 맞춰야 합니다. 이동한 ref는 dispatch 전에 거부합니다.
GitHub App은 sky-builder에 설치되어 Actions write / Contents read 권한이 필요합니다.
IAM trust에는 새 저장소와 브랜치 또는 실제 조직 OIDC subject 형식을 반영해야 합니다.
현재 app-builder IAM의 sources/ GetObject, builds/ PutObject, sky-managed push 및
DescribeImages 범위에 맞춥니다. builds/ GetObject를 새로 요구하지 않습니다.
`sky-managed` ECR은 사전에 Sky 관리 기반에서 생성되어 있어야 합니다.

## worker 설정

기존 DB/S3/workspace/SQS 설정 외에:

- SKY_BUILDER_REPOSITORY / SKY_BUILDER_REF (권장 main)
- SKY_BUILDER_SHA / SKY_BUILDER_PLATFORM_SHA (모두 전체 40자리)
- SKY_GITHUB_APP_ID / SKY_GITHUB_INSTALLATION_ID / SKY_GITHUB_APP_PRIVATE_KEY
- ECS_AGENT_URI: Fargate가 제공하는 task protection agent 주소

GitHub App private key는 실행 비밀로 전달하고 repository variable이나 코드에 넣지
않습니다. installation token은 메모리에 짧게 캐시하며 큐·DB·S3·로그에 기록하지 않습니다.

`sky-service worker --mode build --check-config`는 설정 형식만 검사합니다.
AWS/DB/GitHub 접속을 하지 않습니다. 실제 소비는 `sky-service worker --mode build`.
기존 `worker --mode outbox`는 별도 publisher입니다. 현재 둘을 같은 프로세스에서
자동 실행하거나 기본 워커 명령을 바꾸지 않습니다. 팀과 DB 스키마·태스크 설정·App
자격 증명·OIDC를 맞춘 뒤 활성화하세요. runtime은 DDL을 실행하지 않습니다.

## 검증과 한계

격리 PostgreSQL 계약 테스트: 실제 승인→접수→큐 소비→원격 빌드 모의 결과→조회,
lease 만료·중복·응답 유실·잘못된 결과·상태 기록 rollback·중단 후 재개 검증.
SQS/GitHub/S3/ECR은 모의 처리했습니다. 자격 증명 없는 실제 로컬 Docker 빌드와
오프라인 fixture 실행도 검증합니다(캐시된 Node base image가 있는 경우).
실제 GitHub App/OIDC/S3/ECR 원격 빌드와 Fargate task protection은 아직 실환경에서
실행하지 않았습니다. 운영 DB/AWS 리소스 변경은 수행하지 않았습니다.
TUG의 SQLite 코드·데이터 전환은 별도이며 이 변경이 자동 해결하지 않습니다.
