"""Disclose likely cost drivers without inventing an unverified price quote."""

_DRIVERS = {
    "local-docker": ("현재 PC의 CPU·메모리·전력·디스크",),
    "onprem-compose": ("같은 호스트의 CPU·메모리·전력·디스크",),
    "onprem-vm": ("원격 VM의 CPU·메모리·전력·디스크", "VM 네트워크 전송량"),
    "aws-ecs-express": (
        "ECS Fargate 실행 시간과 요청 자원",
        "로드 밸런서 사용량",
        "ECR 이미지 저장량",
        "로그 및 외부 데이터 전송량",
    ),
    "cloud-run": (
        "Cloud Run 실행 자원·요청",
        "Artifact Registry 이미지 저장량",
        "로그 및 외부 데이터 전송량",
    ),
    "aws-s3-cloudfront": (
        "S3 파일 저장량·요청",
        "CloudFront 요청·전송량",
        "새 릴리스 검증 중 이전 릴리스와 겹치는 자원",
    ),
}


def cost_exposure(target: str, *, database_required: bool = False) -> dict:
    """Return categories only; usage, region, account discounts and taxes are unknown."""
    try:
        drivers = list(_DRIVERS[target])
    except KeyError as exc:
        raise ValueError("지원하지 않는 비용 대상입니다.") from exc
    if target == "aws-ecs-express" and database_required:
        drivers.append("선택한 신규·기존 RDS의 실행·저장·백업")
    return {
        "estimate": None,
        "drivers": drivers,
        "note": "사용량·리전·계정 할인·세금·기존 자원을 확인하지 않아 총액은 미산정입니다.",
    }
