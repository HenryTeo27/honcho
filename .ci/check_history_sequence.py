"""Read-only checks of the actual PostgreSQL conversation sequence."""
import os
import psycopg

uri = os.environ["DB_CONNECTION_URI"].replace("postgresql+psycopg://", "postgresql://", 1)
with psycopg.connect(uri) as connection:
    for workspace, count in (("import-smoke-fixture", 5), ("import-smoke-earlier", 7)):
        rows = connection.execute(
            "SELECT content, seq_in_session, created_at, metadata FROM messages "
            "WHERE workspace_name = %s AND session_name = %s ORDER BY seq_in_session",
            (workspace, workspace)).fetchall()
        assert len(rows) == count
        assert [row[1] for row in rows] == list(range(1, count + 1))
        times = [row[2] for row in rows]
        assert times == sorted(times), "seq_in_session reverses original message time"
        assert all("\x00" not in row[0] and "ghp_" + "C" * 36 not in row[0] for row in rows)
        assert all("Generated compression summary" not in row[0] for row in rows)
        assert len({row[3]["honcho_import_key"] for row in rows}) == count
print("POSTGRES SEQUENCE PASS: both targets preserve actual seq_in_session chronology.")
