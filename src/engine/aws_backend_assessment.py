"""Assess unimplemented AWS options without offering them as executable targets.

Lambda is scoped to a request/event handler, not every Lambda product or adapter.
EC2 is a host-control option; a large app or a database alone never requires it.
Source signals are hypotheses and missing runtime evidence stays unknown.
"""

from __future__ import annotations

from engine.backend_identity import backend_identity
from engine.compatibility import InfrastructureProfile, evidence_id

LAMBDA_REFERENCES = (
    "https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html",
    "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html",
)
HOST_REFERENCES = (
    "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html",
    "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters_ec2.html",
)
UNIMPLEMENTED_AWS_TARGETS = ("aws-lambda", "aws-ec2")


def with_aws_backend_options(profile: InfrastructureProfile, plan: dict) -> dict:
    """Keep comparisons in fixed/static decisions without changing execution eligibility."""
    already_compared = {item["id"] for item in plan.get("candidates", ())}
    return {
        **plan,
        "architecture_options": [
            item for item in aws_backend_assessments(profile) if item["id"] not in already_compared
        ],
    }


def aws_backend_assessments(profile: InfrastructureProfile) -> list[dict]:
    """Return source-bound comparisons. Every result remains unsupported by Sky."""
    signals = dict(profile.source_signals)
    requirements = dict(profile.requirement_evidence)
    checks: dict[str, list[dict]] = {target: [] for target in UNIMPLEMENTED_AWS_TARGETS}

    def check(target: str, rule: str, status: str, kind: str, reason: str) -> None:
        paths = sorted(set(signals.get(kind, ())) | set(requirements.get(kind, ())))
        checks[target].append(
            {
                "rule_id": rule,
                "status": status,
                "requirement": kind,
                "reason": reason,
                "evidence_files": paths,
                "evidence_ids": [evidence_id(kind, path) for path in paths],
            }
        )

    host_signals = [name for name in ("host-kernel-control", "host-device-access") if name in signals]
    lambda_structure = "unknown"
    if host_signals:
        lambda_structure = "incompatible"
        for name in host_signals:
            check(
                "aws-lambda",
                "LAMBDA-HOST-01",
                "violated",
                name,
                "호스트 커널·장치 제어 신호가 있어 요청 핸들러형 Lambda 후보와 충돌합니다.",
            )
    if "background-worker" in profile.requirements or "websocket" in signals:
        lambda_structure = "incompatible"
        for kind in ("background-worker", "websocket"):
            if kind in profile.requirements or kind in signals:
                check(
                    "aws-lambda",
                    "LAMBDA-LIFECYCLE-01",
                    "violated",
                    kind,
                    "지속 워커 또는 장기 연결은 현재 비교하는 요청 핸들러 실행 모델과 다릅니다. "
                    "별도 서비스·이벤트 구조로 바꾸는 판단이 필요합니다.",
                )
    if "persistent-http-server" in signals:
        if lambda_structure != "incompatible":
            lambda_structure = "requires_transformation"
        check(
            "aws-lambda",
            "LAMBDA-ENTRYPOINT-01",
            "unknown",
            "persistent-http-server",
            "상시 HTTP 서버를 그대로 함수 핸들러라고 판단하지 않습니다. "
            "핸들러·웹 어댑터 변환과 요청/응답 계약 검증이 필요합니다.",
        )
    if "function-handler" in signals:
        if lambda_structure == "unknown":
            lambda_structure = "potentially_compatible"
        check(
            "aws-lambda",
            "LAMBDA-HANDLER-01",
            "satisfied",
            "function-handler",
            "함수 핸들러 형태를 소스에서 관찰했습니다. HTTP 이벤트 호환성은 아직 검증되지 않았습니다.",
        )
    else:
        check(
            "aws-lambda",
            "LAMBDA-HANDLER-01",
            "unknown",
            "function-handler",
            "지원 언어·핸들러·호출 이벤트 계약을 확인하지 못했습니다. "
            "DB나 상태 신호가 없다는 이유만으로 Lambda를 추천하지 않습니다.",
        )
    if "sqlite" in profile.requirements or "local-files" in profile.requirements:
        if lambda_structure != "incompatible":
            lambda_structure = "requires_transformation"
        for kind in ("sqlite", "local-files"):
            if kind in profile.requirements:
                check(
                    "aws-lambda",
                    "LAMBDA-DATA-01",
                    "unknown",
                    kind,
                    "로컬 영속 데이터는 외부 저장소 연결 또는 별도 변환 검토가 필요합니다.",
                )
    if "database" in profile.requirements:
        check(
            "aws-lambda",
            "LAMBDA-DATABASE-01",
            "unknown",
            "database",
            "외부 DB는 Lambda를 자동 탈락시키지 않습니다. 연결 수·권한·네트워크와 이전 경로를 검증해야 합니다.",
        )
    for rule, kind, reason in (
        (
            "LAMBDA-DURATION-01",
            "invocation-duration",
            "요청별 실행 시간·시간 제한 적합성은 측정하지 않았습니다.",
        ),
        (
            "LAMBDA-STATE-01",
            "possible-process-local-state",
            "호출 간 프로세스 메모리에 의존하지 않는지는 검증하지 않았습니다.",
        ),
        (
            "LAMBDA-EVENT-01",
            "http-event-contract",
            "HTTP 이벤트·응답 형식과 패키징 계약은 검증하지 않았습니다.",
        ),
    ):
        check("aws-lambda", rule, "unknown", kind, reason)

    ec2_structure = "potentially_compatible" if host_signals else "not_preferred"
    for name in host_signals:
        check(
            "aws-ec2",
            "EC2-HOST-01",
            "satisfied",
            name,
            "호스트 커널·장치 제어 신호가 있어 EC2 기반 실행을 검토할 근거가 있습니다. "
            "컨테이너가 불가능하다는 뜻은 아니며 ECS on EC2도 별도 대안입니다.",
        )
    if not host_signals:
        check(
            "aws-ec2",
            "EC2-NECESSITY-01",
            "unknown",
            "host-control",
            "호스트 제어 필요성을 관찰하지 못했습니다. 앱 크기·DB·워커만으로 EC2를 우선하지 않습니다.",
        )
    check(
        "aws-ec2",
        "EC2-OPERATIONS-01",
        "unknown",
        "host-operations",
        "OS·장치 호환성, 최소 권한, 패치·복구·네트워크 운영 조건은 검증하지 않았습니다.",
    )

    result = []
    for target, structure, references, drivers in (
        (
            "aws-lambda",
            lambda_structure,
            LAMBDA_REFERENCES,
            ["함수 호출·실행 자원", "접근 경로·로그·데이터 전송"],
        ),
        ("aws-ec2", ec2_structure, HOST_REFERENCES, ["인스턴스 실행·디스크", "네트워크·로그·운영 비용"]),
    ):
        identity = backend_identity(target)
        constraints = checks[target]
        result.append(
            {
                "id": target,
                "provider": identity.provider,
                "backend": identity.backend,
                "sky_adapter_support": identity.sky_adapter_support,
                "selection_mode": identity.selection_mode,
                "structural_status": structure,
                "execution_status": "unsupported_by_sky",
                "status": "unsupported_by_sky",
                "native_adapter": {
                    "support": "implemented",
                    "selection_mode": "explicit_zip_or_cli",
                    "command": "sky-service aws-backend " + ("lambda" if target == "aws-lambda" else "ec2"),
                    "profile": (
                        "python_stdlib_handler_zip"
                        if target == "aws-lambda"
                        else "stateless_ecr_container_public_subnet"
                    ),
                    "verification_status": "unverified",
                    "compiler_integration": "explicit_native_zip",
                    "automatic_selection": "unimplemented",
                },
                "reasons": [item["reason"] for item in constraints]
                + [
                    "지원 조건을 통과한 ZIP은 명시적으로 배포할 수 있습니다. 기존 AI 자동 선택에는 아직 포함하지 않습니다."
                ],
                "constraint_results": constraints,
                "violated_rule_ids": sorted(
                    {item["rule_id"] for item in constraints if item["status"] == "violated"}
                ),
                "unknown_rule_ids": sorted(
                    {item["rule_id"] for item in constraints if item["status"] == "unknown"}
                ),
                "reason_codes": sorted(
                    {"SKY_ADAPTER_UNIMPLEMENTED", *(item["rule_id"] for item in constraints)}
                ),
                "evidence_ids": sorted({ref for item in constraints for ref in item["evidence_ids"]}),
                "evidence_files": sorted({path for item in constraints for path in item["evidence_files"]}),
                "provider_references": list(references),
                "cost_estimate": None,
                "cost": {
                    "estimate": None,
                    "drivers": drivers,
                    "note": "미구현 후보의 비용 영향 항목입니다. 사용량·요금·구성은 미검증입니다.",
                },
                "selected": False,
            }
        )
    return result
