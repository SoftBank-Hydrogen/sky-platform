# 모니터링·k6 통합 검증 (2026-10-10)

기준 main: a7233488b447ad8e2eba8dbb917b7b5b6c0546ed.
운영 코드 변경 없이 실제 경계를 연결하여 PR #12 수정의 통합 동작을 확인했다.

## 실행 경로

임시 PostgreSQL → 실제 DeploymentReadService / DatabaseReadApp HTTP 서버 →
테스트 세션 프록시 → observer collectSource·collect·render → 테스트 게임 HTTP/WebSocket.

ALB/Cognito 자체는 실행하지 않았다. 프록시는 테스트 쿠키를 확인하고 로컬 테스트 키로
서명한 ALB 형식의 JWT를 전달한다. 서버는 실제 ES256 서명·issuer/client/signer 및
명시적 조직 멤버십 검증 코드를 사용한다. 공개키 공급만 로컬 테스트 키로 대체했다.
이 테스트의 서명 헤더와 키는 실서비스에 사용하지 않는다.

운영 DB·AWS·Slack 요청은 수행하지 않았다. PostgreSQL은 신규 임시 컨테이너의 tmpfs에
생성했으며 외부 통신이 차단된 internal Docker 네트워크에 배치했다.
테스트 프로세스만 루프백 TCP 프록시로 연결하고, 종료 후 컨테이너와 네트워크를 제거했다.
CI에서는 기존 임시 PostgreSQL 서비스를 사용한다.

## 결과

| 검증 | 결과 |
| --- | --- |
| 실제 DB에 같은 조직의 기록 51개 저장 | 전체 집계 succeeded 1, other 50 |
| 별도 조직 기록 | 조회·집계에서 제외 |
| 게임이 목록 두 번째 페이지에 있음 | 커서 조회 후 자동 발견·등록 |
| 게임 HTTP·scoreboard·WebSocket nonce | 실제 HTTP/WS 테스트 서버 응답 및 메트릭 생성 |
| 페이지 중간 503 | 부분 집계를 내보내지 않고 기존 대상 유지 |
| 상세 조회 503 | 발견 갱신 실패를 표시하고 기존 대상 유지 |
| 서명 토큰 만료·세션 리다이렉트 | 수집 실패 표시, 기존 대상 유지 |
| 정상 인증으로 복귀 | 수집·발견 갱신 정상 복구 |
| DB의 앱 URL 변경 | 안정된 대상 이름 유지, 새 URL로 수집 |
| 테스트 게임 중단·복구 | WebSocket 상태 메트릭 1 → 0 → 1 |
| deployment_state=deleted | 발견 대상 및 해당 대상 메트릭 제거 |
| Sky 쿠키의 게임 전달·플레이어 입장 | 쿠키 전달 없음, join 없음 |
| hosted API에 로컬 X-Sky-Token 전달 | 실제 핸들러가 403으로 거부 |
| 기존 로컬 App / 배열 API | 수집·집계 호환 유지 |
| 실제 k6 hosted smoke | 조회 iteration 5회, sky_read_failures 0 |
| 실제 k6에 만료된 서명 전달 | 사전 검사에서 실패, 부하 반복 시작 안 함 |

pytest 통합 시나리오 3개가 통과했다. 위 표의 검증들은 각 시나리오의 여러 단계다.
기존 observer/discovery/k6 모의 계약 테스트 23개와 Ruff 검사도 통과했다.

## 재실행

먼저 ops/monitoring/exporter에서 npm ci --ignore-scripts를 실행한다.
SKY_TEST_POSTGRES_DSN은 운영 DB가 아닌 임시 루프백 PostgreSQL만 지정해야 한다.

    export SKY_TEST_K6_IMAGE='grafana/k6:2.3.0@sha256:9c2dee7f8ed74d317e4027c06a10f169b625638189de8d4555d0b3486a5aeb34'
    python -m pytest tests/contract/test_monitoring_integration.py -q

DSN이 없으면 DB 통합 테스트는 skip하고, k6 이미지 변수가 없으면 k6 시나리오는 skip한다.
CI는 임시 PostgreSQL, Node 의존성과 k6 이미지 변수를 준비해 모두 실행한다.
k6 Docker는 loopback HTTP 서버에 연결하기 위해 host 네트워크를 사용한다.
테스트의 대상 URL·공개키·세션·게임은 모두 로컬 fixture이며 실서비스 연결 정보는 사용하지 않는다.

## 검증 범위의 한계

실제 ALB/Cognito 로그인·공개키 서비스, ECS 배포 및 운영 세션은 아직 확인하지 않았다.
게임은 HTTP/WebSocket 규약을 구현한 테스트 서버이며 ZIP으로 배포한 실제 앱이 아니다.
Prometheus·Alertmanager의 실행 중 알림 전송과 실제 Slack 수신도 이 테스트 범위 밖이다.
기존 별도 rule/config 테스트를 대신하지 않는다.
이번 결과는 연결 동작 검증이며 AWS 처리량·최대 동시 접속자나 장시간 성능 측정 결과가 아니다.
observer의 실제 주기 타이머·9108 리스너는 실행하지 않고 같은 수집·발견·메트릭 함수를 호출한다.
