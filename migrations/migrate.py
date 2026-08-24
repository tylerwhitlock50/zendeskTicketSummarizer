"""Apply migrations/NNN_*.sql to the RTM Postgres database, in order, once each."""

import os
import sys
from pathlib import Path

import psycopg

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

MIGRATIONS_DIR = Path(__file__).resolve().parent


def main() -> int:
    dsn = os.environ.get("RTM_DATABASE_URL")
    if not dsn:
        print("RTM_DATABASE_URL is not set", file=sys.stderr)
        return 1

    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("CREATE SCHEMA IF NOT EXISTS rtm")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rtm.schema_migrations "
                "(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )

        applied = {
            row[0] for row in conn.execute("SELECT filename FROM rtm.schema_migrations").fetchall()
        }

        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            if path.name in applied:
                print(f"skip  {path.name} (already applied)")
                continue
            try:
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO rtm.schema_migrations (filename) VALUES (%s)", (path.name,)
                    )
            except Exception as exc:
                print(f"FAIL  {path.name}: {exc}", file=sys.stderr)
                return 1
            print(f"apply {path.name}")

    print("migrations up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
