"""Readiness validates versions and projections using read-only statements."""

from unittest.mock import MagicMock

import pytest

from adapters.state.readiness import check_database_ready


def connection(versions=(1, 2)):
    factory = MagicMock()
    factory.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
        (v,) for v in versions
    ]
    return factory


def test_operation_probe_includes_retry_columns_without_ddl():
    factory = connection()
    check_database_ready(factory, operations=True)
    statements = [c.args[0] for c in factory.return_value.__enter__.return_value.execute.call_args_list]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert any("retry_cap_seconds" in sql and "LIMIT 0" in sql for sql in statements)
    assert not any(sql.startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE")) for sql in statements)


@pytest.mark.parametrize("versions", [(), (1,), (1, 2, 3)])
def test_incompatible_schema_rejected_without_repair(versions):
    with pytest.raises(ValueError):
        check_database_ready(connection(versions))
