"""SQLite run cache: feeds, articles, LLM answers and the published history."""

from __future__ import annotations

import time
import sqlite3
import threading

from . import state


# ============================================================================
# Cache (SQLite)
# ============================================================================
class Cache:
    """A small SQLite cache, safe to share between worker threads.

    feeds      raw discovery responses (short TTL): reruns don't re-query feeds
    articles   resolved URL + extracted body per story: Chrome never re-opens it
    bullets    LLM bullet per (story, provider, model): no repeat LLM calls
    published  stories that made it into a digest: lets ``--new-only`` skip them

    ``Cache(None)`` is a disabled cache: every lookup misses, writes do nothing.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS feeds     (url TEXT PRIMARY KEY, body BLOB, fetched_at REAL);
    CREATE TABLE IF NOT EXISTS articles  (key TEXT PRIMARY KEY, final_url TEXT, body TEXT,
                                          authentic INTEGER, fetched_at REAL);
    CREATE TABLE IF NOT EXISTS bullets   (key TEXT, provider TEXT, model TEXT, bullet TEXT,
                                          created_at REAL, PRIMARY KEY (key, provider, model));
    CREATE TABLE IF NOT EXISTS published (key TEXT PRIMARY KEY, company TEXT, digest_date TEXT,
                                          published_at REAL);
    """

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self._db = None
        if path:
            self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
            with self._lock:
                self._db.executescript(self.SCHEMA)
                # caches created before the page-date column existed
                cols = {r[1] for r in self._db.execute("PRAGMA table_info(articles)")}
                if "published" not in cols:
                    self._db.execute("ALTER TABLE articles ADD COLUMN published TEXT DEFAULT ''")
                self._prune()

    @property
    def enabled(self) -> bool:
        return self._db is not None

    def _q(self, sql: str, args=(), one=False):
        if self._db is None:
            return None
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchone() if one else cur.fetchall()
            self._db.commit()
            return rows

    def _prune(self):
        now = time.time()
        self._db.execute("DELETE FROM feeds WHERE fetched_at < ?", (now - 2 * 86400,))
        self._db.execute("DELETE FROM articles WHERE fetched_at < ?", (now - 120 * 86400,))
        self._db.execute("DELETE FROM bullets WHERE created_at < ?", (now - 120 * 86400,))
        self._db.execute("DELETE FROM published WHERE published_at < ?", (now - 365 * 86400,))
        self._db.commit()

    # feeds --------------------------------------------------------------------
    def get_feed(self, url: str, max_age_s: float) -> bytes | None:
        row = self._q("SELECT body, fetched_at FROM feeds WHERE url = ?", (url,), one=True)
        if row and time.time() - row[1] <= max_age_s:
            return row[0]
        return None

    def put_feed(self, url: str, body: bytes) -> None:
        self._q("INSERT OR REPLACE INTO feeds VALUES (?, ?, ?)", (url, body, time.time()))

    # articles -------------------------------------------------------------------
    def get_article(self, key: str, max_age_s: float, failed_max_age_s: float) -> dict | None:
        if not key:
            return None
        row = self._q("SELECT final_url, body, authentic, fetched_at, published FROM articles WHERE key = ?",
                      (key,), one=True)
        if not row:
            return None
        age = time.time() - row[3]
        if age > (max_age_s if row[2] else failed_max_age_s):
            return None
        return {"final_url": row[0], "body": row[1], "authentic": bool(row[2]), "published": row[4] or ""}

    def put_article(self, key: str, final_url: str, body: str, authentic: bool, published: str = "") -> None:
        if key:
            self._q("INSERT OR REPLACE INTO articles (key, final_url, body, authentic, fetched_at, published) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (key, final_url, body, int(authentic), time.time(), published))

    # bullets --------------------------------------------------------------------
    def get_bullet(self, key: str, provider: str, model: str) -> str | None:
        row = self._q("SELECT bullet FROM bullets WHERE key = ? AND provider = ? AND model = ?",
                      (key, provider, model), one=True)
        return row[0] if row else None

    def put_bullet(self, key: str, provider: str, model: str, bullet: str) -> None:
        if key and bullet:
            self._q("INSERT OR REPLACE INTO bullets VALUES (?, ?, ?, ?, ?)",
                    (key, provider, model, bullet, time.time()))

    # published --------------------------------------------------------------------
    def was_published(self, *keys: str) -> bool:
        keys = [k for k in keys if k]
        if not keys or self._db is None:
            return False
        marks = ",".join("?" * len(keys))
        return bool(self._q(f"SELECT 1 FROM published WHERE key IN ({marks}) LIMIT 1", keys, one=True))

    def mark_published(self, keys, company: str, digest_date: str) -> None:
        for k in keys:
            if k:
                self._q("INSERT OR REPLACE INTO published VALUES (?, ?, ?, ?)",
                        (k, company, digest_date, time.time()))

    def close(self) -> None:
        if self._db is not None:
            with self._lock:
                self._db.close()
            self._db = None


# The cache in use for the current run. run() swaps in a real one; everything
# else (tests, one-off calls) gets a disabled cache.
state._CACHE = Cache(None)
