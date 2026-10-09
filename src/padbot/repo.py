"""What the bot reads and writes in the DB. Synchronous, one short-lived
connection per call, so the async handlers run it with asyncio.to_thread."""

import sqlite3
from datetime import datetime

from . import db
from .sizes import SizeRange


class Repo:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def page(self, size: SizeRange, offset: int, limit: int) -> tuple[list[sqlite3.Row], bool]:
        """`limit` rows plus whether more follow (one extra row is fetched to know)."""
        conn = self._connect()
        try:
            rows = db.ranked_products(
                conn, size.min_cm, size.max_cm, limit=limit + 1, offset=offset, max_inclusive=size.max_inclusive
            )
        finally:
            conn.close()
        return rows[:limit], len(rows) > limit

    def product(self, product_code: str) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return db.get_product(conn, product_code)
        finally:
            conn.close()

    def remember_file_id(self, product_code: str, file_id: str | None) -> None:
        conn = self._connect()
        try:
            db.set_tg_file_id(conn, product_code, file_id)
        finally:
            conn.close()

    def last_updated(self) -> datetime | None:
        """finished_at of the latest ok run: the one global freshness check."""
        conn = self._connect()
        try:
            run = db.last_ok_run(conn)
        finally:
            conn.close()
        return datetime.fromisoformat(run["finished_at"]) if run and run["finished_at"] else None
