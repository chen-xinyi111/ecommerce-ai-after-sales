import hashlib
from pathlib import Path

from commerce.db import connect_sync

MIGRATIONS = Path(__file__).parent / "migrations"


def migrate() -> list[str]:
    applied = []
    with connect_sync() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(1101001)")
        conn.execute("CREATE SCHEMA IF NOT EXISTS commerce")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS commerce.schema_migrations "
            "(version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(MIGRATIONS.glob("*.sql")):
            content = path.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM commerce.schema_migrations WHERE version=%s", (path.name,)
            ).fetchone()
            if row:
                if row["checksum"] != checksum:
                    raise RuntimeError(f"Applied migration checksum changed: {path.name}")
                continue
            conn.execute(content)
            conn.execute(
                "INSERT INTO commerce.schema_migrations(version,checksum) VALUES (%s,%s)",
                (path.name, checksum),
            )
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    print({"applied": migrate()})
