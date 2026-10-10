# ECR 이미지에서 인프라 배포 PR까지

main CI: 테스트 → 서비스 이미지 빌드·ZIP smoke → ECR push → 인프라 이미지 태그 변경 PR.
PR 실행은 이미지를 publish하거나 인프라 PR을 만들지 않는다.
ECR 업로드 실패·미설정으로 publish-service가 생략되면 다음 작업도 실행하지 않는다.
인프라 PR 단계는 `SKY_INFRA_IMAGE_PR_ENABLED=true`가 명시적으로 설정됐을 때만 실행한다.
계정과 ECR 접근 경로가 아직 다르면 이 값을 설정하지 않는다.

propose-infra-image는 sky-infra의 terraform/envs/dev/platform-image.auto.tfvars에서
platform_image_tag 한 줄만 7자리 SHA로 바꾼다. 원본 전체 SHA는 PR 본문과 브랜치에 남는다.
팀원이 인프라 PR을 머지하면 기존 deploy.yaml이 ECR 이미지 존재·Terraform 변경 범위를
검사하고 API·워커 배포를 진행한다. 자동 머지는 하지 않는다.

## 최초 설정

sky-platform → Settings → Environments → dev → Environment secrets에
SKY_INFRA_PR_TOKEN을 등록한다. 저장소 Actions secret으로 등록해도 사용할 수 있다.
그다음 이미지가 게시되는 AWS 계정에서 sky-infra 배포 계정이 해당 ECR 이미지를
읽을 수 있는지 확인하고, 저장소 Actions variable `SKY_INFRA_IMAGE_PR_ENABLED`를
`true`로 설정한다. 토큰만 등록하거나 ECR 게시만 성공했다고 자동으로 켜지지 않는다.

Fine-grained PAT 설정:
- Resource owner: SoftBank-Hydrogen
- Repository access: Only select repositories → sky-infra
- Repository permissions: Contents Read and write, Pull requests Read and write
- Metadata는 자동 Read. Workflows나 AWS 권한은 필요하지 않다.
- 만료일을 정하고 조직 승인 필요 시 승인까지 완료한다.

기본 GITHUB_TOKEN은 다른 저장소 쓰기에 사용하지 않는다.
플랫폼 main 조회는 별도의 읽기 전용 GITHUB_TOKEN을 사용한다.
PAT 값은 채팅·Git·PR 본문에 남기지 않는다.
향후 동일 권한의 GitHub App 설치 토큰으로 교체할 수 있다.
옵트인 후 토큰이 없거나 유효하지 않으면 ECR 업로드 다음 PR 생성 작업이 명확히 실패한다.

## 재실행·동시 변경

전체 SHA별 브랜치를 사용하고 동일 실행을 다시 돌리면 열린 PR을 재사용한다.
인프라 main이 이미 해당 태그면 아무것도 바꾸지 않는다.
닫힌 PR을 임의로 다시 만들거나 관련 없는 파일 변경을 덮어쓰지 않는다.
main 최신 SHA와 다른 실행은 건너뛴다. PR 준비 중 main이 바뀌었으면 PR을 생성하지 않는다.
이 검사는 조회 시점 기준이며 PR 생성 직후나 검토 중 더 최신 버전이 생길 수 있으므로
팀원이 머지 전에 최신 배포 대상을 확인해야 한다. 기존 다른 버전 PR을 자동 종료하지 않는다.

운영 DB·AWS 리소스에 직접 변경을 보내지 않는다.
실제 ECR 업로드 및 인프라 plan/deploy에는 각 저장소의 OIDC 신뢰 정책,
동일 AWS 계정·ECR 설정, B안 서비스 실행 준비가 별도로 필요하다.
이미지 PR 생성은 API·워커가 운영 준비를 마쳤다는 보장이 아니다.

## 검증

tests/test_infra_image_pr.py는 GitHub API 대역으로 PR 생성, 재실행, 실패 및 변경 범위를 확인한다.
pytest는 실제 인프라 PR이나 AWS 리소스를 만들지 않는다.
실제 토큰·ECR push → 자동 PR 생성은 시크릿 등록과 OIDC 문제 해결 후 확인한다.

## 현재 연결 상태 (2026-10-10 확인)

플랫폼 저장소 기본 AWS_ACCOUNT_ID는 265233844540이고, 인프라는 977889523182이다.
플랫폼 dev 환경에는 이 값을 덮어쓰는 변수가 없었다.
계정이 다르면 같은 7자리 태그를 기록해도 인프라가 조회하는 ECR에 이미지가 없을 수 있다.
팀에서 서비스 운영 계정을 정한 뒤 AWS_ACCOUNT_ID, AWS_SERVICE_CI_ROLE_ARN,
AWS_REGION 및 ECR 저장소를 같은 배포 대상으로 맞춰야 한다.
기존 인프라 OIDC 신뢰 문제와 이 설정은 이번 PR에서 변경하지 않는다.
