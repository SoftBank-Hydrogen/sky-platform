"""Deterministic workload signals and target compatibility decisions."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

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
TARGET_RESOURCES = {
    "local-docker": ["Docker image", "local container"],
    "cloud-run": ["Artifact Registry repository", "runtime service account", "Cloud Run service"],
    "aws-ecs-express": ["CloudFormation base stack", "ECR repository", "ECS Express service"],
}
# These describe the currently implemented adapters, not everything the providers offer.
TARGET_CAPABILITIES = {
    "local-docker": {
        "postgresql_binding": False,
        "durable_files": False,
        "background_worker": False,
        "image_platform": None,
        "access_modes": ["loopback"],
    },
    "cloud-run": {
        "postgresql_binding": False,
        "durable_files": False,
        "background_worker": False,
        "image_platform": "linux/amd64",
        "access_modes": ["authenticated", "public"],
    },
    "aws-ecs-express": {
        "postgresql_binding": True,
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

    def as_dict(self):
        return asdict(self)


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
    profile: InfrastructureProfile, target: str, *, postgres: bool = False, public_access: bool | None = None
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
    problems = []
    if "sqlite" in profile.requirements or profile.storage == "sqlite":
        problems.append("SQLite 데이터베이스에 영속 저장소·마이그레이션이 필요합니다")
    if ("database" in profile.requirements or profile.storage == "database") and not (
        postgres and capabilities["postgresql_binding"] and profile.database_engines == ("postgresql",)
    ):
        engines = ", ".join(profile.database_engines) if profile.database_engines else "불명"
        problems.append(f"{engines} 데이터베이스 서비스 연결·마이그레이션 검증이 필요합니다")
    if "local-files" in profile.requirements and not capabilities["durable_files"]:
        problems.append("로컬 파일 쓰기에 영속 저장소가 필요합니다")
    if "background-worker" in profile.requirements and not capabilities["background_worker"]:
        problems.append("별도 백그라운드 워커가 필요합니다")
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
        problems.append(
            f"최종 Dockerfile 단계의 {profile.final_image_platform} 플랫폼이 "
            f"{capabilities['image_platform']} 이미지 빌드와 충돌합니다"
        )
    access_mode = (
        deployment_access_mode(target, public_access)
        if target != "auto" and public_access is not None
        else None
    )
    if target != "auto" and public_access is not None and access_mode is None:
        problems.append("선택한 공개 범위로 배포할 수 없습니다")
    return {
        "target": target,
        "detected_requirements": list(profile.requirements),
        "database_engines": list(profile.database_engines),
        "evidence": list(profile.evidence),
        "declared_image_platform": profile.final_image_platform,
        "access_mode": access_mode,
        "postgres_binding": postgres,
        "adapter_capabilities": capabilities.copy(),
        "compatible": not problems,
        "problems": problems,
        "inspection_note": "탐지 신호가 없어도 무상태 앱임이 증명된 것은 아닙니다.",
    }


def validate_infrastructure(profile: InfrastructureProfile, target: str, *, postgres: bool = False) -> None:
    report = infrastructure_compatibility(profile, target, postgres=postgres)
    if report["problems"]:
        raise ValueError(
            "인프라 요구가 감지됐습니다 ("
            + ", ".join(profile.evidence[:3])
            + "): "
            + "; ".join(report["problems"])
            + ". 현재 "
            + target
            + " 구성에서는 지원하지 않아 데이터 손실 또는 작업 누락 위험이 있으므로 배포를 중단합니다."
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
