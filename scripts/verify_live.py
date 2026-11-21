"""Read-only health report for the live OpenCode database.

    python scripts/verify_live.py            # report on the live database
    python scripts/verify_live.py --json     # machine readable output

Nothing is written and nothing is deleted: the database is opened read-only.
The exit code is non-zero when the foreign key check fails.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.config import load_settings  # noqa: E402

COUNT_TABLES = ("project", "session_v2", "session", "session_message", "session_part",
                "event", "event_sequence", "permission", "project_directory", "worktree")


def open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)


def report(con: sqlite3.Connection) -> dict:
    out: dict = {}
    out["quick_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
    out["foreign_key_violations"] = len(con.execute("PRAGMA foreign_key_check").fetchall())
    out["counts"] = {
        table: con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        for table in COUNT_TABLES
    }
    out["projects_owning_sessions"] = con.execute(
        "SELECT COUNT(*) FROM project p WHERE EXISTS "
        "(SELECT 1 FROM session_v2 v WHERE v.project_id = p.id)").fetchone()[0]
    out["projects_without_sessions"] = con.execute(
        "SELECT COUNT(*) FROM project p WHERE NOT EXISTS "
        "(SELECT 1 FROM session_v2 v WHERE v.project_id = p.id)").fetchone()[0]
    out["duplicate_worktrees"] = [
        {"worktree": row[0], "projects": row[1]}
        for row in con.execute(
            "SELECT worktree, COUNT(*) FROM project GROUP BY worktree HAVING COUNT(*) > 1")
    ]
    out["sessions_without_messages"] = con.execute(
        "SELECT COUNT(*) FROM session_v2 v WHERE NOT EXISTS "
        "(SELECT 1 FROM session_message m WHERE m.session_id = v.id)").fetchone()[0]
    out["seq_lag_sessions"] = [
        {"id": row[0], "title": row[1], "max_seq": row[2], "event_seq": row[3]}
        for row in con.execute(
            "SELECT v.id, v.title, MAX(m.seq), es.seq FROM session_v2 v "
            "JOIN session_message m ON m.session_id = v.id "
            "LEFT JOIN event_sequence es ON es.aggregate_id = v.id "
            "GROUP BY v.id HAVING es.seq IS NULL OR es.seq < MAX(m.seq)")
    ]
    out["orphan_event_sequences"] = con.execute(
        "SELECT COUNT(*) FROM event_sequence es WHERE es.aggregate_id LIKE 'ses_%' "
        "AND NOT EXISTS (SELECT 1 FROM session_v2 v WHERE v.id = es.aggregate_id)").fetchone()[0]
    out["other_event_sequences"] = con.execute(
        "SELECT COUNT(*) FROM event_sequence WHERE aggregate_id NOT LIKE 'ses_%'").fetchone()[0]
    out["sessions_outside_default_project"] = [
        {"id": row[0], "directory": row[1]}
        for row in con.execute(
            "SELECT id, directory FROM session_v2 WHERE directory NOT LIKE "
            "'C:/Users/Damianos/Documents/Default Project%' LIMIT 20")
    ]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="database file (default: the live OpenCode database)")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    settings = load_settings()
    path = Path(args.db) if args.db else settings.db_path
    con = open_readonly(path)
    try:
        data = report(con)
    finally:
        con.close()

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("database: %s" % path)
        print("quick_check: %s" % data["quick_check"])
        print("foreign key violations: %d" % data["foreign_key_violations"])
        print("\nrow counts")
        for table, count in data["counts"].items():
            print("  %-18s %d" % (table, count))
        print("\nprojects owning sessions: %d" % data["projects_owning_sessions"])
        print("projects without sessions: %d" % data["projects_without_sessions"])
        print("sessions without messages: %d" % data["sessions_without_messages"])
        print("orphan session counters: %d" % data["orphan_event_sequences"])
        print("other counters (projects/global): %d" % data["other_event_sequences"])
        if data["duplicate_worktrees"]:
            print("\nduplicate worktrees:")
            for entry in data["duplicate_worktrees"]:
                print("  %s -> %d project rows" % (entry["worktree"], entry["projects"]))
        if data["seq_lag_sessions"]:
            print("\nseq-lag sessions (event_sequence behind the messages):")
            for entry in data["seq_lag_sessions"]:
                print("  %s  max_seq=%s event_seq=%s  %s"
                      % (entry["id"], entry["max_seq"], entry["event_seq"], entry["title"]))
        if data["sessions_outside_default_project"]:
            print("\nsessions outside the default project:")
            for entry in data["sessions_outside_default_project"]:
                print("  %s  %s" % (entry["id"], entry["directory"]))

    return 1 if data["foreign_key_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
