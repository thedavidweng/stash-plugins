"""
SQLite ledger tracking published files, remote message IDs, hashes, and skipped files.
Provides ground truth for incremental syncs, audits, and disaster recovery.
"""
import contextlib
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional


class Ledger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ledger_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER,
                    canonical_path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    blake3 TEXT,
                    message_id INTEGER,
                    status TEXT NOT NULL,
                    reason TEXT,
                    updated_at TEXT NOT NULL,
                    remote_path TEXT,
                    fingerprint TEXT
                )
            """)
            # ledgers created before newer columns existed gain them in
            # place; remote_path is derived from the canonical path when
            # absent, fingerprint is the Stash content fingerprint recorded
            # at publish time (used for content-change detection).
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ledger_items)").fetchall()
            }
            if "remote_path" not in columns:
                conn.execute("ALTER TABLE ledger_items ADD COLUMN remote_path TEXT")
            if "fingerprint" not in columns:
                conn.execute("ALTER TABLE ledger_items ADD COLUMN fingerprint TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_path ON ledger_items (canonical_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger_items (status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_updated_at ON ledger_items (updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_remote ON ledger_items (remote_path)")

    def get(self, canonical_path: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM ledger_items WHERE canonical_path = ?", (canonical_path,))
            row = cur.fetchone()
            if row:
                return dict(row)
        return None

    def record_success(
        self,
        canonical_path: str,
        kind: str,
        size: int,
        file_id: Optional[int] = None,
        blake3: Optional[str] = None,
        message_id: Optional[int] = None,
        remote_path: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> None:
        """
        Record a published item.

        Archive and artifact rows mirror the remote path exactly, so their
        remote_path defaults to the canonical path. "browse:"-prefixed keys
        (mode-scoped records) are NOT remote paths; those rows keep NULL
        unless the caller records the actual remote slot, and a NULL never
        overwrites a previously recorded path. fingerprint is the Stash
        content fingerprint recorded at publish time; the backup engine
        compares it to detect changed bytes at an unchanged path.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        default_remote = canonical_path if canonical_path.startswith("/") else None
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO ledger_items (file_id, canonical_path, kind, size, blake3, message_id, status, reason, updated_at, remote_path, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, 'published', NULL, ?, ?, ?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    file_id=excluded.file_id,
                    kind=excluded.kind,
                    size=excluded.size,
                    blake3=excluded.blake3,
                    message_id=excluded.message_id,
                    status='published',
                    reason=NULL,
                    updated_at=excluded.updated_at,
                    remote_path=COALESCE(excluded.remote_path, ledger_items.remote_path),
                    fingerprint=excluded.fingerprint
            """, (file_id, canonical_path, kind, size, blake3, message_id, now, remote_path or default_remote, fingerprint))

    def record_skip(
        self,
        canonical_path: str,
        kind: str,
        size: int,
        reason: str,
        file_id: Optional[int] = None,
    ) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO ledger_items (file_id, canonical_path, kind, size, blake3, message_id, status, reason, updated_at)
                VALUES (?, ?, ?, ?, NULL, NULL, 'skipped', ?, ?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    file_id=excluded.file_id,
                    kind=excluded.kind,
                    size=excluded.size,
                    status='skipped',
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
            """, (file_id, canonical_path, kind, size, reason, now))

    def record_failure(
        self,
        canonical_path: str,
        kind: str,
        size: int,
        reason: str,
        file_id: Optional[int] = None,
    ) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO ledger_items (file_id, canonical_path, kind, size, blake3, message_id, status, reason, updated_at)
                VALUES (?, ?, ?, ?, NULL, NULL, 'failed', ?, ?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    file_id=excluded.file_id,
                    kind=excluded.kind,
                    size=excluded.size,
                    status='failed',
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
            """, (file_id, canonical_path, kind, size, reason, now))

    def all_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if status:
                cur = conn.execute("SELECT * FROM ledger_items WHERE status = ?", (status,))
            else:
                cur = conn.execute("SELECT * FROM ledger_items")
            return [dict(row) for row in cur.fetchall()]

    def get_summary(self) -> Dict[str, Any]:
        with self._get_conn() as conn:
            cur = conn.execute("""
                SELECT
                    status,
                    COUNT(*) as count,
                    COALESCE(SUM(size), 0) as total_bytes
                FROM ledger_items
                GROUP BY status
            """)
            rows = cur.fetchall()
            stats = {
                "published_count": 0,
                "published_bytes": 0,
                "skipped_count": 0,
                "skipped_bytes": 0,
                "failed_count": 0,
                "failed_bytes": 0,
                "total_count": 0,
                "total_bytes": 0,
            }
            for row in rows:
                status = row["status"]
                cnt = row["count"]
                b = row["total_bytes"]
                stats["total_count"] += cnt
                stats["total_bytes"] += b
                if status == "published":
                    stats["published_count"] = cnt
                    stats["published_bytes"] = b
                elif status == "skipped":
                    stats["skipped_count"] = cnt
                    stats["skipped_bytes"] = b
                elif status == "failed":
                    stats["failed_count"] = cnt
                    stats["failed_bytes"] = b

            kind_cur = conn.execute("""
                SELECT
                    kind,
                    COUNT(*) as count,
                    COALESCE(SUM(size), 0) as total_bytes
                FROM ledger_items
                WHERE status = 'published'
                GROUP BY kind
                ORDER BY kind
            """)
            stats["published_by_kind"] = {
                row["kind"]: {"count": row["count"], "bytes": row["total_bytes"]}
                for row in kind_cur.fetchall()
            }
            return stats
