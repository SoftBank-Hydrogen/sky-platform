# B안 1단계: 배포 메타데이터 저장 경계

## 이번 변경

`ports.state.DeploymentRecordStore`는 파일 경로 없이 작업 목록/기록, 헬스체크 기록,
GitHub 자동 배포 설정을 읽고 쓴다. `StoredJob.modified_at`은 created_at이 없는 이전 기록을
복원할 때 사용하는 시각 문자열이다. 반환값은 호출자가 변경해도 저장된 데이터가 바뀌지
않는 분리된 JSON 스냅샷이며, 각 저장 호출은 하나의 기록을 원자적으로 교체한다.

`App(..., record_store=...)`로 구현을 주입한다. 기본 구현은
`DirectoryDeploymentRecordStore`로 기존 `<job_id>/job.json`, `<job_id>/health.json`,
`github-sources.json` 경로와 JSON 형식을 유지한다. 임시 파일(0600)에 쓰고 flush/fsync 후
replace하며, 실패한 임시 파일을 정리한다. job_record_version과 상태 검증,
중단된 배포/롤백 복구 정책은 application 계층에 남긴다.

읽기/쓰기 I/O 실패는 OSError로, 손상된 저장 값은 ValueError로 전달한다.
load_health/load_github_sources의 None은 기록 부재만 의미한다. JSON null은 손상된
기록으로 보고한다. 작업 저장 실패 시 성공 결과를 제거하고 오류 상태를 표시하며,
관측 기록 저장 실패는 배포 성공/실패 상태를 바꾸지 않는다.

업로드 정리는 로컬 job.json뿐 아니라 저장소의 접수된 작업 ID도 확인한다.
따라서 주입한 저장소에 기록이 있는 업로드를 미접수 파일로 잘못 삭제하지 않는다.

## 아직 로컬 구현에 남은 항목

| 상태/산출물 | 현재 | B안 후속 작업 |
|---|---|---|
| 작업·배포·계획·승인·이벤트 메타데이터 | jobs 메모리 + 위 포트를 통한 JSON 저장 | RDS 구현 및 DB 중심 조회·갱신 |
| 헬스 관측 이력 | health_history 메모리 + 저장 포트 | RDS 관측 기록 |
| GitHub 등록 설정·최근 revision | github_sources 메모리 + 저장 포트 | RDS 체크포인트·워커 실행 |
| active_groups, github_polling, monitor_errors | 프로세스 메모리 | DB lease/멱등 제어·상태 보고 |
| 인증 토큰 | 프로세스마다 생성 | API 복제본 간 인증 정책 일관성 |
| PostgreSQL 생성/종료, 스냅샷, 네트워크 작업 기록과 정리 archive | 각 Operations의 별도 JSON·메모리 | 각 작업의 저장 경계·RDS 이관 |
| ZIP·추출 소스·임시 작업 폴더·소스 변환 산출물 | 로컬 파일과 절대 project 경로 | S3 객체 참조·해시, 워커 다운로드 |
| 실행 스레드·배포 그룹·GitHub 폴링 | API 내부 실행 | API/워커 분리 + SQS/outbox |
| 사용자 앱 빌드·리허설 | 호스트 Docker | GitHub Actions 연동 |

이 포트는 공유 DB 구현, 복제본 간 조회 일관성, lease, 여러 기록의 트랜잭션이나
SQS 작업 계약을 제공하지 않는다. 현재 App은 시작 시 읽어 온 스냅샷과 기존 로컬
복구 규칙을 사용한다. 다른 프로세스에서 저장된 변경을 실시간 조회하지 않으며,
다른 워커가 실행 중인 작업을 안전하게 판별할 수 없다. RDS 어댑터만 주입해서
API 두 개를 운영하면 안 된다. DB 중심 조회/갱신과 lease 기반 복구를 함께 구현해야 한다.

## 다음 작업

1. 메타데이터 RDS 스키마와 원자적 상태 전환/lease/outbox 계약 정의.
2. 남은 Operations 기록 저장 경계 분리 및 source/artifact 참조 모델 작성.
3. RDS/S3 구현과 실제 프로세스 간 상태 조회, 재시작 복구 검증.
4. 이후 SQS 워커와 원격 빌드 연결.

계약 테스트는 파일 없는 저장소를 주입해 재시작·불확실한 롤백·관측·GitHub 설정
복원을 확인하며, 파일 구현에서는 원자적 저장 실패·권한·손상 기록 처리를 검증한다.
