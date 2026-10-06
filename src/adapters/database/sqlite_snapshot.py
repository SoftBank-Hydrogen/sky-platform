"""Compile a deliberately small SQLite snapshot into an immutable PostgreSQL migration.

This module does not rewrite application queries. Unsupported SQLite features fail closed.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from adapters.database.migrations import TRANSACTION_CONTROL

MAX_DATABASE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
MAX_SQL_BYTES = 64 * 1024
MAX_TABLES = 8
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SIMPLE_TABLE = re.compile(
    r'\ACREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>"?[A-Za-z_][A-Za-z0-9_]*"?)\s*'
    r"\((?P<columns>[^()]*)\)\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
COLUMN = re.compile(
    r'\A(?P<name>"?[A-Za-z_][A-Za-z0-9_]*"?)\s+(?P<type>INTEGER|TEXT)'
    r"(?:\s+(?P<constraint>PRIMARY\s+KEY(?:\s+AUTOINCREMENT)?|NOT\s+NULL))?\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SqliteSnapshot:
    source_sha256: str
    sql: str
    row_counts: dict[str, int]
    schema: dict[str, list[dict[str, str | bool]]]


def _identifier(name: str) -> str:
    if (
        not IDENTIFIER.fullmatch(name)
        or name.lower().startswith("sqlite_")
        or name == "sky_schema_migrations"
    ):
        raise ValueError(f"PostgreSQL로 안전하게 옮길 수 없는 SQLite 식별자: {name!r}")
    return '"' + name + '"'


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and "\x00" not in value:
        return "'" + value.replace("'", "''") + "'"
    raise ValueError("현재 SQLite 이전은 정수·텍스트·NULL 값만 지원합니다.")


def _declared_columns(table_name: str, ddl: str) -> list[tuple[str, str, str]]:
    statement = SIMPLE_TABLE.fullmatch(ddl)
    if statement is None or statement["name"].strip('"') != table_name:
        raise ValueError(f"{table_name}: 지원하지 않는 SQLite 스키마입니다.")
    result = []
    for entry in statement["columns"].split(","):
        column = COLUMN.fullmatch(entry.strip())
        if column is None:
            raise ValueError(f"{table_name}: 지원하지 않는 SQLite 컬럼 정의입니다.")
        result.append(
            (
                column["name"].strip('"'),
                column["type"].upper(),
                (column["constraint"] or "").upper().replace("  ", " "),
            )
        )
    return result


def compile_sqlite_snapshot(path: Path) -> SqliteSnapshot:
    """Read one stable SQLite file and return bounded, transactional PostgreSQL SQL.

    The caller must review/convert application code independently before deploying.
    """
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_DATABASE_BYTES:
        raise ValueError("SQLite 원본은 2 MiB 이하의 일반 파일이어야 합니다.")
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("SQLite WAL 또는 journal 파일이 있습니다. 일관된 단일 파일 스냅샷을 준비하세요.")
    raw = path.read_bytes()
    if not raw.startswith(b"SQLite format 3\x00") or len(raw) != path.stat().st_size:
        raise ValueError("SQLite 파일 형식 또는 스냅샷 안정성을 확인할 수 없습니다.")
    digest = hashlib.sha256(raw).hexdigest()
    # immutable prevents sidecar creation; query_only makes the intent explicit.
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("SQLite 무결성 검사를 통과하지 못했습니다.")
            objects = connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
            ).fetchall()
            tables = [(name, ddl) for kind, name, ddl in objects if kind == "table"]
            if not 1 <= len(tables) <= MAX_TABLES or any(kind != "table" for kind, _, _ in objects):
                raise ValueError(
                    "현재는 1–8개 기본 테이블만 지원합니다. 뷰·인덱스·트리거는 별도 이전이 필요합니다."
                )
            sql = ["-- Sky SQLite snapshot " + digest, "SET LOCAL standard_conforming_strings = on;"]
            counts: dict[str, int] = {}
            schema: dict[str, list[dict[str, str | bool]]] = {}
            total_rows = 0
            for name, ddl in tables:
                table = _identifier(name)
                if ddl is None:
                    raise ValueError(f"{name}: 지원하지 않는 SQLite 스키마입니다.")
                declared = _declared_columns(name, ddl)
                columns = connection.execute(f"PRAGMA table_xinfo({table})").fetchall()
                if len(columns) != len(declared) or any(column[6] != 0 for column in columns):
                    raise ValueError(f"{name}: 생성·숨김 컬럼은 지원하지 않습니다.")
                if connection.execute(f"PRAGMA foreign_key_list({table})").fetchone():
                    raise ValueError(f"{name}: 외래 키는 별도 이전이 필요합니다.")
                if connection.execute(f"PRAGMA index_list({table})").fetchone():
                    raise ValueError(f"{name}: 인덱스·복합 제약은 별도 이전이 필요합니다.")
                primary = [column for column in columns if column[5]]
                if len(primary) > 1 or (primary and primary[0][2].upper() != "INTEGER"):
                    raise ValueError(f"{name}: 단일 INTEGER 기본 키만 지원합니다.")
                primary_index = next((index for index, column in enumerate(columns) if column[5]), None)
                declarations = []
                names = []
                schema[name] = []
                for index, (_, column_name, declared_type, required, default, key, _) in enumerate(columns):
                    declared_name, declared_type_name, constraint = declared[index]
                    if (
                        declared_name != column_name
                        or declared_type_name != declared_type.upper()
                        or bool(key) != constraint.startswith("PRIMARY KEY")
                        or bool(required) != (constraint == "NOT NULL")
                    ):
                        raise ValueError(f"{name}.{column_name}: SQLite 컬럼 정의가 예상과 다릅니다.")
                    quoted = _identifier(column_name)
                    column_type = declared_type.upper()
                    if default is not None or column_type not in {"INTEGER", "TEXT"}:
                        raise ValueError(f"{name}.{column_name}: 형식 또는 기본값이 지원 범위 밖입니다.")
                    pg_type = "BIGINT" if column_type == "INTEGER" else "TEXT"
                    if key:
                        pg_type += " GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
                    elif required:
                        pg_type += " NOT NULL"
                    declarations.append(f"{quoted} {pg_type}")
                    names.append(quoted)
                    schema[name].append(
                        {
                            "name": column_name,
                            "type": column_type,
                            "primary_key": bool(key),
                            "required": bool(required),
                        }
                    )
                sql.append(f"CREATE TABLE {table} (" + ", ".join(declarations) + ");")
                rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                total_rows += len(rows)
                if total_rows > MAX_ROWS:
                    raise ValueError("SQLite 이전은 총 1,000행 이하만 지원합니다.")
                counts[name] = len(rows)
                for row in rows:
                    for index, value in enumerate(row):
                        column_type = declared[index][1]
                        if value is not None and (
                            (column_type == "INTEGER" and type(value) is not int)
                            or (column_type == "TEXT" and not isinstance(value, str))
                        ):
                            raise ValueError(
                                f"{name}.{declared[index][0]}: SQLite 값이 선언된 형식과 다릅니다."
                            )
                    if primary_index is not None and row[primary_index] < 1:
                        raise ValueError(
                            f"{name}: 기본 키는 양수여야 PostgreSQL identity로 옮길 수 있습니다."
                        )
                    values = ", ".join(_literal(value) for value in row)
                    sql.append(f"INSERT INTO {table} (" + ", ".join(names) + f") VALUES ({values});")
                if primary:
                    key_name = _identifier(primary[0][1])
                    sequence = 0
                    if "AUTOINCREMENT" in declared[primary_index][2]:
                        previous = connection.execute(
                            "SELECT seq FROM sqlite_sequence WHERE name = ?", (name,)
                        ).fetchone()
                        sequence = previous[0] if previous else 0
                        if type(sequence) is not int or sequence < 0:
                            raise ValueError(f"{name}: SQLite 자동 증가 상태를 확인할 수 없습니다.")
                    sql.append(
                        f"SELECT setval(pg_get_serial_sequence('{table}', '{primary[0][1]}'), "
                        f"GREATEST(COALESCE(MAX({key_name}), 1), {sequence}), "
                        f"MAX({key_name}) IS NOT NULL OR {sequence} > 0) FROM {table};"
                    )
                sql.append(
                    f"SELECT 1 / CASE WHEN (SELECT COUNT(*) FROM {table}) = {len(rows)} THEN 1 ELSE 0 END;"
                )
            result = "\n".join(sql) + "\n"
    except sqlite3.DatabaseError as exc:
        raise ValueError("SQLite 스냅샷을 읽지 못했습니다.") from exc
    if len(result.encode("utf-8")) > MAX_SQL_BYTES:
        raise ValueError("생성된 PostgreSQL 마이그레이션이 64 KiB 제한을 초과했습니다.")
    if TRANSACTION_CONTROL.search(result):
        raise ValueError("값에 SQL 마이그레이션 검사와 충돌하는 트랜잭션 키워드가 있습니다.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("SQLite 원본이 변환 중 변경됐습니다.")
    return SqliteSnapshot(digest, result, counts, schema)
