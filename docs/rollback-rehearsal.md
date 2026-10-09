# 롤백 리허설 준비

`scripts/rollback_rehearsal.py`는 Sky가 관리하는 테스트 앱의 기존 릴리스 두 개를 사용한다. 현재 B가 실행 중이고 이전 A가 superseded인 상태에서 B → A 롤백, A 상태 확인, A → B 복귀, B 상태 확인을 수행한다. 앱 업로드·빌드·인프라 생성·삭제는 하지 않는다. 서비스 서버 자체의 sky-dev-platform 배포 롤백이 아니라 **Sky로 배포한 사용자 테스트 앱**의 AWS ECS Express 릴리스 검증이다.

## 실행 전

- 새 리허설 전용 앱 ID를 `rollback-demo-` 접두어로 만든다(전체 31자 이하). 기존 Sky 배포 흐름으로 정상 버전 A와 B를 순서대로 배포한다.
- B는 succeeded/active, A는 succeeded/superseded이며 같은 앱·AWS 계정·리전·서비스·주소를 사용해야 한다. 두 이미지가 달라야 하고 A 태스크 정의와 이미지가 남아 있어야 한다.
- 현재 버전 B가 정상일 때만 시작한다. 사용자가 접속하는 실제 게임에서 실행하지 않는다. 롤백으로 WebSocket 연결 및 서버 메모리의 판 상태가 끊길 수 있다.
- 같은 앱에 다른 배포·수동 롤백을 동시에 실행하지 않는다. 이 스크립트는 앱 전체 작업 잠금을 새로 만들지 않으며 기존 서버의 요청별 보호를 사용한다.
- Sky Cognito 로그인 쿠키가 필요하면 별도 보호 파일로 준비한다. 비밀번호·쿠키·토큰을 명령 인수나 Git에 넣지 않는다. 쿠키 만료 시 자동 재로그인하지 않는다.
- Python 3.12 이상이 필요하다. 추가 패키지나 직접 AWS 액세스 키를 요구하지 않는다. AWS 권한은 Sky 서비스의 기존 배포 역할이 사용한다.

## 먼저 조회만

저장소 루트에서 예시 값을 실제 테스트 앱의 정보로 바꿔 실행한다. `--execute`가 없으면 작업 상세 조회와 선행 조건 검사만 하며 상태 검사 기록·WebSocket 프로브·롤백 요청을 보내지 않는다.

```sh
python scripts/rollback_rehearsal.py \
  --sky-url https://cloudas.store \
  --cookie-file /PROTECTED/PATH/sky_cookie \
  --application-id rollback-demo-tug \
  --account YOUR_TEST_ACCOUNT_ID --region ap-northeast-2 \
  --current-job CURRENT_B_JOB_ID --previous-job PREVIOUS_A_JOB_ID \
  --output rehearsal-plan.json
```

출력 파일은 기존 파일을 덮어쓰지 않는다. 잘못된 계정·앱·리전·서비스·이미지·상태는 거부한다. 원격 HTTP와 URL 안의 인증 정보는 허용하지 않고, 리디렉션으로 쿠키를 다른 주소에 보내지 않는다. 로컬 API 테스트에만 HTTP 루프백 주소를 허용한다.

## 실제 테스트 앱에서 실행

실제 배포 버전을 변경하므로 테스트 앱과 환경이 확인된 뒤 실행한다. 위 명령의 출력 파일을 새 이름으로 바꾸고 아래 인수를 추가한다.

```sh
--execute --confirm-application rollback-demo-tug --require-websocket
```

`--require-websocket`은 게임의 `sky.probe.v1` 계약 검증까지 요구한다. 이 경우 두 릴리스에 sky-probe-protocol 계약이 있어야 한다. 옵션을 빼면 HTTP·릴리스 검증만 하며 결과에 WS를 `not_requested`로 기록한다. 게임 참가나 탭 입력은 하지 않는다.

각 전환은 기본 30분까지 기다린다(`--timeout` 30~3600초). 요청 오류 때 롤백 POST를 자동 재시도하지 않는다. 이전 A의 상태 검사가 실패해도 A가 활성화된 사실이 확정됐으면 B 복귀를 시도한다. 진행 중·needs_attention·API 연결 실패 등 결과가 불명확할 때는 추가 변경을 멈추고 `restoration: needs_attention`을 남긴다. 실행 결과를 확인하지 않고 재실행하지 않는다. 프로세스 강제 종료·호스트 장애 때의 복귀는 보장하지 않는다.

## 결과 해석

- `planned`: 조회·선행 조건 검사 통과. 실제 롤백 검증 아님.
- `passed`: B → A → B 전환 증거, HTTP 및 요청한 WS 검사 통과. 원래 B의 이미지·태스크 정의 복귀 확인.
- `failed`: 단계 실패. `restoration=verified`면 B 복귀는 확인했지만 리허설은 실패다.
- `restoration=needs_attention`: 현재 상태 확인이 필요하다. Sky 작업 상세·AWS 실제 서비스 상태를 확인하고 기존 reconcile 흐름으로 처리한다.

조회·전환 대기·최종 복귀 확인마다 작업 응답의 필수 필드와 타입을 검증한다. 응답이 누락되거나 잘못된 형식이면 `job_response_schema_invalid` 등 고정된 오류로 보고서에 남긴다. 이후 상태가 정상 응답으로 확인되면 복귀를 시도하되, 복귀 응답도 불완전하면 `restoration=needs_attention`으로 기록하고 성공으로 처리하지 않는다.

보고서에는 작업 ID·검사 단계·소요 시간·복귀 여부만 남기며 쿠키·토큰·응답 원문은 저장하지 않는다. Sky의 `rollback_rehearsal` 인증서 상태를 덮어쓰지 않는 독립 검증 도구다. 게임 판 상태/SQLite·RDS 데이터 보존과 DB 스키마 롤백은 별도 검증해야 한다.

## 준비 단계 검증

테스트는 실제 Sky HTTP 핸들러·세션 토큰·릴리스 상태 전환·저장/재시작을 사용한다. AWS 어댑터와 HTTP/WS 외부 검사만 모의 처리한다. 정상 왕복, 초기 상태 검사 실패, 이전 WS 검사 실패 후 복귀, AWS 결과 불명확 시 중단, 복귀 실패, 앱/계정/리전/이미지 불일치 거부를 검사한다.

이 결과는 실제 ECS 롤백·재접속·DB 데이터 보존을 증명하지 않는다. 실제 AWS 리허설은 아직 실행하지 않았다.

2026-10-10 준비 단계에서 새 테스트 11개를 포함한 전체 오프라인 테스트 710개와 subtest 160개가 통과했다. Ruff 검사·포맷 검사도 통과했다. 기준 main은 `90148776`이며, Windows 줄바꿈 변환을 끈 Git 아카이브에 새 파일을 추가해 Linux 컨테이너에서 검사했다. 컨테이너에는 AWS/OpenAI 자격 증명과 호스트 Docker 소켓을 연결하지 않았고 네트워크는 none이었다.
