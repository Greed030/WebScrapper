"""SQLite-кэш загруженных страниц с TTL. Хранит сырой HTML, а не разобранный результат,
поэтому изменения в разборе не требуют сброса кэша."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from ..models import RawPage

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    key        TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload    BLOB NOT NULL
)
"""


class PageCache:
    def __init__(self, path: str | Path, ttl_seconds: int, enabled: bool = True) -> None:
        self.ttl = ttl_seconds
        self.enabled = enabled and ttl_seconds > 0
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            db_path = Path(path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute(_SCHEMA)
                self._conn.commit()

    async def get(self, key: str) -> RawPage | None:
        if not self._conn:
            return None
        return await asyncio.to_thread(self._get, key)

    async def put(self, key: str, page: RawPage) -> None:
        if not self._conn:
            return
        await asyncio.to_thread(self._put, key, page)

    async def purge_expired(self) -> int:
        if not self._conn:
            return 0
        return await asyncio.to_thread(self._purge)

    def _get(self, key: str) -> RawPage | None:
        with self._lock:
            row = self._conn.execute("SELECT fetched_at, payload FROM pages WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        fetched_at, payload = row
        if time.time() - fetched_at > self.ttl:
            return None
        try:
            page = RawPage.model_validate_json(zlib.decompress(payload))
        except Exception:  # битая или устаревшая запись — просто промах
            log.warning("не удалось прочитать запись кэша %s", key)
            return None
        return page.model_copy(update={"from_cache": True})

    def _put(self, key: str, page: RawPage) -> None:
        payload = zlib.compress(page.model_dump_json(exclude={"from_cache"}).encode("utf-8"))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pages (key, fetched_at, payload) VALUES (?, ?, ?)",
                (key, page.fetched_at, payload),
            )
            self._conn.commit()

    def _purge(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM pages WHERE fetched_at < ?", (time.time() - self.ttl,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        if self._conn:
            with self._lock:
                self._conn.close()
            self._conn = None
