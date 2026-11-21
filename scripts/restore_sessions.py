"""Restore sessions that are missing from the live database from a backup.

    python scripts/restore_sessions.py --backup <backup.db> --dry-run
    python scripts/restore_sessions.py --backup <backup.db>

The backup is the dashboard backup (`data/backups/opencode.db.backup-*`), a full
copy of the database. Every session that exists in the backup but not in the
live database is restored together with its messages, parts, instructions,
legacy mirror rows and events.

The `event_sequence` counter of a restored session is set to
`MAX(session_message.seq)`: a counter that is behind the messages makes the
next prompt fail with a UNIQUE(session_id, seq) violation, and the session
cannot send prompts at all.

Nothing is deleted by this script. It only inserts missing rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.config import load_settings  # noqa: E402

# rows that belong to a session and have to travel with it
SESSION_CHILDREN = ("session_message", "session_part", "instruction_entry",
                    "instruction_state", "session_inbox", "blob_ref", "schedule")
# rows of the legacy mirror (they hang on the legacy `session` table)
LEGACY_CHILDREN = ("message", "part", "todo")


def missing_sessions(con: sqlite3.Connection, only_with_messages: bool,
                     min_messages: int = 1, only_ids: list[str] | None = None) -> list[str]:
    sql = ("SELECT b.id FROM bk.session_v2 b WHERE NOT EXISTS "
           "(SELECT 1 FROM main.session_v2 v WHERE v.id = b.id)")
    if only_with_messages:
        sql += (" AND EXISTS (SELECT 1 FROM bk.session_message m WHERE m.session_id = b.id)")
    # only sessions whose project still exists: session_v2.project_id is a
    # foreign key, and a session without its project cannot be inserted
    sql += (" AND EXISTS (SELECT 1 FROM main.project p WHERE p.id = b.project_id)")
    if min_messages > 1:
        sql += (" AND (SELECT COUNT(*) FROM bk.session_message m "
                "WHERE m.session_id = b.id) >= %d" % min_messages)
    ids = [row[0] for row in con.execute(sql)]
    if only_ids:
        wanted = set(only_ids)
        ids = [i for i in ids if i in wanted]
    return ids


def placeholders(count: int) -> str:
    return ",".join("?" * count)


def restore(con: sqlite3.Connection, ids: list[str]) -> dict:
    stats: dict = {}
    if not ids:
        return stats
    marks = placeholders(len(ids))

    con.execute("INSERT OR IGNORE INTO main.session_v2 SELECT * FROM bk.session_v2 "
                "WHERE id IN (%s)" % marks, ids)
    stats["session_v2"] = con.total_changes

    for table in SESSION_CHILDREN:
        con.execute("INSERT OR IGNORE INTO main.%s SELECT * FROM bk.%s "
                    "WHERE session_id IN (%s)" % (table, table, marks), ids)

    # the legacy mirror rows of the same ids
    legacy = [row[0] for row in con.execute(
        "SELECT b.id FROM bk.session b WHERE b.id IN (%s) AND NOT EXISTS "
        "(SELECT 1 FROM main.session s WHERE s.id = b.id)" % marks, ids)]
    if legacy:
        legacy_marks = placeholders(len(legacy))
        con.execute("INSERT OR IGNORE INTO main.session SELECT * FROM bk.session "
                    "WHERE id IN (%s)" % legacy_marks, legacy)
        for table in LEGACY_CHILDREN:
            con.execute("INSERT OR IGNORE INTO main.%s SELECT * FROM bk.%s "
                        "WHERE session_id IN (%s)" % (table, table, legacy_marks), legacy)
    stats["legacy_sessions"] = len(legacy)

    # the events of the restored aggregates
    con.execute("INSERT OR IGNORE INTO main.event SELECT * FROM bk.event "
                "WHERE aggregate_id IN (%s)" % marks, ids)

    # the counter: never behind the highest message seq
    for sid in ids:
        max_seq = con.execute(
            "SELECT MAX(seq) FROM main.session_message WHERE session_id=?", (sid,)).fetchone()[0]
        if max_seq is None:
            continue
        con.execute(
            "INSERT INTO main.event_sequence (aggregate_id, seq, owner_id) VALUES (?, ?, NULL) "
            "ON CONFLICT(aggregate_id) DO UPDATE SET seq=MAX(seq, excluded.seq)",
            (sid, max_seq))
    stats["counters"] = len(ids)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True, help="a full copy of the database")
    parser.add_argument("--db", help="database to repair (default: the live database)")
    parser.add_argument("--dry-run", action="store_true", help="only report what would happen")
    parser.add_argument("--include-empty", action="store_true",
                        help="also restore sessions that have no messages")
    parser.add_argument("--min-messages", type=int, default=1,
                        help="only restore sessions with at least this many messages")
    parser.add_argument("--ids", help="comma separated session ids to restore")
    args = parser.parse_args()

    settings = load_settings()
    db_path = Path(args.db) if args.db else settings.db_path
    backup_path = Path(args.backup)
    if not backup_path.is_file():
        print("backup not found: %s" % backup_path, file=sys.stderr)
        return 2
    only_ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()] or None

    con = sqlite3.connect(db_path, timeout=300, isolation_level=None)
    con.execute("PRAGMA busy_timeout=300000")
    con.execute("ATTACH DATABASE ? AS bk", (str(backup_path),))
    try:
        ids = missing_sessions(con, only_with_messages=not args.include_empty,
                               min_messages=args.min_messages, only_ids=only_ids)
        print("database : %s" % db_path)
        print("backup   : %s" % backup_path)
        print("sessions to restore: %d" % len(ids))
        for sid in ids:
            row = con.execute("SELECT title, (SELECT COUNT(*) FROM bk.session_message m "
                              "WHERE m.session_id = bk.session_v2.id) FROM bk.session_v2 "
                              "WHERE id=?", (sid,)).fetchone()
            print("   %s  %5s msgs  %s" % (sid, row[1], row[0]))
        if not ids:
            print("nothing to do")
            return 0
        if args.dry_run:
            print("\ndry run: nothing was written")
            return 0

        before = con.execute("SELECT COUNT(*) FROM main.session_v2").fetchone()[0]
        con.execute("BEGIN IMMEDIATE")
        try:
            stats = restore(con, ids)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        after = con.execute("SELECT COUNT(*) FROM main.session_v2").fetchone()[0]
        violations = con.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0]
        print("\nrestored %d session(s): session_v2 %d -> %d" % (len(ids), before, after))
        print("legacy mirror rows restored: %d" % stats.get("legacy_sessions", 0))
        print("event counters aligned: %d" % stats.get("counters", 0))
        print("foreign key violations: %d" % violations)
        return 0 if violations == 0 else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
