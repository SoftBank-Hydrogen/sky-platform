# Sky k6 시나리오

서비스 조회 성능을 먼저 확인한다. 업로드·AI 분석·빌드·AWS 배포·배포 앱 health/probe 요청은 실행하지 않는다. AWS 리소스를 새로 만들지는 않지만 기존 서비스의 요청·로그 비용과 부하는 발생할 수 있다.

## 시나리오

| PROFILE | 부하 | 목적 |
| --- | --- | --- |
| smoke (기본) | 1 VU, 조회 5회 | 실제 Sky 연결·인증·응답 형식 확인 |
| baseline | 초당 조회 10회, 5분 | 기준 성능 측정 |
| ramp | 1→5→10→20→1회/초, 총 10분 | 부하 증가·감소에 따른 변화 |
| soak | 초당 조회 5회, 15분 | 지속 부하에서 자원·오류 변화 |

조회는 화면 `/` 20%, 작업 목록 `/api/jobs` 80%다. `JOB_ID`를 지정하면 목록 70%, 작업 상세 10%가 된다. 각 iteration에서 요청을 1번 보내므로 설정 rate는 조회 요청/초다. 시작 전 인증 모드별 사전 확인 요청은 별도다. VU는 동시 실행 자리이며 사용자 수와 동일하지 않다.

초기 통과 기준은 조회 오류율 1% 미만, p95 1초 미만, p99 2초 미만이다. 응답이 200이어도 HTML 로그인 화면이나 잘못된 JSON이면 실패로 센다. 부하를 보내지 못한 `dropped_iterations`도 확인한다. 서버 지연과 부하 발생기 부족을 구분해야 하며, 이는 HTTP 5xx와 다른 지표다. 이 기준과 부하는 가설이며 실측 후 조정한다.

## 실행

k6는 [Grafana 공식 이미지](https://hub.docker.com/r/grafana/k6) `2.3.0`을 기준으로 준비했다. 아래는 Linux/VM에서 저장소 루트 기준 실행 예시다. 본인이 관리하는 테스트 서버 주소를 넣는다. Docker가 없으면 별도 설치된 k6로 같은 환경변수를 전달해 실행할 수 있다.

```sh
docker run --rm --read-only --cap-drop ALL \
  -v "$PWD/scripts/load:/scripts:ro" \
  -e BASE_URL=https://YOUR-SKY-HOST \
  -e PROFILE=smoke \
  grafana/k6:2.3.0 run /scripts/platform.js > sky-smoke.json
```

JSON 결과에는 요약 지표만 담는다. 출력 파일은 저장소 밖에 보관한다. `--http-debug`는 인증 정보를 노출할 수 있으므로 사용하지 않는다. 최초 실행은 smoke부터 한다. 실제 서비스의 baseline/ramp/soak는 팀과 테스트 시간을 맞추고 진행한다.

```sh
# 짧은 확인: 초당 2회, 10초
docker run --rm --read-only --cap-drop ALL \
  -v "$PWD/scripts/load:/scripts:ro" \
  -e BASE_URL=https://YOUR-SKY-HOST \
  -e PROFILE=baseline -e RATE=2 -e DURATION=10s \
  grafana/k6:2.3.0 run /scripts/platform.js > sky-baseline.json
```

`RATE`는 baseline/soak에서 1~50, `DURATION`은 5초~30분 범위로 제한한다. ramp는 표의 고정 단계로 실행하며 RATE/DURATION으로 바뀌지 않는다. 기본 최대 100 VU다. 이 제한이 서버 안전을 보장하는 것은 아니므로 CPU·메모리·알람을 함께 관찰한다. 루프백 외 HTTP는 기본 차단한다. 폐쇄된 검증 환경에서만 `ALLOW_INSECURE_HTTP=true`를 사용한다. TLS 인증서 검증은 끄지 않는다.

## Cognito와 Sky 토큰

ALB에 Cognito가 연결되어 있으면 정상 로그인 세션의 Cookie 헤더를 `SKY_COOKIE` 환경변수로 전달한다. 쿠키는 본인 터미널에서만 준비하고 채팅·Git·결과 문서에 남기지 않는다. 로그인/MFA 자동화와 새 사용자 인증 성능은 이번 범위에 포함하지 않는다. 로그인 리다이렉트는 따라가지 않고 실패 처리한다.

인증 방식은 `AUTH_MODE=local|hosted`으로 지정하며 기본은 local이다. local은 화면에서 X-Sky-Token을 추출하거나 SKY_API_TOKEN을 사용한다. ALB/Cognito 배포에는 `AUTH_MODE=hosted`과 SKY_COOKIE를 전달한다. hosted에서는 로컬 토큰을 보내지 않으며 SKY_API_TOKEN 설정을 거부한다. 인증된 /api/config 응답도 검사한다. ALB가 전달하는 신원은 서버의 조직 멤버십에 등록되어 있어야 한다.

작업 목록은 기존 배열과 {items,next_cursor} 응답을 지원한다. 기본 부하는 첫 페이지에만 보내며 전체 기록 개수를 측정하지 않는다. 다른 페이지는 JOB_CURSOR로 불투명 커서를 지정한다. 커서는 URL 인코딩하고 메트릭 태그에는 넣지 않는다. 각 iteration의 요청 수는 계속 1회다. 사전 확인은 local 3~4회, hosted 4~5회다.

Docker에 인증 환경변수를 전달할 때 값 자체를 명령에 적지 말고 `-e SKY_COOKIE -e SKY_API_TOKEN` 형태로 전달한다. 접속 주소에 토큰·비밀번호를 넣지 않는다.

## 배포 작업과 게임은 별도 검증

조회 테스트가 통과해도 ZIP 배포 성공을 의미하지 않는다. 이후 승인된 게임 배포 1건을 수행하며 baseline 조회 부하를 함께 실행한다. 배포 시작/완료·단계별 실패·조회 지연·호스트 자원 변화를 기록한다. 반복 배포 성공률 측정은 배포 담당자와 횟수·정리 방법을 정한 뒤 진행한다.

게임은 HTTP 접속 외에 두 플레이어 입장·클릭·승패 저장을 확인한다. `sky.probe` nonce 왕복은 앱 메시지 처리 확인이며 게임 전체 기능 확인을 대신하지 않는다. 실제 게임 규약에 맞춘 `game.js`의 프로브·2인 플레이 시나리오를 추가했다. 실행 조건과 통합 모니터링 구성은 [모니터링 README](../../ops/monitoring/README.md)를 참고한다.

## 기록할 내용

- 커밋/이미지 태그, 실행 시각, 프로필, 실제 요청 수, 작업 개수.
- p95/p99, 오류율, dropped iterations, CPU/메모리/디스크 변화.
- 로그인 세션/태스크 재시작 여부, 오류 원인, 개선 후 같은 조건으로 재측정한 결과.
- 로컬 검증과 AWS 실측을 구분한다. 지연 임계값 통과만으로 최대 사용자 수를 추정하지 않는다.

모니터링 항목은 [서비스 모니터링 준비](../../docs/service-monitoring.md)에 정리했다.

## 준비 단계 검증 (2026-10-10)

`grafana/k6:2.3.0`으로 VM의 외부 통신이 차단된 Docker 네트워크에서 검증했다. Sky 소스는 `32c94e1b6d6633d8164786b009be2a783acde413`이며 작업 목록이 비어 있는 HTTP 서버를 사용했다. AWS/OpenAI 키, 호스트 Docker 소켓, 외부 공개 포트는 연결하지 않았다. 서비스 볼륨 초기화·실제 배포·Cognito는 검증 범위에서 제외했다.

| 확인 항목 | 결과 |
| --- | --- |
| smoke | 사전 확인 3회 + 조회 5회, 조회 오류 0 |
| 짧은 baseline (2회/초, 10초) | 사전 확인 3회 + 조회 21회, 조회 오류 0, dropped iterations 0 |
| ramp/soak | k6 설정 해석 통과. 장시간 부하 실행은 하지 않음 |
| 잘못된 입력 | 알 수 없는 프로필·범위 초과 rate/duration·잘못된 작업 경로 거부 |
| 인증/대상 오류 | 잘못된 Sky 토큰·Sky가 아닌 임시 HTML 페이지 거부 |

arrival-rate의 경계 시점 스케줄링 때문에 10초 설정에서 21회가 실행됐다. 실제 부하량은 결과의 iteration/요청 수로 기록한다. 위 결과는 스크립트 동작 확인이며 AWS 처리량·수용 인원·다중 사용자 게임 성능의 근거로 사용하지 않는다. `JOB_ID` 상세 조회와 Cognito 쿠키 연결은 실제 작업·로그인 세션으로 추가 확인해야 한다.
