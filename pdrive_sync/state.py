"""SQLite state store — the 'S' (last-synced snapshot) in the 3-way sync.

One row per synced path, keyed by POSIX-style relative path with '/'
separators, e.g. 'Documents/report.pdf'. Root is ''.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    path        TEXT PRIMARY KEY,
    is_dir      INTEGER NOT NULL,
    local_sha1  TEXT,
    local_size  INTEGER,
    local_mtime REAL,
    remote_uid  TEXT,
    remote_sha1 TEXT,
    remote_mtime REAL,
    synced_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(path);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# The Proton CLI sanitizes Windows-illegal chars to '_' in local filenames on
# download, so the on-disk local path can differ from the remote/snapshot path.
# We record the actual local path per node (NULL when identical to `path`).
_ALTER = "ALTER TABLE nodes ADD COLUMN local_path TEXT"


class StateDB:
    def __init__(self, path=None):
        config.ensure_dirs()
        self.path = str(path or config.DB_PATH)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        # migrate: add local_path if missing
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(nodes)")]
        if "local_path" not in cols:
            self.conn.execute(_ALTER)
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- node CRUD ---------------------------------------------------------
    def get(self, path: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM nodes WHERE path=?", (path,))
        return cur.fetchone()

    def upsert(
        self,
        path: str,
        is_dir: bool,
        local_sha1=None,
        local_size=None,
        local_mtime=None,
        remote_uid=None,
        remote_sha1=None,
        remote_mtime=None,
        local_path=None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO nodes
               (path,is_dir,local_sha1,local_size,local_mtime,remote_uid,remote_sha1,remote_mtime,local_path,synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 is_dir=excluded.is_dir,
                 local_sha1=excluded.local_sha1,
                 local_size=excluded.local_size,
                 local_mtime=excluded.local_mtime,
                 remote_uid=excluded.remote_uid,
                 remote_sha1=excluded.remote_sha1,
                 remote_mtime=excluded.remote_mtime,
                 local_path=COALESCE(excluded.local_path, nodes.local_path),
                 synced_at=excluded.synced_at
            """,
            (
                path,
                1 if is_dir else 0,
                local_sha1,
                local_size,
                local_mtime,
                remote_uid,
                remote_sha1,
                remote_mtime,
                local_path,
                time.time(),
            ),
        )
        self.conn.commit()

    def set_local_path(self, path: str, local_path: str) -> None:
        self.conn.execute("UPDATE nodes SET local_path=? WHERE path=?", (local_path, path))
        self.conn.commit()

    def local_path_for(self, row) -> str:
        """The on-disk relative path for a snapshot row (== path when unsanitized)."""
        lp = row["local_path"] if "local_path" in row.keys() else None
        return lp or row["path"]

    def update_local(self, path: str, sha1, size, mtime) -> None:
        self.conn.execute(
            "UPDATE nodes SET local_sha1=?, local_size=?, local_mtime=?, synced_at=? WHERE path=?",
            (sha1, size, mtime, time.time(), path),
        )
        self.conn.commit()

    def update_remote(self, path: str, uid, sha1, mtime) -> None:
        self.conn.execute(
            "UPDATE nodes SET remote_uid=?, remote_sha1=?, remote_mtime=?, synced_at=? WHERE path=?",
            (uid, sha1, mtime, time.time(), path),
        )
        self.conn.commit()

    def delete(self, path: str) -> None:
        self.conn.execute("DELETE FROM nodes WHERE path=?", (path,))
        self.conn.commit()

    def delete_under(self, path: str) -> None:
        """Delete a path and everything beneath it (for directory deletes)."""
        self.conn.execute("DELETE FROM nodes WHERE path=? OR path LIKE ?", (path, path.rstrip("/") + "/%"))
        self.conn.commit()

    def all_paths(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM nodes ORDER BY path"))

    def children_of(self, prefix: str) -> list[sqlite3.Row]:
        if prefix == "":
            return self.all_paths()
        return list(self.conn.execute("SELECT * FROM nodes WHERE path LIKE ?", (prefix.rstrip("/") + "/%",)))

    # -- meta --------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
