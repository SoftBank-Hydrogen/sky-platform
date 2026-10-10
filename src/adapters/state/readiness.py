"""Read-only schema probes. Runtime startup never initializes or migrates a DB."""


def check_database_ready(connection_factory, *, operations=False):
    import psycopg

    try:
        with connection_factory() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '3s'")
            versions = tuple(row[0] for row in connection.execute(
                "SELECT version FROM sky_state.schema_versions ORDER BY version"
            ).fetchall())
            if versions != (1, 2):
                raise ValueError("State schema must be migrated separately")
            connection.execute("SELECT workspace,kind,record_id,document,revision "
                               "FROM sky_state.metadata_records LIMIT 0")
            if operations:
                versions = tuple(row[0] for row in connection.execute(
                    "SELECT version FROM sky_state.operation_schema_versions ORDER BY version"
                ).fetchall())
                if versions != (1, 2):
                    raise ValueError("Operation schema must be migrated separately")
                connection.execute("SELECT id,workspace,operation_id,attempt_id,application_id,generation,"
                                   "available_at,published_at,publisher_owner,publisher_epoch,publisher_until,"
                                   "publish_attempts,max_attempts,retry_base_seconds,retry_cap_seconds,"
                                   "failed_at,failure_code FROM sky_state.outbox_events LIMIT 0")
    except psycopg.Error:
        raise OSError("State database is not ready") from None
