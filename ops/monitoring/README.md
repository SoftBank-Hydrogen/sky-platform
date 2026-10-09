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

- `compose.yml`: 별도 모니터링 서버에서 observer·Prometheus·Grafana·Alertmanager 실행. 기본 설정은 외부 알림 비활성화.
- `targets.example.json`: Sky와 게임 API 접속 주소를 지정하는 예시.
- `exporter/`: 기존 API를 읽어 지표로 변환하는 수집기.
- `prometheus/`: 수집 설정과 초기 알람 규칙.
- `grafana/`: 데이터 소스와 자동 발견 상태를 포함한 21개 패널 대시보드 자동 등록.
- `alertmanager/`, `compose.slack.yml`: #sky-alerts 알림 설정. Webhook 준비 후 별도로 활성화.
- `cloudwatch-read-policy.json`: AWS 지표/로그 조회용 권한 초안. 실제 역할에 아직 적용하지 않았으며 최종 범위를 인프라 담당자와 검토한다.

수동 대상은 운영자가 지정한다. `targets.discovery.example.json`처럼 Sky 대상에 `discovery`를 설정하면 성공했고 현재 active인 게임 배포를 자동 발견한다. 업로드 앱이 지정한 임의 URL을 자동 신뢰하지 않고 운영자가 정한 호스트 허용 목록을 적용한다. 대상 이름은 앱/배포 환경별로 유지하며 작업 ID·사용자 ID를 매번 Prometheus label로 추가하지 않는다.

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

알람 규칙은 Prometheus에서 평가하고 Alertmanager로 전달한다. 기본 receiver는 외부 전송이 비활성화된 상태이며 Slack 연결은 아래 override로 명시적으로 켠다. 규칙 등록만으로 Slack에 메시지가 발송되지 않는다. CloudWatch 자체 알람의 Slack 연결은 아직 포함하지 않았다.

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

## 게임 자동 등록

1. Sky의 인증된 `/api/jobs`에서 성공·active 상태를 읽는다.
2. 해당 작업 상세에서 `sky-probe-protocol` 계약과 배포 주소를 확인한다.
3. 운영자가 지정한 `allowedHosts`와 일치하는 주소만 게임 대상으로 등록한다.
4. 재배포 시 같은 앱/배포 대상의 주소를 갱신하고 superseded/deleted 등 비활성 배포는 제거한다.
5. Sky 조회 실패·인증 만료 때는 기존 대상을 유지하고 `sky_discovery_up=0`을 표시한다.

최초에 허용할 호스트와 Sky 접속 정보를 한 번 설정하면 이후 주소 변경마다 파일을 편집하거나 재시작할 필요가 없다. 정상 상태의 등록 반영은 대략 한 번의 poll 주기(기본 30초)다. 현재 게임의 `/stats`·점수판·`/ws` 계약에 맞춘 기능으로, 일반 웹 앱을 게임으로 등록하지 않는다. 자동 등록은 기본적으로 꺼져 있다.

`applicationIds`는 선택 사항이다. 특정 게임만 관찰하려면 안정적인 앱 ID를 지정하고, 동일 계약의 새 게임도 모두 발견하려면 항목을 제거한다. 앱 ID가 유지될 때만 재배포 후 지표 이름도 유지된다. 익명 단발 배포는 작업 ID가 앱 식별자 역할을 하므로 별도 대상으로 보일 수 있다.

`allowedHosts`는 소문자 exact hostname 또는 `*.example.com` 형식이다. 루프백/메타데이터/다른 호스트는 명시적으로 허용하지 않으면 등록되지 않는다. 온프렘이 `127.0.0.1` 주소만 제공하면 모니터링 서버에서 접근 가능한 주소가 아니므로 터널/내부 주소를 먼저 확정한다. 쿠키와 인증 헤더는 Sky에서 게임으로 전달하지 않는다. 인증이 필요한 게임은 별도 수동 대상으로 설정한다.

전체 수집 대상은 최대 20개이며 자동 발견 소스가 여러 개면 남은 자리를 균등 배분한다. 용량 초과·호스트 거부는 `sky_discovery_rejected`와 알람으로 표시한다. AWS ECS CPU·메모리 패널의 리소스 선택은 아직 자동 등록하지 않으며 실제 리소스 정보/조회 권한을 연결할 때 추가한다.

## Slack 알림 — #sky-alerts

경로는 `Prometheus → Alertmanager → Slack Incoming Webhook`이다. 장애/복구를 보내고 같은 종류·대상의 알람을 묶는다. 기존 규칙의 1~2분 지속 조건 이후 최초 집계는 30초, 변경된 그룹 알림은 5분, 계속되는 동일 장애의 재알림은 4시간 기준이다.

Slack 앱의 Incoming Webhook을 **#sky-alerts에 연결**한다. 채널 생성과 앱 설치에는 해당 워크스페이스 권한이 필요하다. 최신 Incoming Webhook은 생성 때 선택한 채널에 묶이므로 설정 파일의 channel 값만 바꿔 다른 채널로 전환하지 않는다. Webhook URL은 비밀이며 채팅/Git에 넣지 않는다.

호스트 관리자 권한으로 아래 명령을 실행해 URL을 숨겨 입력하고 Alertmanager UID 65534만 읽도록 저장한다. 기존 파일은 덮어쓰지 않는다.

```sh
python3 - <<'PY'
import os, getpass
from urllib.parse import urlsplit
url = getpass.getpass('Slack Webhook URL: ').strip()
parsed = urlsplit(url)
if (parsed.scheme != 'https' or parsed.hostname != 'hooks.slack.com'
    or parsed.username or parsed.password or parsed.query or parsed.fragment
    or not parsed.path.startswith('/services/')):
    raise ValueError('Slack Incoming Webhook URL이 필요합니다.')
descriptor = os.open('secrets/slack_webhook', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
with os.fdopen(descriptor, 'w') as file:
    file.write(url)
os.chown('secrets/slack_webhook', 65534, 65534)
PY
docker compose -f compose.yml -f compose.slack.yml config --quiet
docker compose -f compose.yml -f compose.slack.yml up -d alertmanager
```

**마지막 실행 명령부터 실제 알림이 전송될 수 있다.** 이후에도 같은 두 Compose 파일을 사용해 운영한다. 기본 파일만 다시 적용하면 외부 알림은 비활성화된다. Webhook 갱신 시 기존 파일을 안전하게 교체하고 Alertmanager를 재시작한다.

메시지에는 장애/복구 상태, 알람명, 심각도, 대상, 요약을 넣는다. 배포 코드·URL 토큰·쿠키는 포함하지 않는다. Alertmanager가 보낼 수 없는 상황은 전송 실패 지표/로그와 외부 모니터링으로 별도 확인해야 한다.

실제 Slack 전송은 아직 실행하지 않았다. 로컬 모의 Webhook으로 형식과 장애/복구 흐름을 검증하며 실제 채널 수신 확인은 Webhook 준비 후 별도로 한다. 참고: [Slack Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/), [Alertmanager 설정](https://prometheus.io/docs/alerting/latest/configuration/).

### 자동 등록·알림 추가 검증 — 2026-10-10

외부 통신이 없는 Docker 네트워크에서 배포 목록은 모의 Sky API, 게임은 실제 게임 서버 소스로 검증했다. 실제 AWS 배포 연동 검증과는 구분한다.

- 수집기·자동 발견 단위 테스트 7개 통과. 재배포 시 이름 유지, 비활성 제외, 허용되지 않은 주소 거부, 인증 정보 미전달 확인.
- 성공·active 게임 자동 등록과 통계 수집, Sky 조회 장애 시 기존 대상 유지, 삭제 후 지표 제거 확인.
- Prometheus 설정·9개 알람 구문 검사, 새 알람 3개의 장애 발동·정상 비발동 테스트 통과.
- Alertmanager 두 설정과 Compose 결합 검사 통과. 모의 Webhook에서 #sky-alerts 장애·복구 메시지 수신 확인.
- 21개 대시보드 패널의 ID·데이터 소스·쿼리 구조 검사 통과. 추가 패널의 Grafana 등록/화면은 실제 스택 연결 후 확인한다.

위 모의 Webhook 형식 테스트는 Alertmanager에 합성 알람을 직접 전달했다. 이후 아래 전체 흐름 검증을 추가했다. 실제 Slack 수신과 CloudWatch 자체 알람의 Slack 전송은 아직 검증하지 않았다.

### 알림 전체 흐름 검증 — 2026-10-10

외부 통신이 차단된 별도 Docker 네트워크에서 실제 게임 서버 소스·수집기·Prometheus·Alertmanager·모의 Webhook을 연결했다. 배포 목록은 모의 Sky API가 제공했으며, 수집기 이미지는 현재 소스와 SHA256이 일치했다.

1. 자동 등록된 게임의 WebSocket 정상 지표, Prometheus 수집, Alertmanager 발견을 확인했다. 게임 WS 장애 알람이 발동하지 않는 것도 확인했다.
2. 해당 테스트 게임 컨테이너만 중단했다. 수집기 WS 지표가 0이 되고, Prometheus의 GameWebSocketUnavailable이 pending → firing으로 바뀌었다. 운영 규칙의 `for: 2m`는 그대로 사용했다.
3. Prometheus가 생성한 알람이 Alertmanager를 거쳐 모의 #sky-alerts에 FIRING 메시지로 도착했다. Alertmanager API에 알람을 직접 주입하지 않았다. 장애 중 게임 대상은 제거되지 않았다.
4. 게임 컨테이너를 재시작했다. WS 지표가 정상으로 돌아오고 Prometheus 알람이 해제된 뒤 같은 대상의 RESOLVED 메시지를 수신했다.

이번 한 번의 격리 실행에서는 중단 후 장애 메시지까지 약 2분 32초, 재시작 후 복구 메시지까지 약 28초였다. 테스트에서만 group_wait=1s, group_interval=5s로 줄였고 수집·평가는 15초 주기였다. 운영 설정은 group_wait=30s, group_interval=5m, 수집기 30초 주기이므로 위 시간을 운영 알림 지연이나 성능 보장으로 사용하지 않는다.

테스트 컨테이너·네트워크는 종료 후 정리했다. 공개 포트·호스트 Docker 소켓·AWS 자격 증명·실제 Slack Webhook은 컨테이너에 연결하지 않았다. 실제 환경의 인증·네트워크·Slack 수신은 별도 검증이 필요하다. 알림 기본 상세 링크는 내부 Alertmanager 주소이므로 운영 연결 때 접근 가능한 대시보드 링크를 확정한다.
