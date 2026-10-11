import textwrap

import pytest

from application.agent import CLOUD_SQL_TRANSPORT, instructions_for
from application.consistency import check_python_connection_transactions


def project(tmp_path, original, converted):
    roots = [tmp_path / "original", tmp_path / "work"]
    for root, files in zip(roots, [original, converted]):
        for name, source in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
    return roots


@pytest.mark.parametrize(
    "binding",
    [
        "import psycopg\nconn = psycopg.connect()",
        "import psycopg as pg\nconn = pg.connect()",
        "from psycopg import connect as open_db\nconn = open_db()",
        "from psycopg import Connection as C\nconn = C.connect()",
    ],
)
def test_existing_connection_context_is_rejected(tmp_path, binding):
    roots = project(tmp_path, {}, {"db.py": binding + '\nwith conn:\n    conn.execute("SELECT 1")'})
    with pytest.raises(ValueError, match=r"CV-04:.*db.py:") as error:
        check_python_connection_transactions(*roots)
    assert "with conn.transaction():" in str(error.value)
    assert "conn.commit()/conn.rollback()" in str(error.value)


@pytest.mark.parametrize("annotation", ["psycopg.Connection", '"psycopg.Connection"', "Connection"])
def test_connection_parameter_annotations(tmp_path, annotation):
    roots = project(
        tmp_path,
        {},
        {
            "routes.py": f"import psycopg\nfrom psycopg import Connection\n"
            f'def write(db: {annotation}):\n    with db:\n        db.execute("SELECT 1")'
        },
    )
    with pytest.raises(ValueError, match="with db:"):
        check_python_connection_transactions(*roots)


def test_original_annotation_survives_removed_type_in_converted_route(tmp_path):
    original = {
        "routes.py": """
        import sqlite3
        from fastapi import Depends
        def write(db: sqlite3.Connection = Depends(get_db)):
            with db:
                db.execute('SELECT 1')
    """
    }
    converted = {
        "routes.py": """
        import psycopg
        from fastapi import Depends
        def write(db=Depends(get_db)):
            with db:
                db.execute('SELECT 1')
    """
    }
    with pytest.raises(ValueError, match="routes.py:5: with db:"):
        check_python_connection_transactions(*project(tmp_path, original, converted))


@pytest.mark.parametrize("parameter", ["db=Depends(get_db)", "db: Annotated[object, Depends(get_db)]"])
def test_fastapi_dependency_factory_across_modules(tmp_path, parameter):
    converted = {
        "board/db.py": """
            import psycopg as pg
            def connect():
                return pg.connect()
            def get_db():
                conn = connect()
                try:
                    yield conn
                finally:
                    conn.close()
        """,
        "board/routes/posts.py": f"from fastapi import Depends\nfrom typing import Annotated\n"
        f"from ..db import get_db\ndef write({parameter}):\n"
        '    with db:\n        db.execute("SELECT 1")',
    }
    with pytest.raises(ValueError, match="board/routes/posts.py:5: with db:"):
        check_python_connection_transactions(*project(tmp_path, {}, converted))


@pytest.mark.parametrize("context", ["db.transaction()", "db.cursor()", 'open("image.png", "wb")'])
def test_transaction_cursor_and_file_contexts_are_allowed(tmp_path, context):
    converted = {"db.py": f"import psycopg\ndb = psycopg.connect(autocommit=True)\nwith {context}:\n    pass"}
    check_python_connection_transactions(*project(tmp_path, {}, converted))


def test_scope_shadowing_and_reassignment_do_not_flag_file_handles(tmp_path):
    converted = {
        "db.py": """
        import psycopg
        db = psycopg.connect()
        def file_only(db):
            with db:
                pass
        def changed():
            conn = psycopg.connect()
            conn.close()
            conn = open('image.png', 'wb')
            with conn:
                pass
        with psycopg.connect() as conn:
            with conn.cursor():
                pass
    """
    }
    check_python_connection_transactions(*project(tmp_path, {}, converted))


def test_comments_strings_sqlite_only_and_psycopg2_are_not_blocked(tmp_path):
    source = 'import sqlite3\ndb = sqlite3.connect(":memory:")\nwith db:\n    pass'
    check_python_connection_transactions(*project(tmp_path, {}, {"db.py": source}))
    (tmp_path / "work/db.py").write_text('import psycopg2\ntext = "with db:"\n# with db:\n')
    check_python_connection_transactions(tmp_path / "original", tmp_path / "work")


def test_fixing_each_connection_context_passes(tmp_path):
    source = 'import psycopg\ndb = psycopg.connect(autocommit=True)\nwith db:\n    db.execute("SELECT 1")'
    roots = project(tmp_path, {}, {"db.py": source})
    with pytest.raises(ValueError, match="CV-04"):
        check_python_connection_transactions(*roots)
    (roots[1] / "db.py").write_text(source.replace("with db:", "with db.transaction():"))
    check_python_connection_transactions(*roots)


def test_connection_passed_to_untyped_imported_helper(tmp_path):
    sources = {
        "db.py": "import psycopg\nfrom security import save\nconn = psycopg.connect()\nsave(conn)\n",
        "security.py": 'def save(db):\n    with db:\n        db.execute("SELECT 1")',
    }
    with pytest.raises(ValueError, match="security.py:2: with db:"):
        check_python_connection_transactions(*project(tmp_path, {}, sources))


def test_guard_runs_before_adapter_is_called(tmp_path):
    from application.agent import DeploymentTools

    original, work = project(
        tmp_path, {}, {"db.py": "import psycopg\ndb=psycopg.connect()\nwith db:\n    pass"}
    )
    tools = DeploymentTools.__new__(DeploymentTools)
    tools.plan = object()
    tools.sqlite_conversion = {"path": "data/board.db"}
    tools.original, tools.work = original, work
    with pytest.raises(ValueError, match="CV-04"):
        tools.deploy_application()


def test_python_instructions_are_common_and_cloudsql_transport_is_target_specific():
    for target in ["aws-ecs-express", "cloud-run"]:
        instructions = instructions_for(target)
        assert "psycopg `with conn:` closes it on exit" in instructions
        assert "with conn.transaction():" in instructions
        assert "autocommit=True" in instructions
        assert "savepoint" in instructions
        assert (CLOUD_SQL_TRANSPORT in instructions) == (target == "cloud-run")
