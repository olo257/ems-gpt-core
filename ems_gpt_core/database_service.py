"""MariaDB connection boundary for EMS-GPT Core."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

def build_database(options: dict, timezone: Any):
    @contextmanager
    def db(database: str | None = None):
        import pymysql
        from pymysql.cursors import DictCursor

        conn = pymysql.connect(
            host=options["db_host"], port=int(options["db_port"]),
            user=options["db_user"], password=options["db_password"],
            database=database or options["db_name"], charset="utf8mb4",
            autocommit=False, cursorclass=DictCursor, connect_timeout=10,
            read_timeout=30, write_timeout=30,
        )
        try:
            with conn.cursor() as cur:
                offset = datetime.now(timezone).utcoffset() or timedelta(0)
                minutes = int(offset.total_seconds() // 60)
                sign = "+" if minutes >= 0 else "-"
                hours, mins = divmod(abs(minutes), 60)
                cur.execute("SET time_zone=%s", (f"{sign}{hours:02d}:{mins:02d}",))
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def qname(value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("Invalid SQL identifier")
        return f"`{value}`"

    return SimpleNamespace(db=db, qname=qname)
