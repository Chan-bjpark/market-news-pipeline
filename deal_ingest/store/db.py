"""
SQLite DB helper for deal_ingest.

Usage:
    from store.db import Database
    with Database("store/articles.db") as db:
        db.upsert_article(...)
        rows = db.recent_articles(days=3)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional


SCHEMA_FILE = Path(__file__).parent / "schema.sql"


@dataclass
class Article:
    source: str                  # 'thebell', 'dealsite', etc.
    source_label: str            # '더벨', '딜인사이트', etc.
    url: str                     # 정규화된 URL
    title: str
    published_at: str            # ISO 8601 with timezone
    summary: Optional[str] = None
    category: Optional[str] = None
    paywalled: bool = False
    raw_meta: dict = field(default_factory=dict)
    fetched_at: Optional[str] = None  # 자동 채워짐

    def to_row(self) -> tuple:
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()
        return (
            self.source,
            self.source_label,
            self.url,
            self.title,
            self.summary,
            self.published_at,
            self.fetched_at,
            self.category,
            1 if self.paywalled else 0,
            json.dumps(self.raw_meta, ensure_ascii=False),
        )


class Database:
    """SQLite wrapper. Use as context manager."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "Database":
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _ensure_schema(self):
        with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
            self._conn.executescript(f.read())
        self._conn.commit()

    # --- article ops ---

    def upsert_article(self, article: Article) -> bool:
        """
        Insert article. URL UNIQUE 제약 위반 시 무시 (중복 기사 스킵).
        Returns True if newly inserted, False if duplicate.
        """
        try:
            self._conn.execute(
                """
                INSERT INTO articles
                  (source, source_label, url, title, summary,
                   published_at, fetched_at, category, paywalled, raw_meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                article.to_row(),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_articles(
        self,
        days: int = 3,
        sources: Optional[Iterable[str]] = None,
        categories: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """
        Return articles published within `days` days (KST 기준은 호출자가 계산).
        published_at은 ISO 8601이므로 문자열 비교가 정상 동작 (timezone 일관성 가정).
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        sql = "SELECT * FROM articles WHERE published_at >= ?"
        params: list = [cutoff]

        if sources:
            placeholders = ",".join("?" for _ in sources)
            sql += f" AND source IN ({placeholders})"
            params.extend(sources)

        if categories:
            placeholders = ",".join("?" for _ in categories)
            sql += f" AND category IN ({placeholders})"
            params.extend(categories)

        sql += " ORDER BY published_at DESC"

        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def purge_old(self, retention_days: int = 90) -> int:
        """Remove articles older than retention_days. Returns deleted count."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        cursor = self._conn.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))
        return cursor.rowcount

    # --- run log ops ---

    @contextmanager
    def run_log(self, collector: str) -> Iterator["RunLogContext"]:
        ctx = RunLogContext(self._conn, collector)
        ctx.start()
        try:
            yield ctx
            ctx.finish("success")
        except Exception as e:
            ctx.finish("failed", error_message=str(e))
            raise


class RunLogContext:
    def __init__(self, conn: sqlite3.Connection, collector: str):
        self.conn = conn
        self.collector = collector
        self.run_id: Optional[int] = None
        self.fetched_count = 0
        self.new_count = 0

    def start(self):
        cur = self.conn.execute(
            "INSERT INTO run_log (run_started, collector, status) VALUES (?, ?, 'running')",
            (datetime.now(timezone.utc).isoformat(), self.collector),
        )
        self.run_id = cur.lastrowid

    def finish(self, status: str, error_message: Optional[str] = None):
        self.conn.execute(
            """UPDATE run_log
                  SET run_finished = ?, status = ?, fetched_count = ?,
                      new_count = ?, error_message = ?
                WHERE id = ?""",
            (
                datetime.now(timezone.utc).isoformat(),
                status,
                self.fetched_count,
                self.new_count,
                error_message,
                self.run_id,
            ),
        )
        self.conn.commit()
