"""SQLite access layer for the OpenCode dashboard.

The live database is in WAL mode and is being written by the running OpenCode
service, so every read uses a short-lived read-only connection with a busy
timeout. Write operations (maintenance) use their own connection and never
stop the service.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

IDENT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def quote_ident(name: str) -> str:
    """Quote a SQL identifier safely (table/column names come from user input)."""
    if not name or not set(name) <= IDENT_OK:
        raise ValueError("invalid identifier: %r" % name)
    return '"%s"' % name


class Database:
    """Thin wrapper around the OpenCode SQLite database."""

    def __init__(self, path: Path | str, busy_timeout_ms: int = 30_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.Lock()
        self._schema_cache: dict[str, Any] = {}
        self._schema_at = 0.0
        self._counts_cache: dict[str, Any] = {}
        self._counts_at = 0.0

    # ------------------------------------------------------------------ open
    def exists(self) -> bool:
        return self.path.is_file()

    def _uri(self, readonly: bool) -> str:
        p = str(self.path).replace("\\", "/")
        return "file:%s?mode=%s" % (p, "ro" if readonly else "rwc")

    def connect(self, readonly: bool = True) -> sqlite3.Connection:
        con = sqlite3.connect(self._uri(readonly), uri=True, timeout=self.busy_timeout_ms / 1000)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=%d" % self.busy_timeout_ms)
        if not readonly:
            con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self.connect(readonly=True)
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        con = self.connect(readonly=False)
        con.execute("PRAGMA busy_timeout=%d" % (self.busy_timeout_ms * 6))
        try:
            yield con
        finally:
            con.close()

    # --------------------------------------------------------------- queries
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.read() as con:
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self.read() as con:
            row = con.execute(sql, params).fetchone()
            return None if row is None else row[0]

    def table_names(self, refresh: bool = False) -> list[str]:
        if refresh:
            self._schema_cache.clear()
            self._schema_at = 0.0
        if "tables" in self._schema_cache:
            return list(self._schema_cache["tables"])
        with self.read() as con:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        self._schema_cache["tables"] = names
        self._schema_at = time.time()
        return list(names)

    def columns(self, table: str) -> list[dict]:
        key = "cols:" + table
        if key in self._schema_cache:
            return list(self._schema_cache[key])
        with self.read() as con:
            rows = con.execute("PRAGMA table_info(%s)" % quote_ident(table)).fetchall()
        cols = [
            {"name": r[1], "type": r[2], "notnull": bool(r[3]), "default": r[4], "pk": bool(r[5])}
            for r in rows
        ]
        self._schema_cache[key] = cols
        return list(cols)

    def column_names(self, table: str) -> list[str]:
        return [c["name"] for c in self.columns(table)]

    def indexes(self, table: str) -> list[dict]:
        key = "idx:" + table
        if key in self._schema_cache:
            return list(self._schema_cache[key])
        with self.read() as con:
            out = []
            for row in con.execute("PRAGMA index_list(%s)" % quote_ident(table)).fetchall():
                name = row[1]
                cols = [c[2] for c in con.execute("PRAGMA index_info(%s)" % quote_ident(name))]
                out.append({"name": name, "unique": bool(row[2]), "origin": row[3], "columns": cols})
        self._schema_cache[key] = out
        return list(out)

    def foreign_keys(self, table: str) -> list[dict]:
        key = "fk:" + table
        if key in self._schema_cache:
            return list(self._schema_cache[key])
        with self.read() as con:
            rows = con.execute("PRAGMA foreign_key_list(%s)" % quote_ident(table)).fetchall()
        out = [{"from": r[3], "to_table": r[2], "to_column": r[4], "on_delete": r[6]} for r in rows]
        self._schema_cache[key] = out
        return list(out)

    def row_counts(self, max_age: float = 15.0) -> dict[str, int]:
        if time.time() - self._counts_at < max_age:
            return dict(self._counts_cache)
        counts: dict[str, int] = {}
        with self.read() as con:
            for table in self.table_names():
                try:
                    counts[table] = con.execute(
                        "SELECT COUNT(*) FROM %s" % quote_ident(table)).fetchone()[0]
                except sqlite3.Error:
                    counts[table] = -1
        self._counts_cache = counts
        self._counts_at = time.time()
        return dict(counts)

    def pragmas(self) -> dict:
        with self.read() as con:
            out = {
                "page_count": con.execute("PRAGMA page_count").fetchone()[0],
                "freelist_count": con.execute("PRAGMA freelist_count").fetchone()[0],
                "page_size": con.execute("PRAGMA page_size").fetchone()[0],
                "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0],
                "user_version": con.execute("PRAGMA user_version").fetchone()[0],
                "application_id": con.execute("PRAGMA application_id").fetchone()[0],
            }
        return out

    def file_stats(self) -> dict:
        def size(p: Path) -> int:
            try:
                return p.stat().st_size
            except OSError:
                return 0

        db = self.path
        return {
            "db_bytes": size(db),
            "wal_bytes": size(Path(str(db) + "-wal")),
            "shm_bytes": size(Path(str(db) + "-shm")),
            "db_mtime": db.stat().st_mtime if db.exists() else None,
        }

    def integrity(self, deep: bool = False) -> dict:
        with self.read() as con:
            check = con.execute("PRAGMA quick_check" if not deep else "PRAGMA integrity_check")
            result = check.fetchone()[0]
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
        return {
            "check": result,
            "ok": result == "ok",
            "foreign_key_violations": len(fk),
            "violations": [dict(zip([d[0] for d in con.description], row)) for row in fk[:20]]
            if False else [list(row) for row in fk[:20]],
        }


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
