# B안 SQLite 소스 준비 단계

현재 SQLite 데이터 준비가 로컬 `DeploymentTools`에 묶여 있어 B안의 임시 워커가 재사용하기 어려웠다. `application.sqlite_preparation`으로 분리하고 기존 에이전트도 같은 함수를 사용한다.

## 구현한 경계

`preflight_sqlite_conversion`은 기존 경로·SQLite 해시·스키마·테이블별 행 수와 함께 생성할 SQL 파일 이름과 SHA-256(`migration`)을 반환한다. 정적 API 준비 결과에도 이 검토 정보가 포함된다. 이때 원본·prepared ZIP은 기존처럼 변경하지 않으며 SQLite 배포 차단을 유지한다.

`SqliteSourcePreparation.prepare(principal, original, conversion, expected_source_digest=...)`는 후속 준비 워커가 사용할 애플리케이션 서비스다.

1. 원본 artifact의 조직 권한과 전체 소스 digest를 확인한다.
2. 기존 SourceArtifactService로 해시가 검증된 소스를 복원한다.
3. 별도 임시 복사본에서 SQLite 해시·스키마·행 수·생성 SQL 해시를 재검사한다.
4. `migrations/0000_sky_sqlite_import.sql`을 작성하고 작업 복사본의 SQLite 파일만 제거한다.
5. 변환된 복사본을 immutable prepared artifact로 저장한다. 원본은 보존한다.
6. `application_conversion_required`, DB 연결 필요, 새 검토 필요 상태를 반환한다. SQL 본문과 행 데이터는 상태에 포함하지 않는다.

같은 입력을 재실행하면 같은 prepared artifact를 얻는다. 로컬 작업 복사본에서 재개할 때는 원본 스냅샷과 SQL의 완전 일치를 다시 검사한다. SQL 수정, 추가 마이그레이션, 행 수 변경, 심볼릭 링크, 원본과 작업 경로 중첩은 거부한다. 기존 로컬 에이전트의 과거 검토는 SQL manifest가 없어도 읽지만 새로운 artifact 준비 서비스는 manifest를 요구한다. 과거 B안 preview는 다시 준비해야 한다.

## 아직 연결하지 않은 경계

후속 준비 큐 구현은 b-sqlite-preparation-queue.md를 참고한다. 이 서비스는 승인된 deploy 작업의 소비 루프에는 등록하지 않는다. 현재 승인된 deploy 작업의 소스를 변환하면 승인 digest가 깨지므로 build consumer가 소스를 임의로 수정하도록 연결해서는 안 된다.

후속 작업은 별도 **배포 전 준비 작업**으로 연결해야 한다.

- 별도 준비 작업의 접수·outbox·SQS 전달·소유권·heartbeat·체크포인트는 후속 준비 큐 PR에 구현했다. 운영 큐·IAM 설정과 실제 활성화는 인프라 담당자와 맞춘다.
- 앱 코드를 PostgreSQL로 변환하고 async 호출·의존성·숫자 응답·게임 프로토콜을 검사한다.
- 팀의 DB 할당 결과에 앱·조직·대상 환경·세대·Secret ARN이 일치하도록 연결한다. 비밀번호는 소스나 작업 명령에 넣지 않는다.
- 완성된 소스의 새 계획과 preview를 만들어 재검토·승인한다.
- 승인된 데이터 이전을 별도 실행하고 성공 여부를 확인한 뒤 원격 이미지 빌드·배포를 실행한다.

SQL 작성은 SQL 실행이나 앱 변환 완료를 의미하지 않는다. 운영 DB DDL, 인프라 apply, 워커 활성화, OpenAI 호출은 이번 변경에 포함하지 않는다. 기존 `sqlite_conversion_required` 차단과 built-image executor의 환경변수 차단도 유지한다.

## 검증

원본 보존, 동일 입력 재실행, 검토·작업 파일 변경 거부, 조직 접근 제한, 경로 링크 제한, 기존 SQLite/Cloud SQL 에이전트 호환성, 실제 TUG ZIP의 13개 기록에 대한 artifact 준비를 검사한다. 생성 SQL 실행은 loopback 임시 PostgreSQL에서만 검사하고 트랜잭션을 rollback한다.

