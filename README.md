# Sky

**One Action, Infinite Clouds.** 웹앱을 올리면 실행 환경을 판단하고, 배포 결과를 확인합니다.

SoftBank Hackathon 2026 팀 **SoftBank-Hydrogen**의 배포 플랫폼입니다.
ZIP·앱 폴더·공개 GitHub 저장소를 입력으로 받습니다. 배포 대상 앱보다 **배포 시스템의 품질**에 집중합니다.

## 배포 흐름

```text
앱 입력 → 소스 분석 → 후보 비교 → 코드·인프라 계획 → 빌드·배포 → 실제 응답 검증
```

- **근거로 판단합니다.** 설정 파일·소스·DB를 규칙과 AST로 분석합니다. AI의 판단과 인용도 검증합니다.
- **환경 차이를 처리합니다.** 지원하는 패턴에서 실행 설정과 코드를 바꿉니다. 원본은 보존합니다.
- **실행 결과로 성공을 판단합니다.** HTTP 응답을 확인하고, DB·WebSocket 등 추가 검증 결과를 기록합니다.
- **운영까지 이어집니다.** GitHub 변경 감지, 갱신, 롤백, 모니터링 기능을 제공합니다.

AI는 계획과 코드 변경을 제안합니다. Python 서비스가 계약·정책을 검사하고 어댑터로 실행합니다.
지원하지 않거나 근거가 부족한 요구사항은 차단하거나 추가 확인을 요청합니다.

## 배포 대상

| 대상 | 용도 | 구현·검증 상태 |
| --- | --- | --- |
| Local Docker | 같은 PC에서 컨테이너 실행 | 실제 배포 검증 |
| On-prem Compose | 같은 PC에서 Compose 실행, SQLite 볼륨 보관 | 구현 |
| On-prem Linux VM | SSH로 원격 Docker에 배포 | 로컬 Linux VM 검증 |
| AWS ECS Express | Fargate 컨테이너, 관리형 HTTPS 주소 | 실제 AWS 배포 검증 |
| AWS S3 + CloudFront | 정적 웹앱 호스팅 | 실제 AWS 배포 검증 |
| AWS Lambda | 외부 의존성 없는 Python 핸들러 | 명시 선택 ZIP 배포·실제 AWS 검증 |
| AWS EC2 | 무상태 HTTP 컨테이너 | 명시 선택 ZIP 배포 구현, 실배포 미검증 |
| GCP Cloud Run | 컨테이너 실행, Cloud SQL 연결 경로 | 구현, 전체 실환경 검증 필요 |

자동 선택은 **서버에 설정된 대상·앱 요구사항·사용자 정책**을 기준으로 후보를 좁힙니다.
Lambda·EC2는 명시 선택 경로이며, AI 자동 선택과는 아직 연결되지 않았습니다.
현재 EC2 경로는 DB·영속 파일·워커·호스트 제어를 지원하지 않습니다.

DB가 없는 앱은 DB 없이 배포합니다. SQLite는 지원 경로에서 볼륨에 보관하거나 PostgreSQL로 이전합니다.
AWS RDS는 앱의 DB 구성 요소입니다. 게임 데모에서 SQLite → PostgreSQL 코드 변환과 데이터 이전을 검증했습니다.
이 검증이 모든 앱이나 모든 배포 대상에 대한 성공을 보장하지는 않습니다.

## 빠른 시작

Python 3.12 이상과 Docker가 필요합니다.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,aws-native,state-postgres]'
.venv/bin/sky-platform
```

브라우저에서 **http://127.0.0.1:8080**에 접속합니다.
앱을 올리고 대상을 선택한 뒤 **배포하기**를 누릅니다. 설정되지 않은 대상은 화면에서 비활성화됩니다.

AI 기능을 쓰려면 `OPENAI_API_KEY`를 설정합니다.
로컬 실행은 현재 디렉터리의 `.env`도 읽습니다. 파일 권한은 `chmod 600 .env`로 제한합니다.
키가 없어도 정해진 규칙과 명시 대상의 지원 경로를 사용할 수 있습니다.

| 기능 | 필요한 설정 |
| --- | --- |
| AWS | AWS CLI 자격 증명, `SKY_AWS_ACCOUNT_ID`, `SKY_AWS_REGION` |
| Lambda·EC2 | 위 AWS 설정과 `SKY_AWS_ROLE_BOUNDARY_ARN`, EC2는 `SKY_EC2_SUBNET_ID` 추가 |
| GCP | `gcloud` 인증, `SKY_GCP_PROJECT`, `SKY_GCP_REGION` |
| Linux VM | `SKY_VM_SSH_HOST`, `SKY_VM_SSH_USER`, `SKY_VM_PUBLIC_HOST` |

계정·역할 권한·네트워크 설정도 해당 어댑터의 조건을 충족해야 합니다.
실행 옵션은 `.venv/bin/sky-platform --help`에서 확인할 수 있습니다.

## 로컬 실행과 클라우드 호스팅

로컬 실행은 `.sky/`에 작업 상태를 저장합니다.
호스팅 모드는 PostgreSQL에 상태를, S3에 소스를 저장합니다. **Sky 상태 DB와 사용자 앱 DB는 분리합니다.**

```text
ALB + Cognito → API → PostgreSQL 상태 DB / S3 소스
                  → 작업 접수·outbox → SQS → 워커 → 원격 빌더 → ECR → 앱 배포
```

호스팅 API는 신원·조직 소유권을 확인합니다. ZIP 접수는 서버에서 검증·승인 기록·작업 등록을 이어 처리합니다.
실제 실행에는 호환되는 워커와 빌더 설정이 필요합니다. API에 OpenAI 키를 넣는 방식은 사용하지 않습니다.
호스팅 모드의 SQLite 변환·DB 연결을 포함한 전체 자동 배포는 아직 통합 검증이 남아 있습니다.

V4의 목표는 **관측 → 변경 제안 → 승인 → 재배포 → 검증**입니다.
운영 판단, 공유 DB 할당, 전용 RDS 준비, 데이터 이전·전환 구성 요소가 있습니다.
이 기능을 모든 ZIP 앱에 자동 적용하는 운영 루프는 아직 완성·검증된 상태가 아닙니다.

## 저장소와 코드 구조

| 저장소 | 역할 |
| --- | --- |
| **sky-platform** | 분석, 판단, 코드 변환, 배포, API·워커·UI |
| [sky-infra](https://github.com/SoftBank-Hydrogen/sky-infra) | Terraform 기반 Sky 호스팅 인프라, Ansible 기반 VM 준비 |
| [demo-game](https://github.com/SoftBank-Hydrogen/demo-game) | 배포 시연용 앱 |

```text
src/
  domain/        모델과 접근 권한
  engine/        증거, 후보 평가, 호환성, 컴파일
  application/   분석·배포·DB 이전·롤백·모니터링 유스케이스
  adapters/      AWS·GCP·Docker·VM·DB·AI·GitHub 연결
  interfaces/    HTTP API, CLI, 호스팅 API·워커
  assets/        UI, CloudFormation 템플릿, DB 마이그레이터
tests/           단위·계약·회귀·통합·실환경 테스트
```

Sky 호스팅 리소스는 `sky-infra`가 관리합니다. 사용자 앱 리소스는 배포 어댑터가 별도로 관리합니다.
`terraform destroy`만으로 사용자 앱 리소스까지 정리되지는 않습니다.

## 개발·검증

```sh
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

PostgreSQL·Docker 통합 테스트에는 별도 실행 환경이 필요합니다. 실계정·실모델 테스트는 수동으로 실행합니다.
GitHub Actions에는 코드 검사, UI 테스트, 서비스 이미지 검증과 ECR 게시 흐름이 정의돼 있습니다.
현재 주 CI 워크플로는 수동 비활성화 상태입니다. 이미지 게시에는 워크플로 활성화와 OIDC·ECR 설정이 필요합니다.
서비스 이미지 게시와 실제 서비스 갱신은 별도 단계입니다.

## 사용·운영 문서

- [Linux VM 배포](docs/onprem-vm.md)
- [롤백 리허설](docs/rollback-rehearsal.md)
- [모니터링](docs/service-monitoring.md)
- [호스팅 ZIP 접수](docs/b-upload-preview-flow.md) · [원격 빌드](docs/b-consumer-remote-build.md) · [이미지 배포](docs/b-built-image-deployment.md)
- [상태 저장소](docs/state-store-boundary.md) · [PostgreSQL 상태 DB](docs/postgres-state-foundation.md)
- [서비스 이미지와 인프라 연결](docs/infra-image-pr.md)
