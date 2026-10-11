import argparse

from commerce.config import CommerceSettings
from commerce.db import connect_sync
from commerce.migrate import migrate
from commerce.seed import seed


def reset(confirm: str):
    config = CommerceSettings()
    if (
        confirm != "agent_commerce_demo"
        or config.POSTGRES_DB != confirm
        or config.POSTGRES_HOST != "127.0.0.1"
    ):
        raise RuntimeError("Reset is restricted to the dedicated loopback demo database")
    with connect_sync() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(1101001)")
        actual_row = conn.execute("SELECT current_database() AS name").fetchone()
        assert actual_row is not None
        actual = actual_row["name"]
        marker = conn.execute("SELECT marker FROM commerce.demo_marker WHERE id=1").fetchone()
        if actual != confirm or not marker or marker["marker"] != "agent-commerce-demo-v1":
            raise RuntimeError("Demo marker missing; refusing reset")
        conn.execute("DROP SCHEMA commerce CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    migrate()
    return seed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    print(reset(parser.parse_args().confirm))
