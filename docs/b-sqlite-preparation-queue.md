# SQLite 준비 작업의 API → Outbox → SQS → 워커 계약

PR #31의 원본 보존·이전 SQL 생성 서비스를 별도 준비 작업으로 연결한다. 기존 승인된 deploy 작업의 소스를 변경하지 않는다. 운영 인프라 활성화는 이 코드 PR에 포함하지 않는다.

## API

`sky-service api --enable-preparation --enable-sqlite-preparation`로 명시적으로 활성화한다. 기존 ALB 신뢰·멤버십 설정, HTTPS `--origin`, DB 및 S3 설정이 필요하다. 기본값은 비활성화다.

- `POST /api/deployment-previews/{id}/prepare-sqlite`
  - 동일 origin, 배포 권한, `Content-Type: application/json`, `Idempotency-Key`가 필요하다.
  - 본문은 `{"preview_digest":"검토 화면에서 받은 digest"}`와 선택 항목 `conversion_profile`을 받는다. 기본값은 `sqlite-data`이며, 검증한 TUG ZIP에만 `tug-postgres-v1`을 선택할 수 있다. 임의 소스·SQL·계획·비밀 값을 받지 않는다.
  - 준비된 정적 preview의 만료·digest·원본 참조·SQLite 지원 여부를 확인하고 작업을 접수한다. DB 작업·outbox 생성이 한 트랜잭션이다.
  - 응답은 202와 작업 ID·상태다. SQL 생성이나 S3 변환은 API에서 실행하지 않는다.
- `GET /api/sqlite-preparation-jobs/{operation_id}`
  - 요청한 사용자·조직에게 상태와 준비 결과를 반환한다.
  - `succeeded`는 이 준비 작업의 완료다. 반환 결과의 `application_conversion_required`, `database_binding_required`, `requires_new_review`가 남는다. 배포 완료를 의미하지 않는다.

UI 버튼은 아직 추가하지 않았다. HTTP 경로를 통해 먼저 검증한다.

## 작업 공간·큐 분리

다음 네 값이 필요하다.

| 환경변수 | 용도 |
| --- | --- |
| `SKY_STATE_WORKSPACE` | 기존 배포 작업 공간 |
| `SKY_JOB_QUEUE_URL` | 기존 배포 FIFO 큐 |
| `SKY_PREPARATION_WORKSPACE` | 준비 전용 작업 공간, 예: `team-prepare` |
| `SKY_PREPARATION_QUEUE_URL` | 같은 계정·리전의 별도 준비 FIFO 큐 |

준비 workspace나 큐가 기존 배포 값과 같으면 시작을 거부한다. 작업 공간은 기존 sky_state 테이블의 논리적 구분이며 별도 DB를 만드는 것이 아니다. `prepare_sqlite` 작업 종류는 코드 허용 목록에만 추가하므로 기존 operation schema에 새 DDL은 필요하지 않다. 다만 기존 스키마가 준비돼 있어야 한다.

인프라 담당자와 별도 큐·DLQ 및 IAM 권한을 맞춘 후 아래 두 프로세스를 띄운다.

```sh
sky-service worker --mode preparation-outbox --check-config
sky-service worker --mode prepare-sqlite --check-config

sky-service worker --mode preparation-outbox
sky-service worker --mode prepare-sqlite
```

두 check-config는 AWS·DB·GitHub 호출 없이 설정을 검사한다. 소비 워커는 유효한 Fargate `ECS_AGENT_URI`와 ECS task protection을 요구한다. 기존 DB 비밀 읽기 권한, 준비 큐의 publish/receive/delete/visibility 권한, 원본 S3 읽기·준비 artifact 쓰기 권한이 필요하다. 이 워커에는 GitHub App·OpenAI·앱 DB 비밀번호·Docker가 필요하지 않다. 이 단계는 앱 코드의 AI 변환을 실행하지 않는다. `tug-postgres-v1`은 원본 digest가 일치하는 게임에 고정된 PostgreSQL 변환을 적용한다.

## 처리·복구

- 큐 본문은 workspace·작업·시도·앱 ID만 포함한다. 소스와 검토 내용은 DB의 불변 명령에서 읽는다.
- 한 준비 workspace 안에서 앱별 중복 실행을 막는다. 준비와 배포는 작업 공간이 다르므로 서로 같은 앱 잠금을 공유하지 않는다. 준비 작업은 불변 소스 사본만 작성하며 실행 중 앱 리소스를 바꾸지 않는다.
- lease, SQS visibility, ECS task protection을 갱신한다. 소유권을 잃으면 결과를 저장하거나 메시지를 삭제하지 않는다.
- 임시 작업 디렉터리는 정리된다. 원본 artifact는 유지된다.
- S3 쓰기는 내용 주소와 조건부 생성으로 보호된다. 응답 유실 시 같은 원본에서 재생성한 artifact를 다시 검증한다. 저장된 결과 checkpoint도 재생성 결과와 일치해야 한다.
- SIGTERM 등의 정상 중단은 새 attempt와 outbox를 만든다. 갑작스러운 프로세스 종료는 lease 만료 후 같은 기존 복구 규칙을 따른다.
- 처리 중 잡힌 일시 I/O 오류는 최초 시도를 포함해 최대 세 번 실행한다. 입력·검토 오류는 즉시 실패한다. 정상 중단이나 프로세스 강제 종료의 횟수는 이 I/O 제한에 포함하지 않는다.
- 완료 DB commit의 응답을 잃으면 메시지를 보존한다. 재전달 시 DB의 완료 상태를 확인해 삭제하며 다시 실행하지 않는다.

## 다음 작업

검증한 TUG의 PostgreSQL 변환·async 호출·게임 프로토콜은 구현 및 임시 DB 검증을 완료했다. 팀 할당 작업 결과의 실제 조회·ECS 설정 반영, 변환된 소스의 새 preview·승인 생성, 승인된 SQL 이전 실행이 남아 있다. 기존 SQLite preview의 배포 차단은 유지한다. 현재 작업 결과 artifact만으로 기존 승인을 재사용해 빌드·배포할 수 없다.

