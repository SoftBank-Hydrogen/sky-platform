"""Deterministic workload signals and target compatibility decisions."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from hashlib import sha256

SOURCE_EXTENSIONS = {
    ".js",
    ".cjs",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".rb",
    ".go",
    ".php",
    ".java",
    ".kt",
    ".cs",
    ".prisma",
}
MANIFESTS = {
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Gemfile",
    "go.mod",
    "Cargo.toml",
    "Procfile",
    "Dockerfile",
}
SKIP_DIRECTORIES = {
    "tests",
    "test",
    "__tests__",
    "spec",
    "docs",
    "examples",
    "vendor",
    "dist",
    "build",
    "node_modules",
}
SQLITE_FILES = {".db", ".sqlite", ".sqlite3"}
SQLITE_SOURCE = re.compile(
    r"(?:\b(?:require|import)\s*\(?\s*['\"](?:node:)?sqlite(?:3)?['\"]|"
    r"\b(?:import|from)\s+(?:sqlite3|aiosqlite)\b|"
    r"\b(?:better-sqlite3|sqlite3|aiosqlite|sqlalchemy\.dialects\.sqlite)\b|"
    r"\b(?:jdbc:sqlite:|sqlite:///|file:[^\s'\"]+\.db\?mode=)|"
    r"\b(?:provider|dialect)\s*[:=]\s*['\"]sqlite['\"])",
    re.IGNORECASE,
)
SQLITE_DEPENDENCIES = {"sqlite3", "better-sqlite3", "sqlite", "aiosqlite", "pysqlite3", "sqlite-utils"}
DATABASE_DEPENDENCIES = {
    "pg",
    "postgres",
    "mysql",
    "mysql2",
    "mariadb",
    "mongodb",
    "mongoose",
    "@prisma/client",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
    "asyncpg",
    "pymysql",
    "mysqlclient",
    "pymongo",
    "motor",
    "sqlalchemy",
}
DATABASE_ENGINE_DEPENDENCIES = {
    "postgresql": {"pg", "postgres", "psycopg", "psycopg2", "psycopg2-binary", "asyncpg"},
    "mysql": {"mysql", "mysql2", "mariadb", "pymysql", "mysqlclient"},
    "mongodb": {"mongodb", "mongoose", "pymongo", "motor"},
}
DATABASE_ENGINE_SOURCE = {
    "postgresql": re.compile(
        r"\b(?:require\s*\(?\s*|from\s+)['\"](?:pg|postgres)['\"]|"
        r"\b(?:import|from)\s+(?:psycopg2?|asyncpg)(?:\b|\.)|"
        r"\bprovider\s*=\s*['\"]postgresql['\"]|\bpostgres(?:ql)?(?:\+\w+)?://",
        re.IGNORECASE,
    ),
    "mysql": re.compile(
        r"\b(?:require\s*\(?\s*|from\s+)['\"](?:mysql2?|mariadb)['\"]|"
        r"\b(?:import|from)\s+(?:pymysql|MySQLdb)(?:\b|\.)|"
        r"\bprovider\s*=\s*['\"]mysql['\"]|\bmysql(?:\+\w+)?://",
        re.IGNORECASE,
    ),
    "mongodb": re.compile(
        r"\b(?:require\s*\(?\s*|from\s+)['\"](?:mongodb|mongoose)['\"]|"
        r"\b(?:import|from)\s+(?:pymongo|motor)(?:\b|\.)|"
        r"\bprovider\s*=\s*['\"]mongodb['\"]|\bmongodb(?:\+\w+)?://",
        re.IGNORECASE,
    ),
}
DATABASE_SOURCE = re.compile(
    r"\b(?:require\s*\(?\s*|from\s+)['\"](?:pg|postgres|mysql2?|mariadb|mongodb|mongoose)['\"]|"
    r"\b(?:import|from)\s+(?:psycopg2?|asyncpg|pymysql|MySQLdb|pymongo|motor|sqlalchemy)(?:\b|\.)|"
    r"\bprovider\s*=\s*['\"](?:postgresql|mysql|mongodb|sqlserver|cockroachdb)['\"]|"
    r"\b(?:postgres(?:ql)?|mysql|mongodb)(?:\+\w+)?://",
    re.IGNORECASE,
)
WORKER_DEPENDENCIES = {"bull", "bullmq", "celery", "rq", "huey", "dramatiq", "sidekiq", "resque"}
LOCAL_WRITE = re.compile(
    r"\b(?:writeFile(?:Sync)?|appendFile(?:Sync)?|createWriteStream)\s*\(\s*['\"](?:\./)?(?:data|uploads|storage)/[^'\"]+['\"]|"
    r"\bopen\s*\(\s*['\"](?:\./)?(?:data|uploads|storage)/[^'\"]+['\"]\s*,\s*['\"][wax+]",
    re.IGNORECASE,
)
WEBSOCKET_SOURCE = re.compile(r"\b(?:WebSocketServer|new\s+WebSocket\s*\(|\.on\s*\(\s*['\"]upgrade['\"])")
PROCESS_LOCAL_MAP = re.compile(r"\b(?:const|let|var)\s+\w+\s*=\s*new\s+(?:Map|Set)\s*\(")
TARGET_RESOURCES = {
    "local-docker": ["Docker image", "local container"],
    "onprem-compose": ["Docker image", "same-host Compose service", "optional SQLite volume"],
    "onprem-vm": ["Docker image", "remote Linux VM Compose service"],
    "cloud-run": ["Artifact Registry repository", "runtime service account", "Cloud Run service"],
    "aws-ecs-express": ["CloudFormation base stack", "ECR repository", "ECS Express service"],
}
# These describe the currently implemented adapters, not everything the providers offer.
TARGET_CAPABILITIES = {
    "local-docker": {
        "postgresql_binding": False,
        "existing_rds_binding": False,
        "new_rds_provisioning": False,
        "sqlite_volume": True,
        "remote_host": False,
        "durable_files": False,
        "background_worker": False,
        "image_platform": None,
        "access_modes": ["loopback"],
    },
    "onprem-compose": {
        "postgresql_binding": False,
        "existing_rds_binding": False,
        "new_rds_provisioning": False,
        "sqlite_volume": True,
        "remote_host": False,
        "durable_files": False,
        "background_worker": False,
        "image_platform": None,
        "access_modes": ["loopback"],
    },
    "onprem-vm": {
        "postgresql_binding": False,
        "existing_rds_binding": False,
        "new_rds_provisioning": False,
        "sqlite_volume": False,
        "remote_host": True,
        "durable_files": False,
        "background_worker": False,
        "image_platform": None,
        "access_modes": ["public"],
    },
    "cloud-run": {
        "postgresql_binding": False,
        "existing_rds_binding": False,
        "new_rds_provisioning": False,
        "sqlite_volume": False,
        "remote_host": False,
        "durable_files": False,
        "background_worker": False,
        "image_platform": "linux/amd64",
        "access_modes": ["authenticated", "public"],
    },
    "aws-ecs-express": {
        "postgresql_binding": True,
        "existing_rds_binding": True,
        "new_rds_provisioning": True,
        "sqlite_volume": False,
        "remote_host": False,
        "durable_files": False,
        "background_worker": False,
        "image_platform": "linux/amd64",
        "access_modes": ["public"],
    },
}
DOCKER_FROM = re.compile(r"(?im)^\s*FROM\s+(?:--platform=([^\s]+)\s+)?[^\s#]+")
MAX_INSPECT_FILES = 1000
MAX_INSPECT_BYTES = 8 * 1024 * 1024
MAX_INSPECT_FILE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class InfrastructureProfile:
    storage: str
    evidence: tuple[str, ...]
    scanned_files: int
    requirements: tuple[str, ...] = ()
    database_engines: tuple[str, ...] = ()
    final_image_platform: str | None = None
    requirement_evidence: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source_signals: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def as_dict(self):
        return asdict(self)


def evidence_id(requirement: str, path: str) -> str:
    return "E-" + sha256(f"{requirement}\0{path}".encode()).hexdigest()[:12]


def deployment_access_mode(target: str, public_access: bool) -> str | None:
    """Return the access mode this adapter will actually deploy, if allowed."""
    if target not in TARGET_CAPABILITIES:
        raise ValueError("지원하지 않는 배포 대상입니다.")
    modes = TARGET_CAPABILITIES[target]["access_modes"]
    if "loopback" in modes:
        return "loopback"
    if public_access and "public" in modes:
        return "public"
    if not public_access and "authenticated" in modes:
        return "authenticated"
    return None


def infrastructure_compatibility(
    profile: InfrastructureProfile,
    target: str,
    *,
    postgres: bool = False,
    local_sqlite: bool = False,
    public_access: bool | None = None,
) -> dict:
    """Assess detected requirements against the actual Sky target adapter."""
    if target != "auto" and target not in TARGET_CAPABILITIES:
        raise ValueError("지원하지 않는 배포 대상입니다.")
    # Auto has no adapter yet: only requirements supported by every candidate pass here.
    capabilities = TARGET_CAPABILITIES.get(
        target,
        {
            "postgresql_binding": False,
            "durable_files": False,
            "background_worker": False,
            "image_platform": None,
            "access_modes": [],
        },
    )
    constraints = []
    evidence_by_requirement = dict(profile.requirement_evidence)
    source_signals = dict(profile.source_signals)

    def check(rule_id: str, requirement: str, problem: str | None) -> None:
        constraints.append(
            {
                "rule_id": rule_id,
                "status": "violated" if problem else "satisfied",
                "requirement": requirement,
                "evidence_files": list(evidence_by_requirement.get(requirement, ())),
                "evidence_ids": [
                    evidence_id(requirement, path) for path in evidence_by_requirement.get(requirement, ())
                ],
                "reason": problem or "현재 대상 어댑터의 선언과 충돌하지 않습니다.",
            }
        )

    if "sqlite" in profile.requirements or profile.storage == "sqlite":
        check(
            "DATA-SQLITE-01",
            "sqlite",
            None
            if local_sqlite and target in {"local-docker", "onprem-compose"}
            else "SQLite 데이터베이스에 영속 저장소·마이그레이션이 필요합니다",
        )
    if ("database" in profile.requirements or profile.storage == "database") and not (
        postgres and capabilities["postgresql_binding"] and profile.database_engines == ("postgresql",)
    ):
        engines = ", ".join(profile.database_engines) if profile.database_engines else "불명"
        check(
            "DATA-BINDING-01",
            "database",
            f"{engines} 데이터베이스 서비스 연결·마이그레이션 검증이 필요합니다",
        )
    elif "database" in profile.requirements or profile.storage == "database":
        check("DATA-BINDING-01", "database", None)
    if "local-files" in profile.requirements:
        check(
            "STORAGE-DURABILITY-01",
            "local-files",
            None if capabilities["durable_files"] else "로컬 파일 쓰기에 영속 저장소가 필요합니다",
        )
    if "background-worker" in profile.requirements:
        check(
            "WORKER-01",
            "background-worker",
            None if capabilities["background_worker"] else "별도 백그라운드 워커가 필요합니다",
        )
    if "websocket" in source_signals:
        constraints.append(
            {
                "rule_id": "PROTOCOL-WS-01",
                "status": "unknown",
                "requirement": "websocket",
                "evidence_files": list(source_signals["websocket"]),
                "evidence_ids": [evidence_id("websocket", path) for path in source_signals["websocket"]],
                "reason": "WebSocket 사용 신호가 있습니다. 이 대상의 실제 ingress 왕복은 아직 검증되지 않았습니다.",
            }
        )
    literal_platform = (
        profile.final_image_platform
        if profile.final_image_platform
        and re.fullmatch(r"[a-z0-9_]+/[a-z0-9_]+(?:/[a-z0-9_]+)?", profile.final_image_platform)
        else None
    )
    if (
        literal_platform
        and capabilities["image_platform"]
        and "/".join(literal_platform.split("/")[:2]) != capabilities["image_platform"]
    ):
        check(
            "IMAGE-PLATFORM-01",
            "image-platform",
            f"최종 Dockerfile 단계의 {profile.final_image_platform} 플랫폼이 "
            f"{capabilities['image_platform']} 이미지 빌드와 충돌합니다",
        )
    elif literal_platform and capabilities["image_platform"]:
        check("IMAGE-PLATFORM-01", "image-platform", None)
    access_mode = (
        deployment_access_mode(target, public_access)
        if target != "auto" and public_access is not None
        else None
    )
    if target != "auto" and public_access is not None and access_mode is None:
        check("ACCESS-01", "access-mode", "선택한 공개 범위로 배포할 수 없습니다")
    elif target != "auto" and public_access is not None:
        check("ACCESS-01", "access-mode", None)
    problems = [item["reason"] for item in constraints if item["status"] == "violated"]
    unknowns = [item["reason"] for item in constraints if item["status"] == "unknown"]
    return {
        "target": target,
        "detected_requirements": list(profile.requirements),
        "database_engines": list(profile.database_engines),
        "evidence": list(profile.evidence),
        "declared_image_platform": profile.final_image_platform,
        "access_mode": access_mode,
        "postgres_binding": postgres,
        "local_sqlite_binding": local_sqlite and target in {"local-docker", "onprem-compose"},
        "adapter_capabilities": capabilities.copy(),
        "compatible": not problems,
        "problems": problems,
        "unknowns": unknowns,
        "constraint_results": constraints,
        "inspection_note": "탐지 신호가 없어도 무상태 앱임이 증명된 것은 아닙니다.",
    }


def validate_infrastructure(
    profile: InfrastructureProfile, target: str, *, postgres: bool = False, local_sqlite: bool = False
) -> None:
    report = infrastructure_compatibility(profile, target, postgres=postgres, local_sqlite=local_sqlite)
    if report["problems"]:
        sqlite_guidance = (
            " SQLite 앱은 Local Docker에서 DB 경로를 확인한 영속 볼륨을 명시하거나 "
            "AWS ECS Express를 선택하고 인터넷 공개를 허용한 뒤, "
            "'SQLite 파일을 PostgreSQL로 이전하기'와 기존 RDS 또는 신규 RDS 생성 계획을 선택하세요. "
            "이전은 실험적이며 RDS 비용이 발생합니다."
            if "sqlite" in profile.requirements or profile.storage == "sqlite"
            else ""
        )
        raise ValueError(
            "인프라 요구가 감지됐습니다 ("
            + ", ".join(profile.evidence[:3])
            + "): "
            + "; ".join(report["problems"])
            + ". 현재 "
            + target
            + " 구성에서는 지원하지 않아 데이터 손실 또는 작업 누락 위험이 있으므로 배포를 중단합니다."
            + sqlite_guidance
        )


def explicit_infrastructure_plan(
    target: str,
    profile: InfrastructureProfile,
    *,
    existing_postgres_id: str | None = None,
    create_postgres_id: str | None = None,
) -> dict:
    """Record the supported resources that a user-selected deployment will actually use."""
    if target not in TARGET_RESOURCES:
        raise ValueError("지원하지 않는 배포 대상입니다.")
    if existing_postgres_id and create_postgres_id:
        raise ValueError("PostgreSQL 신규 생성과 기존 DB 사용을 동시에 선택할 수 없습니다.")
    if existing_postgres_id is None and create_postgres_id is None:
        return {
            "target": target,
            "workload": "unconfirmed",
            "rationale": "사용자가 배포 대상을 지정했습니다. 알려진 영속 저장소 의존성은 사전 검사합니다.",
            "evidence": [],
            "resources": TARGET_RESOURCES[target],
            "planner": "user",
        }
    database_id = create_postgres_id or existing_postgres_id
    if (
        target != "aws-ecs-express"
        or profile.database_engines != ("postgresql",)
        or "database" not in profile.requirements
        or not re.fullmatch(r"sky-[a-z][a-z0-9]*(?:-[a-z0-9]+)*", database_id)
    ):
        raise ValueError("PostgreSQL 인프라 계획의 대상과 탐지 결과가 다릅니다.")
    if create_postgres_id is not None:
        return {
            "target": target,
            "workload": "postgresql-http",
            "rationale": "앱의 PostgreSQL 의존성과 SQL 마이그레이션을 확인했습니다. 검토한 계획으로 앱 소유 RDS를 생성한 뒤 ECS 서비스를 배포합니다. 앱 배포가 실패해도 DB와 데이터는 보존됩니다.",
            "evidence": [],
            "detected_files": list(profile.evidence),
            "resources": [*TARGET_RESOURCES[target], "new RDS PostgreSQL", "one-off SQL migration task"],
            "database": {"binding": "create", "database_id": create_postgres_id},
            "planner": "user",
        }
    return {
        "target": target,
        "workload": "postgresql-http",
        "rationale": "앱의 PostgreSQL 의존성과 SQL 마이그레이션을 확인했습니다. 소유권을 검증한 기존 RDS에 ECS 서비스를 연결합니다. DB는 새로 생성하지 않으며 앱 종료 후에도 보존됩니다.",
        "evidence": [],
        "detected_files": list(profile.evidence),
        "resources": [*TARGET_RESOURCES[target], "existing RDS PostgreSQL", "one-off SQL migration task"],
        "database": {"binding": "existing", "database_id": database_id},
        "planner": "user",
    }
