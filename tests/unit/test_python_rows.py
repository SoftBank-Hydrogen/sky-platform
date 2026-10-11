import os
import textwrap
import uuid

import pytest

from application.agent import CLOUD_SQL_TRANSPORT, DeploymentTools, instructions_for
from application.consistency import check_python_dict_row_access


def write(tmp_path, files):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "consumer",
    [
        'db.execute("SELECT count(*)").fetchone()[0]',
        'row = db.execute("INSERT ... RETURNING id").fetchone()\nvalue = row[0]',
        'rows = db.execute("SELECT id").fetchall()\nvalue = rows[0][0]',
        'for row in db.execute("SELECT id").fetchall():\n    value = row[-1]',
        '[row[0] for row in db.execute("SELECT id").fetchall()]',
        'with db.cursor() as cur:\n    cur.execute("SELECT id")\n    value = cur.fetchone()[0]',
    ],
)
def test_positional_dict_row_access_rejected(tmp_path, consumer):
    root = write(
        tmp_path,
        {
            "app.py": "import psycopg as pg\nfrom psycopg.rows import dict_row as mapping\n"
            "db = pg.connect(row_factory=mapping)\n" + consumer
        },
    )
    with pytest.raises(ValueError, match="CV-04: psycopg dict_row") as error:
        check_python_dict_row_access(root)
    assert 'fetchone()["total"]' in str(error.value)
    assert 'fetchone()["id"]' in str(error.value)


@pytest.mark.parametrize("parameter", ["db=Depends(get_db)", "db: Annotated[object, Depends(get_db)]"])
def test_relative_fastapi_factory_with_row_factory_assignment(tmp_path, parameter):
    root = write(
        tmp_path,
        {
            "board/db.py": """
                from psycopg import connect
                from psycopg.rows import dict_row
                def connect_db():
                    conn = connect(autocommit=True)
                    conn.row_factory = dict_row
                    return conn
                def get_db():
                    conn = connect_db()
                    try:
                        yield conn
                    finally:
                        conn.close()
            """,
            "board/routes/posts.py": "from fastapi import Depends\nfrom typing import Annotated\n"
            f"from ..db import get_db\ndef posts({parameter}):\n"
            '    return db.execute("SELECT COUNT(*)").fetchone()[0]',
        },
    )
    with pytest.raises(ValueError, match="board/routes/posts.py:5"):
        check_python_dict_row_access(root)


@pytest.mark.parametrize(
    "consumer",
    [
        'db.execute("SELECT COUNT(*) AS total").fetchone()["total"]',
        'db.execute("INSERT ... RETURNING id").fetchone()["id"]',
        'rows = db.execute("SELECT id").fetchall()\nrow = rows[0]\nvalue = row["id"]',
        'rows = db.execute("SELECT id").fetchall()[:5]\nrow = rows[0]\nvalue = row["id"]',
        '[row["id"] for row in db.execute("SELECT id").fetchall()]',
        'with db.cursor(row_factory=tuple_row) as cur:\n    value = cur.execute("SELECT 1").fetchone()[0]',
        'other = psycopg.connect(row_factory=tuple_row)\nvalue = other.execute("SELECT 1").fetchone()[0]',
        'values = [1, 2]\nvalue = values[0]\ntext = "fetchone()[0]"\n# row[0]',
        'row = db.execute("SELECT 1").fetchone()\nrow = [123]\nvalue = row[0]',
    ],
)
def test_named_rows_tuple_overrides_and_unrelated_indexes_allowed(tmp_path, consumer):
    root = write(
        tmp_path,
        {
            "app.py": "import psycopg\nfrom psycopg.rows import dict_row, tuple_row\n"
            "db = psycopg.connect(row_factory=dict_row)\n" + consumer
        },
    )
    check_python_dict_row_access(root)


def test_keyword_dictionary_and_unknown_custom_factory(tmp_path):
    root = write(
        tmp_path,
        {
            "app.py": "import psycopg\nfrom psycopg.rows import dict_row\n"
            "options = {'row_factory': dict_row}\ndb=psycopg.connect(**options)\n"
            'db.execute("SELECT 1").fetchone()[0]'
        },
    )
    with pytest.raises(ValueError, match="CV-04"):
        check_python_dict_row_access(root)
    path = root / "app.py"
    path.write_text(path.read_text().replace("'row_factory': dict_row", "'row_factory': compatible_row"))
    check_python_dict_row_access(root)


def test_row_guard_runs_before_deployment_adapter(tmp_path):
    root = write(
        tmp_path / "work",
        {
            "app.py": "import psycopg\nfrom psycopg.rows import dict_row\n"
            'db=psycopg.connect(row_factory=dict_row)\ndb.execute("SELECT 1").fetchone()[0]'
        },
    )
    tools = DeploymentTools.__new__(DeploymentTools)
    tools.plan, tools.sqlite_conversion = object(), {"path": "data/board.db"}
    tools.original, tools.work = tmp_path / "original", root
    tools.original.mkdir()
    with pytest.raises(ValueError, match="CV-04: psycopg dict_row"):
        tools.deploy_application()


def test_row_instructions_apply_to_both_cloud_targets():
    for target in ("aws-ecs-express", "cloud-run"):
        instructions = instructions_for(target)
        assert "psycopg dict_row allows only" in instructions
        assert 'SELECT COUNT(*) AS total then fetchone()["total"]' in instructions
        assert 'RETURNING id then fetchone()["id"]' in instructions
        assert (CLOUD_SQL_TRANSPORT in instructions) == (target == "cloud-run")


def test_real_postgres_named_rows_and_committed_insert():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SKY_TEST_POSTGRES_DSN not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.rows import dict_row, tuple_row

    table = sql.Identifier("sky_row_contract_" + uuid.uuid4().hex)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(
            sql.SQL("CREATE TABLE {} (id BIGINT GENERATED ALWAYS AS IDENTITY, title TEXT)").format(table)
        )
        try:
            with conn.transaction():
                inserted = conn.execute(
                    sql.SQL("INSERT INTO {} (title) VALUES (%s) RETURNING id").format(table), ("post",)
                ).fetchone()
                with pytest.raises(KeyError):
                    _ = inserted[0]
                assert type(inserted["id"]) is int
            with psycopg.connect(dsn, row_factory=dict_row) as observer:
                counted = observer.execute(
                    sql.SQL("SELECT COUNT(*) AS total FROM {}").format(table)
                ).fetchone()
                assert counted == {"total": 1}
                assert type(counted["total"]) is int
            with conn.cursor(row_factory=tuple_row) as cursor:
                assert cursor.execute("SELECT 42").fetchone()[0] == 42
        finally:
            conn.execute(sql.SQL("DROP TABLE {}").format(table))
