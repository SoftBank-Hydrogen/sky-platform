# 검증한 TUG ZIP의 PostgreSQL 변환

## 범위와 호출

`POST /api/deployment-previews/{id}/prepare-sqlite`에 기존 `preview_digest`와 `conversion_profile: "tug-postgres-v1"`을 전달한다. 기존 인증·권한·origin·idempotency 검사는 그대로 필요하다. 기본 `sqlite-data`는 SQL 준비만 한다.

이 레시피는 TUG-Sky-almostfinaltest.zip의 정규화 원본 digest `c925c00901ac9691a1d404ecbccb41e9067e5298b46b8a386f1d9a2012f38b86`에만 적용한다. 다른 ZIP은 접수를 거부한다. 범용 앱 변환이나 AI 변환이 아니다. 원본을 보존하고 사본의 db.js, server.js, package.json, package-lock.json과 RDS CA를 변경한다. 워커는 npm, Docker, SQL을 실행하지 않는다.

PostgreSQL pg 8.23.0을 고정하고 모든 저장·조회 호출을 비동기로 바꾼다. 파라미터 SQL, 안전한 정수 변환, 연결 수 제한, 시간 제한, CA 검증 TLS를 사용한다. /health는 rounds 테이블 조회까지 확인하며 DB 장애·테이블 누락 시 503을 반환한다. 게임 방은 메모리에 있으므로 게임 서버는 여전히 단일 replica가 필요하다.

변환 결과는 `database_binding_required`이며 새 source digest와 변경 파일 해시를 포함한다. 이전 SQLite preview 승인을 재사용할 수 없다.

## DB 할당 결과와 환경변수 제안

`reference_from_allocation`은 신뢰할 수 있는 워커 작업 저장소에서 읽은 할당 완료 결과를 받는 계약이다. 브라우저가 제출한 결과를 신뢰해서는 안 된다. 조직·앱·계정·리전·allocation ID·원본/변환 artifact·DB 이름·로그인 역할·Secret ARN을 확인한다. 현재 지원 계약은 shared_workload이며 sky_state와 dedicated/promotion은 거부한다.

`AwsApplicationDatabaseEnvironment.plan()`은 STS 계정, RDS 상태·태그·암호화·비공개 설정, Secret 소유 태그·RDS resource ID를 읽어 환경변수 제안을 만든다. GetSecretValue나 쓰기 호출을 하지 않는다. PGPASSWORD는 ECS secrets 항목에 `Secret ARN:password::` 참조로 제안한다. PGHOST, PGPORT, PGDATABASE, PGUSER, PGSSLROOTCERT, SKY_APP_DB_CONNECTION_LIMIT은 일반 환경변수다.

아직 실제 할당 작업 저장소 조회와 ECS 실행 경로에 연결하지 않았다. 메타데이터 검사는 실제 SQL ACL·네트워크 접근을 증명하지 않는다. 새 preview 승인, 실행 역할의 Secret/KMS 권한, 네트워크 확인, 승인된 SQL 이전 실행과 배포 통합 검증이 필요하다. 팀의 개별 RDS 전환 계약은 합의 후 별도로 연결해야 한다.

## 검증

격리된 loopback PostgreSQL 17에서 CA 검증 TLS와 권한이 제한된 앱 역할로 원본 13개 기록을 이전하고 모든 필드를 비교했다. 변환한 Node 22 이미지에서 HTTP, WebSocket probe, 다른 origin 거부, 2인 게임 결과와 점수 저장(13→14), heartbeat를 검증했다. DB 연결 단절 시 503, 복구 후 200, 앱 재시작 및 이미지 교체 후 14개 기록 유지도 확인했다.

이는 프로토콜 수준 검증이며 Unity 화면 플레이, 실제 AWS 배포, AWS 성능 측정 결과가 아니다. 운영 DB나 Terraform은 변경하지 않았다. 자동 테스트는 모의 AWS 메타데이터와 임시 PostgreSQL을 사용한다.
