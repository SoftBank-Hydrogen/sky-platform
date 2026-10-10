"""SQLite conversions must initialize pg numeric decoding before connection creation."""

import json
import shutil
import subprocess

import pytest

from application.consistency import PG_NUMERIC_PARSERS, check_postgres_numeric_parsers


@pytest.mark.parametrize(
    "binding,receiver,constructor,suffix",
    [
        ("const { types, Pool } = require('pg');", "types", "Pool", ".js"),
        ("const pg = require('pg');", "pg.types", "pg.Pool", ".cjs"),
        ("import { types, Client } from 'pg';", "types", "Client", ".mjs"),
        ("import pg, { types, Pool } from 'pg';", "types", "Pool", ".mjs"),
        ("import pg from 'pg';", "pg.types", "pg.Client", ".js"),
        ("import * as pg from 'pg';", "pg.types", "pg.Pool", ".mjs"),
        ("import { types as t, Pool as P } from 'pg';", "t", "P", ".mjs"),
        ("const { types: t, Pool: P } = require('pg');", "t", "P", ".cjs"),
        ("const pg = require('pg'); const { types, Pool } = pg;", "types", "Pool", ".js"),
    ],
)
@pytest.mark.parametrize("builtin", [False, True])
def test_cjs_esm_registration_before_connection(tmp_path, binding, receiver, constructor, suffix, builtin):
    path = tmp_path / ("db" + suffix)
    path.write_text(binding + f"\nconst pool = new {constructor}();")
    with pytest.raises(ValueError, match="CV-04.*INT8") as error:
        check_postgres_numeric_parsers(tmp_path)
    assert PG_NUMERIC_PARSERS in str(error.value)
    registrations = PG_NUMERIC_PARSERS.split("\n", 1)[1].replace(
        "types.setTypeParser", receiver + ".setTypeParser"
    )
    if builtin:
        registrations = registrations.replace("(20,", f"({receiver}.builtins.INT8,").replace(
            "(1700,", f"({receiver}.builtins.NUMERIC,"
        )
    path.write_text(binding + "\n" + registrations + f"\nconst pool = new {constructor}();")
    check_postgres_numeric_parsers(tmp_path)


@pytest.mark.parametrize(
    "registration",
    [
        "// types.setTypeParser(20, Number); types.setTypeParser(1700, Number);",
        "/* types.setTypeParser(20, Number); types.setTypeParser(1700, Number); */",
        "const text = 'types.setTypeParser(20, Number); types.setTypeParser(1700, Number);';",
        "const text = `types.setTypeParser(20, Number); types.setTypeParser(1700, Number);`;",
        "function later() { types.setTypeParser(20, Number); types.setTypeParser(1700, Number); }",
        "if (false) { types.setTypeParser(20, Number); types.setTypeParser(1700, Number); }",
        "if (false) types.setTypeParser(20, Number); types.setTypeParser(1700, Number);",
        "const later = () => types.setTypeParser(20, Number); types.setTypeParser(1700, Number);",
        "setTimeout(() => { types.setTypeParser(20, Number); types.setTypeParser(1700, Number); }, 0);",
        "types.setTypeParser(20, Number);",
        "types.setTypeParser(1700, Number);",
        "other.types.setTypeParser(20, Number); other.types.setTypeParser(1700, Number);",
    ],
)
def test_missing_partial_deferred_or_non_executable_registration_is_rejected(tmp_path, registration):
    (tmp_path / "db.js").write_text("const {types, Pool} = require('pg');\n" + registration + "\nnew Pool();")
    with pytest.raises(ValueError, match="CV-04"):
        check_postgres_numeric_parsers(tmp_path)


@pytest.mark.parametrize(
    "early", ["const pool = new Pool();", "client.query('select 1');", "client.connect();"]
)
def test_late_registration_is_rejected(tmp_path, early):
    (tmp_path / "db.js").write_text("const { Pool } = require('pg');\n" + early + "\n" + PG_NUMERIC_PARSERS)
    with pytest.raises(ValueError, match="before Pool/Client"):
        check_postgres_numeric_parsers(tmp_path)


def test_each_connection_module_requires_initialization(tmp_path):
    (tmp_path / "db.js").write_text(PG_NUMERIC_PARSERS)
    (tmp_path / "other.js").write_text("const pg = require('pg'); new pg.Client();")
    with pytest.raises(ValueError, match="other.js"):
        check_postgres_numeric_parsers(tmp_path)


def test_registration_before_import_initialization_is_rejected(tmp_path):
    (tmp_path / "db.js").write_text(
        "types.setTypeParser(20, Number); types.setTypeParser(1700, Number);\nconst {types} = require('pg');"
    )
    with pytest.raises(ValueError, match="CV-04"):
        check_postgres_numeric_parsers(tmp_path)


def test_python_sqlite_and_vendored_files_are_unchanged(tmp_path):
    (tmp_path / "db.py").write_text("import psycopg")
    (tmp_path / "db.js").write_text("const sqlite = require('node:sqlite');")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "driver.js").write_text("require('pg');")
    check_postgres_numeric_parsers(tmp_path)


def test_shared_parser_snippet_preserves_large_integers_and_finite_decimals():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the supplied parser functions")
    # Capture precisely the functions supplied to the real pg registration interface.
    code = PG_NUMERIC_PARSERS.replace(
        "const { types } = require('pg');",
        "const parsers = {}; const types = { setTypeParser: (oid, fn) => parsers[oid] = fn };",
    )
    code += "\nconsole.log(JSON.stringify([20, 1700].map(oid => ['0', '5', '1791631452154', '9007199254740991', '9007199254740992', '9007199254740993', '-9007199254740993', '12.5', 'NaN', 'Infinity'].map(v => parsers[oid](v)))));"
    result = subprocess.run([node, "-e", code], check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values[0] == [
        0,
        5,
        1791631452154,
        9007199254740991,
        "9007199254740992",
        "9007199254740993",
        "-9007199254740993",
        "12.5",
        "NaN",
        "Infinity",
    ]
    assert values[1] == [
        0,
        5,
        1791631452154,
        9007199254740991,
        "9007199254740992",
        "9007199254740993",
        "-9007199254740993",
        12.5,
        "NaN",
        "Infinity",
    ]
