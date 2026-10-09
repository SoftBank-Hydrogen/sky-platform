# Sky · 게임 모니터링

## 지금 연결할 수 있는 것

Sky 소스 `8969fd8`, 게임 소스 `8b5f57b`를 확인했다. 새 `sky-infra`에는 아직 브랜치/코드가 없으므로 실제 AWS 리소스와 IAM 권한은 연결하지 않았다. 이전 인프라를 수정하거나 Terraform apply를 실행하지 않는다.

게임에는 `/stats`, `/api/scoreboard`, `/ws`의 `sky.probe`가 이미 있다. Sky에는 `/health`, 화면의 세션 토큰, `/api/jobs`가 있다. 먼저 이 정보를 별도 수집기로 읽어서 Prometheus에 저장한다. 앱 코드를 수정할 필요가 없다.

```text
Sky /health + /api/jobs ─┐
게임 /stats + 점수판 + WS ├─ observer → Prometheus ─┐
                        ┘                        ├─ Grafana
AWS ECS·ALB·EC2 지표/로그 ──── CloudWatch ────────┘
```

수집기는 30초마다 조회하고 Prometheus는 15초마다 수집한다. WebSocket은 join 없이 난수 nonce를 보내고 같은 nonce가 5초 이내에 돌아오는지 확인한다. 연결 수립 시간까지 포함한 총 한도는 10초다. 게임 참가·탭·배포 요청을 보내지 않는다. 단, 기존 게임의 메시지 카운터에는 프로브 메시지도 포함된다.

## 구성 파일

- `compose.yml`: 별도 모니터링 서버에서 observer·Prometheus·Grafana 실행.
- `targets.example.json`: Sky와 게임 API 접속 주소를 지정하는 예시.
- `exporter/`: 기존 API를 읽어 지표로 변환하는 수집기.
- `prometheus/`: 수집 설정과 초기 알람 규칙.
- `grafana/`: 데이터 소스와 19개 패널 대시보드 자동 등록.
- `cloudwatch-read-policy.json`: AWS 지표/로그 조회용 권한 초안. 실제 역할에 아직 적용하지 않았으며 최종 범위를 인프라 담당자와 검토한다.

수집 대상은 운영자가 지정한다. 업로드 앱이 지정한 임의 URL을 자동 등록하지 않는다. 새 게임 배포 후 작업 ID·대상 ECS 서비스·접속 주소를 확인하고 `targets.json`을 수정한 뒤 observer를 재시작한다. 자동 대상 등록은 후속 작업이다. 대상 이름은 고정하고 작업 ID·사용자 ID를 매번 Prometheus label로 추가하지 않는다.

## 시작 방법 (Linux/VM)

현재 실제 주소가 없어 운영용 스택은 실행하지 않았다. 아래 설정은 인프라 확인 후 한다. 저장소의 `ops/monitoring` 디렉터리에서 실행한다.

```sh
cp targets.example.json targets.json
mkdir -p secrets
chmod 755 secrets
```

`targets.json`의 예시 주소를 실제 Sky와 **게임 API** 주소로 바꾼다. 게임의 Unity 화면 주소와 API 주소가 다르면 API 주소를 넣는다. 게임 화면·WASM 로딩은 이 수집기의 `/stats` 검사와 별도로 확인해야 한다. 원본 서버는 루프백에만 바인딩하므로 배포 후 실제 외부/내부 접근 경로를 검증한다.

Grafana 초기 비밀번호는 출력하지 않고 파일로 만든다. 아래는 Docker를 관리할 수 있는 Linux 호스트 관리자 권한 기준이며 파일 소유자는 Grafana 이미지의 UID 472다.

```sh
python3 - <<'PY'
from pathlib import Path
import os, secrets
path = Path('secrets/grafana_admin_password')
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
with os.fdopen(descriptor, 'w') as file:
    file.write(secrets.token_urlsafe(32))
os.chown(path, 472, 0)
path.chmod(0o400)
PY
docker compose -f compose.yml config --quiet
docker compose -f compose.yml up -d --build
```

파일이 이미 있으면 생성 명령은 실패하며 기존 비밀번호를 덮어쓰지 않는다. 비밀번호는 본인 터미널에서만 확인한다. 초기 비밀번호 파일을 바꾸는 것만으로 기존 Grafana DB의 비밀번호가 변경되지는 않는다.

Grafana는 VM의 `127.0.0.1:3000`에만 열린다. 로컬에서 SSH 포워딩 후 `http://127.0.0.1:3000`으로 접속한다. 계정명은 `sky-admin`이다.

```sh
ssh -N -L 3000:127.0.0.1:3000 kt_proj_ro1
```

Prometheus와 수집기는 호스트에 포트를 공개하지 않는다. 모니터링 서버에서 대상 API로 접근할 수 있어야 한다. HTTPS/WSS를 우선 사용하고 인증서를 검증한다. 내부 HTTP를 쓸 경우 폐쇄된 네트워크로 제한한다. 앱에 `/metrics`를 인터넷으로 공개할 필요는 없다. VM이 꺼지면 수집도 중단되므로 상시 관찰에는 계속 켜져 있는 별도 호스트가 필요하다.

## Cognito와 AWS 인증

Cognito 보호가 있으면 사용자 본인이 정상 로그인한 뒤 Cookie 헤더를 `secrets/sky_cookie`에 저장한다. observer의 UID 1000이 읽을 수 있도록 소유권을 맞추고 파일 권한을 400으로 제한한다. 파일 내용은 Git·로그·채팅에 남기지 않는다. Cognito가 없으면 target의 `cookieFile` 항목을 제거한다. 쿠키 만료 시 수집 실패로 표시되므로 이 방식은 초기 시연용이다. 운영에서는 인프라 담당자와 내부 수집 경로/서비스 인증을 별도로 마련한다. 인증 보호를 끄거나 공개 우회 경로를 만들지 않는다.

Grafana의 CloudWatch 데이터 소스는 기본 AWS 인증 체인을 사용한다. AWS에 둘 경우 조회 전용 IAM 역할을 우선 사용한다. 현재 로컬 VM에 AWS 자격 증명을 임의로 추가하지 않는다. VM에서 사용한다면 만료되는 별도 프로필/세션을 보호된 파일로 연결하는 방식을 확인한다. 기본 compose에는 AWS 키가 없다.

대시보드의 `region`, `sky_cluster`, `sky_service`, `game_cluster`, `game_service`를 실제 값으로 바꾼다. ECS가 아닌 게임 배포 대상에는 해당 ECS 패널을 적용하지 않는다. 이후 실제 리소스에 맞춰 ALB 요청/5xx/p95, EC2 호스트 지표를 추가한다. CloudWatch Logs는 Grafana Explore에서 실제 로그 그룹을 선택해 조회한다. 아직 로그 그룹 고정 패널과 AWS 알람은 생성하지 않았다.

## 지표 해석

| 지표 | 의미 / 한계 |
| --- | --- |
| `up{job="sky-observer"}` | Prometheus가 수집기에 접근했는지. 앱 정상 여부와 다름 |
| `sky_observed_collection_up` | 대상 응답·인증·형식을 읽을 수 있는지. 0이면 인증 만료도 조사 |
| `sky_observed_collection_error{reason}` | 인증·HTTP·응답 형식·타임아웃·네트워크·쿠키 파일 오류를 고정된 분류로 표시. 원문 오류/자격 증명은 내보내지 않음 |
| `sky_observed_http_up` | Sky /health 또는 게임 /stats 정상 여부. 웹 화면 전체 로딩 보장 아님 |
| `sky_observed_jobs{status}` | 현재 보관된 작업 수 gauge. 상태 변경·삭제에 따라 감소하므로 rate()를 쓰지 않음 |
| `sky_observed_websocket_up` | 게임 앱 코드의 nonce 왕복 성공. 모든 게임 기능 검증 아님 |
| `sky_game_players` | 게임 참가자 수. probe는 참가하지 않음 |
| `sky_game_messages`, `sky_game_taps_accepted` 등 | 게임 프로세스 수명 동안의 counter. 재시작 시 초기화되며 rate()가 reset을 처리 |
| `sky_game_scoreboard_up`, `sky_game_stored_rounds` | 점수판 읽기 성공·보관 라운드 수. 쓰기와 재시작 후 영속성은 별도 기능 검증 |
| `sky_observed_last_success_timestamp_seconds` | 마지막 수집 성공 시각. 오래된 데이터와 정상 상태를 구분 |

게임 통계·점수판·WebSocket은 각각 검사한다. `/stats` 실패나 형식 오류만으로 WS/점수판을 실패로 표시하지 않는다. 쿠키 파일 오류처럼 WS 검사를 시작하지 못한 경우에는 WS 결과를 내보내지 않고 수집 실패 원인을 표시한다.

수집이 실패하면 이전 앱 지표를 재사용하지 않는다. 무응답·잘못된 JSON·토큰 실패를 정상 0명/0건으로 표시하지 않는다. 작업 성공률과 단계별 시간은 아직 이벤트 기반 지표가 없으므로 별도 코드 작업이 필요하다. 게임 메시지 처리 시간·DB 쓰기 오류도 기존 API에서 제공하지 않아 아직 수집하지 않는다.

알람 규칙은 Prometheus에서 평가하지만 Alertmanager/SNS 등 수신 연결은 아직 없다. 규칙 등록만으로 팀에 알림이 발송되지 않는다. 중단·수집 실패·WS 실패·점수판 실패를 먼저 평가하고, 인프라 알람/수신자는 실제 환경 연결 단계에서 정한다.

Grafana 데이터는 named volume에 저장하고 Prometheus 보관은 7일/1GB로 제한한다. OSS 설치에 라이선스 비용은 없지만 VM/스토리지·CloudWatch 로그/쿼리·Container Insights 등 비용은 별도다. 필요한 지표만 켜고 실제 데이터량으로 비용을 확인한다.

## 게임 k6

저장소 루트의 `scripts/load/game.js`는 기본적으로 프로브 1회만 실행한다.

```sh
docker run --rm --read-only --cap-drop ALL \
  -v "$PWD/scripts/load:/scripts:ro" \
  -e GAME_HTTP_URL=https://YOUR-GAME-API-HOST \
  -e GAME_WS_URL=wss://YOUR-GAME-API-HOST/ws \
  grafana/k6:2.3.0 run /scripts/game.js > game-probe.json
```

`GAME_MODE=play ALLOW_GAME_INPUT=true`는 **게임과 DB를 변경**한다. 실제 플레이어가 없는 테스트 게임에서만 실행한다. 2 VU가 각각 참가하고 플레이 중 초당 5회 탭을 보내며 45초 동안 한 라운드 결과와 DB 저장 라운드 증가를 확인한다. 서버 한 방을 공유하므로 2 VU는 두 명의 플레이어이며 방 두 개가 아니다. 탭은 1~10회/초로 제한한다. 부하 규모를 늘리는 프로필은 정상 시나리오 검증 후 추가한다.

게임 서버는 Origin 허용 목록을 검사한다. 기본 k6/probe는 Origin을 보내지 않아 서버 프로토콜을 확인하며, 실제 브라우저 접근 가능 여부를 증명하지 않는다. 필요하면 `GAME_ORIGIN`/target의 `origin`에 실제 프런트엔드 Origin을 넣고 별도로 브라우저 검증한다.

k6 결과는 현재 JSON 요약이며 Grafana에 실시간 전송하지 않는다. 실시간 통합은 결과 전송 방식과 보관 정책을 정한 뒤 추가한다. 테스트 시작/종료 시각을 기록해 서버 지표와 비교한다.

## 다음 연결 작업

1. 새 infra 업로드와 실제 Sky 이미지 배포 여부 확인.
2. Sky/게임 API·WS·프런트엔드 주소 및 인증 경로 확인.
3. 모니터링 호스트의 접근 경로·조회 전용 AWS 역할·로그 그룹 연결.
4. 두 서버의 접속 검사와 낮은 부하 실측.
5. 배포 단계별 시간·게임 처리 지연 지표, ALB/EC2 패널, 알림 발송 및 k6 실시간 결과 연결.

참고: [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/), [CloudWatch 데이터 소스](https://grafana.com/docs/grafana/latest/datasources/aws-cloudwatch/), [Prometheus 설정](https://prometheus.io/docs/prometheus/latest/configuration/configuration/).

## 준비 단계 검증 — 2026-10-10

외부 통신이 차단된 VM Docker 네트워크에서 최신 Sky 소스와 게임 서버를 실행했다. AWS/OpenAI 키·호스트 Docker 소켓·공개 포트를 연결하지 않았다. 테스트 컨테이너와 네트워크는 종료 후 정리했다.

| 항목 | 결과 |
| --- | --- |
| 수집기 단위 테스트 | 4개 통과. 잘못된 대상·인증·응답 형식·쿠키 파일, nonce 검증·비참가 확인 |
| Compose·Prometheus 설정 | 구문 검사 통과, 알람 규칙 6개 로드 |
| 알람 동작 | 수집기/WS 장애 시 발동, 정상 시 비발동 오프라인 테스트 통과 |
| 실제 API 수집 | Sky 작업 목록·게임 통계·점수판·프로브 수집, Prometheus 조회 확인 |
| Grafana | 두 데이터 소스와 실패 원인을 포함한 19개 패널 등록 API 확인. 화면 렌더링과 AWS 쿼리는 미검증 |
| k6 Sky smoke | 조회 5회, 오류 0 |
| k6 게임 probe | 1세션, 오류 0 |
| k6 게임 play | 2세션, 오류 0. welcome/result 및 저장된 라운드 증가 확인 |
| k6 게임 실패 처리 | 잘못된 메시지 형식과 라운드 결과 직후의 조기 연결 종료를 실패로 판정 |

게임 DB는 테스트 컨테이너의 임시 SQLite 파일이다. 위 결과는 실제 AWS 배포 성공·재시작 후 DB 영속성·최대 동시 사용자 수를 증명하지 않는다. Cognito 로그인, 실제 프런트엔드 Origin/Unity 로딩, AWS 지표·로그, 알림 발송은 실제 환경 연결 후 검증한다.

`.github/workflows/monitoring.yml`은 수집기 테스트·대시보드 구조·Prometheus 알람·k6 설정 해석을 자동 검사한다. 실제 서비스 부하나 AWS 호출은 실행하지 않는다. 게임 테스트는 잘못된 메시지 형식과 45초 이전의 비정상 연결 종료도 실패로 처리한다.
