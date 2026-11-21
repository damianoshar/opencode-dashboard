"""Maintenance operations: backup, verify, shrink, prune, move, delete, export, repair.

Every mutating operation must be called with ``confirm=True`` and is written to
an audit log. The OpenCode service is never stopped - SQLite serialises the work
and the API keeps the app's projections consistent.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Settings
from .db import Database, quote_ident


class OperationError(RuntimeError):
    """Raised when a maintenance operation cannot complete."""


@dataclass
class OperationResult:
    name: str
    ok: bool
    started: str
    finished: str
    duration_s: float
    details: dict[str, Any]
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "started": self.started,
            "finished": self.finished,
            "duration_s": round(self.duration_s, 2),
            "details": self.details,
            "message": self.message,
        }


class Maintenance:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.settings.ensure_dirs()

    # ------------------------------------------------------------- audit log
    def audit(self, action: str, payload: dict, ok: bool, message: str = "") -> None:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "ok": ok,
            "message": message,
            "payload": payload,
        }
        try:
            self.settings.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self.settings.audit_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def audit_entries(self, limit: int = 100) -> list[dict]:
        path = self.settings.audit_log
        if not path.exists():
            return []
        entries = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            return []
        return entries[-limit:][::-1]

    # ------------------------------------------------------------- opencode cli
    def _opencode_bin(self) -> str:
        override = os.environ.get("OPENCODE_BIN")
        if override and Path(override).is_file():
            return override
        found = shutil.which("opencode")
        if found and Path(found).suffix.lower() not in {".ps1", ".cmd", ".bat"}:
            return found
        for candidate in (
            Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@opencode" /
            "cli" / "bin" / "opencode.exe",
            Path.home() / ".opencode" / "bin" / "opencode.exe",
            Path("/usr/local/bin/opencode"),
            Path.home() / ".local" / "bin" / "opencode",
        ):
            if candidate and candidate.is_file():
                return str(candidate)
        raise OperationError("cannot find the opencode executable; set OPENCODE_BIN")

    def api(self, args: Sequence[str], timeout: int = 300) -> str:
        result = subprocess.run([self._opencode_bin(), *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        if result.returncode != 0:
            raise OperationError(((result.stdout or "") + (result.stderr or "")).strip()[:800])
        return (result.stdout or "").strip()

    def api_post(self, route: str, body: dict, timeout: int = 300) -> str:
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return self.api(["api", "post", route, "--data", payload], timeout=timeout)

    def api_delete_session(self, session_id: str) -> None:
        # the OpenAPI operation id is `session.remove`; `session.delete` does not exist
        # (the server answers "Operation not found" and the session survives).
        self.api(["api", "session.remove", "--param", "sessionID=" + session_id])

    def remove_session(self, session_id: str) -> tuple[bool, str | None]:
        """Delete one session through the OpenCode API.

        Returns (removed, error). A session that is already gone counts as
        removed, so the operation is idempotent.
        """
        try:
            self.api_delete_session(session_id)
            return True, None
        except OperationError as exc:
            text = str(exc)
            if "SessionNotFoundError" in text or "HTTP 404" in text \
                    or "not found" in text.lower():
                return True, None
            return False, text[:200]

    # -------------------------------------------------------------- inspection
    def verify(self, deep: bool = False) -> dict:
        result = self.db.integrity(deep=deep)
        result["pragmas"] = self.db.pragmas()
        result["files"] = self.db.file_stats()
        result["checked_at"] = datetime.now().isoformat(timespec="seconds")
        return result

    def list_backups(self) -> dict:
        """Every backup we can find: live-dir snapshots, dashboard backups, recovery dirs."""
        items: list[dict] = []

        def add(path: Path, kind: str, source: str) -> None:
            try:
                stat = path.stat()
            except OSError:
                return
            items.append({
                "path": str(path),
                "name": path.name,
                "kind": kind,
                "source": source,
                "bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })

        live_dir = self.settings.opencode_dir
        if live_dir.is_dir():
            for path in sorted(live_dir.glob("opencode.db.backup-*")):
                if path.suffix in (".db", ".db-wal", ".db-shm") or "-wal" in path.name \
                        or "-shm" in path.name:
                    continue
                add(path, "database", "opencode data dir")
            add(live_dir / "opencode.db", "live", "opencode data dir")

        if self.settings.backup_dir.is_dir():
            for path in sorted(self.settings.backup_dir.glob("*.db")):
                add(path, "database", "dashboard backups")
            for path in sorted(self.settings.backup_dir.glob("*.json")):
                add(path, "manifest", "dashboard backups")

        recovery_root = Path.home() / "opencode-recovery"
        if recovery_root.is_dir():
            for path in sorted(recovery_root.rglob("opencode.db")):
                add(path, "database", "recovery workspace")

        items.sort(key=lambda i: i["mtime"], reverse=True)
        return {"items": items, "total": len(items),
                "dashboard_backup_dir": str(self.settings.backup_dir)}

    def backup_status(self, path: str) -> dict:
        """Integrity + row counts for one backup file."""
        target = Path(path)
        if not target.is_file():
            raise OperationError("backup not found: %s" % target)
        uri = "file:%s?mode=ro" % str(target).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            check = con.execute("PRAGMA quick_check").fetchone()[0]
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
            counts = {}
            for (table,) in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                try:
                    counts[table] = con.execute(
                        "SELECT COUNT(*) FROM %s" % quote_ident(table)).fetchone()[0]
                except sqlite3.Error:
                    counts[table] = -1
            pages = con.execute("PRAGMA page_count").fetchone()[0]
            free = con.execute("PRAGMA freelist_count").fetchone()[0]
        finally:
            con.close()
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "quick_check": check,
            "ok": check == "ok",
            "foreign_key_violations": len(fk),
            "page_count": pages,
            "free_pages": free,
            "counts": counts,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    # ---------------------------------------------------------------- mutation
    def create_backup(self, label: str = "", to_opencode_dir: bool = False,
                      confirm: bool = False) -> OperationResult:
        name = "create_backup"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("create_backup requires confirm=true")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_label = "".join(c for c in (label or "") if c.isalnum() or c in "-_").strip()
        filename = "opencode.db.backup-%s%s" % (stamp, ("-" + safe_label) if safe_label else "")
        if to_opencode_dir:
            destination = self.settings.opencode_dir / filename
        else:
            destination = self.settings.backup_dir / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source = sqlite3.connect("file:%s?mode=ro" % str(self.db.path).replace("\\", "/"),
                                     uri=True)
            dest = sqlite3.connect(str(destination))
            try:
                source.backup(dest)
            finally:
                dest.close()
                source.close()
            status = self.backup_status(str(destination))
            manifest = {
                "created": datetime.now().isoformat(timespec="seconds"),
                "source": str(self.db.path),
                "file": destination.name,
                "bytes": destination.stat().st_size,
                "label": label,
                "quick_check": status["quick_check"],
                "counts": status["counts"],
            }
            (destination.parent / (destination.name + ".json")).write_text(
                json.dumps(manifest, indent=2), encoding="utf-8")
            result = OperationResult(
                name, True, started, datetime.now().isoformat(timespec="seconds"),
                time.time() - t0,
                {"path": str(destination), "bytes": destination.stat().st_size,
                 "quick_check": status["quick_check"], "counts": status["counts"]},
                "backup written and verified")
            self.audit(name, {"path": str(destination), "label": label}, True, result.message)
            return result
        except Exception as exc:  # noqa: BLE001
            self.audit(name, {"label": label}, False, str(exc))
            raise OperationError(str(exc)) from exc

    def checkpoint(self, truncate: bool = True, confirm: bool = False) -> OperationResult:
        name = "checkpoint"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("checkpoint requires confirm=true")
        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        try:
            res = con.execute("PRAGMA wal_checkpoint(%s)" % ("TRUNCATE" if truncate else "PASSIVE")
                              ).fetchone()
        finally:
            con.close()
        result = OperationResult(name, True, started, datetime.now().isoformat(timespec="seconds"),
                                 time.time() - t0, {"checkpoint": list(res)},
                                 "WAL checkpoint complete")
        self.audit(name, {"truncate": truncate}, True, result.message)
        return result

    def vacuum(self, secure: bool = True, confirm: bool = False) -> OperationResult:
        name = "vacuum"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("vacuum requires confirm=true")
        before = self.db.file_stats()
        con = sqlite3.connect(str(self.db.path), timeout=900, isolation_level=None)
        con.execute("PRAGMA busy_timeout=900000")
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if secure:
                con.execute("PRAGMA secure_delete=ON")
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
        after = self.db.file_stats()
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"before_bytes": before["db_bytes"], "after_bytes": after["db_bytes"],
             "saved_bytes": before["db_bytes"] - after["db_bytes"], "secure_delete": secure},
            "database rewritten")
        self.audit(name, {"secure": secure}, True, result.message)
        return result

    def prune_projects(self, keep: str, confirm: bool = False) -> OperationResult:
        """Reduce the project registry to one project (no project-delete API exists)."""
        name = "prune_projects"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("prune_projects requires confirm=true")
        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            keep_id, keep_dir = self._resolve_project(con, keep)
            foreign = con.execute(
                "SELECT COUNT(*) FROM session_v2 WHERE project_id<>?", (keep_id,)).fetchone()[0]
            if foreign:
                raise OperationError(
                    "%d sessions still belong to other projects - move them first" % foreign)
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "UPDATE session SET directory=?, project_id=?, path='' "
                    "WHERE directory<>? OR project_id<>? OR COALESCE(path,'')<>''",
                    (keep_dir, keep_id, keep_dir, keep_id))
                legacy_updated = con.execute("SELECT changes()").fetchone()[0]
                deleted: dict[str, int] = {}
                for table in ("permission", "project_directory", "worktree"):
                    con.execute("DELETE FROM %s WHERE project_id<>?" % quote_ident(table), (keep_id,))
                    deleted[table] = con.execute("SELECT changes()").fetchone()[0]
                con.execute("DELETE FROM project WHERE id<>?", (keep_id,))
                deleted["project"] = con.execute("SELECT changes()").fetchone()[0]
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"keep": keep_id, "keep_directory": keep_dir, "deleted": deleted,
             "legacy_rows_repointed": legacy_updated},
            "project registry pruned")
        self.audit(name, {"keep": keep}, True, result.message)
        return result

    def _resolve_project(self, con: sqlite3.Connection, value: str) -> tuple[str, str]:
        rows = con.execute("SELECT id, worktree FROM project").fetchall()
        if not rows:
            raise OperationError("no projects in the database")
        if len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower()):
            for pid, worktree in rows:
                if pid.lower() == value.lower():
                    return pid, worktree
            raise OperationError("no project with id %s" % value)
        wanted = value.replace("\\", "/").rstrip("/").lower()
        hits = [(pid, worktree) for pid, worktree in rows
                if (worktree or "").replace("\\", "/").rstrip("/").lower() == wanted]
        if not hits:
            hits = [(pid, worktree) for pid, worktree in rows
                    if (worktree or "").replace("\\", "/").rstrip("/").lower().endswith(wanted)]
        if not hits:
            raise OperationError("no project matches %r" % value)
        if len(hits) > 1:
            raise OperationError("ambiguous directory: " + ", ".join(h for _, h in hits))
        return hits[0]

    def move_sessions(self, target: str, ids: Iterable[str] | None = None,
                      filters: dict | None = None, all_sessions: bool = False,
                      confirm: bool = False) -> OperationResult:
        name = "move_sessions"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("move_sessions requires confirm=true")
        target_path = Path(target).expanduser()
        if not target_path.is_dir():
            raise OperationError("target directory does not exist: %s" % target_path)
        wanted = set(ids or [])
        if not wanted and filters is None and not all_sessions:
            raise OperationError("move_sessions requires ids, filters or all=true")
        rows = self.db.query("SELECT id, directory FROM session_v2 ORDER BY time_created")
        selected = [r["id"] for r in rows if all_sessions or r["id"] in wanted]
        if filters is not None:
            selected = list(dict.fromkeys(selected + self.resolve_session_ids(filters)))
        if not selected:
            raise OperationError("no sessions selected")
        moved, failed = [], []
        for sid in selected:
            try:
                self.api_post("/api/session/%s/move" % sid, {"directory": str(target_path)})
                moved.append(sid)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": sid, "error": str(exc)[:200]})
        result = OperationResult(
            name, not failed, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"target": str(target_path), "moved": len(moved), "failed": failed,
             "ids": moved},
            "moved %d of %d sessions" % (len(moved), len(selected)))
        self.audit(name, {"target": str(target_path), "count": len(selected)},
                   not failed, result.message)
        return result

    def resolve_session_ids(self, filters: dict | None) -> list[str]:
        """Resolve session ids from a filter set (same filters as the sessions view).

        `filters=None` means "no selection"; an empty filter set means "every
        session", which is what "select all matching" means when no filter is
        active.
        """
        if filters is None:
            return []
        from .queries import SessionFilters

        session_filters = SessionFilters(
            project_id=filters.get("project_id"),
            directory=filters.get("directory"),
            agent=filters.get("agent"),
            model=filters.get("model"),
            state=filters.get("state"),
            kind=filters.get("kind"),
            search=filters.get("search"),
            created_from=filters.get("created_from"),
            created_to=filters.get("created_to"),
        )
        where, params = session_filters.where()
        rows = self.db.query("SELECT v.id FROM session_v2 v WHERE " + where, params)
        return [r["id"] for r in rows]

    def resolve_project_ids(self, filters: dict | None) -> list[str]:
        """Resolve project ids from a filter set.

        `filters=None` means "no selection"; an empty search matches every
        project, which is what "select all matching" means when the search box
        is empty.
        """
        if filters is None:
            return []
        search = (filters.get("search") or "").strip()
        like = "%" + search + "%"
        rows = self.db.query(
            "SELECT id FROM project WHERE worktree LIKE ? OR id LIKE ? OR name LIKE ?",
            [like, like, like])
        return [r["id"] for r in rows]

    def delete_sessions(self, ids: Iterable[str], filters: dict | None = None,
                        confirm: bool = False) -> OperationResult:
        name = "delete_sessions"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("delete_sessions requires confirm=true")
        selected = [i for i in ids if i]
        if filters is not None:
            selected = list(dict.fromkeys(selected + self.resolve_session_ids(filters)))
        if not selected:
            raise OperationError("no sessions selected")
        api_failures = []
        removed = []
        for sid in selected:
            ok, error = self.remove_session(sid)
            if ok:
                removed.append(sid)
            else:
                api_failures.append({"id": sid, "error": error})
        # Only purge the mirror rows of sessions the API actually removed: wiping
        # the rows of a session that is still alive would destroy its content and
        # leave an empty shell behind.
        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                for sid in removed:
                    for table in ("session_part", "session_message", "part", "message"):
                        if not con.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (table,)).fetchone():
                            continue
                        con.execute("DELETE FROM %s WHERE session_id=?" % table, (sid,))
                    con.execute("DELETE FROM session WHERE id=?", (sid,))
                    # the counter of a deleted session is dead weight and would
                    # break a session that ever reuses the id
                    con.execute("DELETE FROM event_sequence WHERE aggregate_id=?", (sid,))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        remaining = self.db.scalar(
            "SELECT COUNT(*) FROM session_v2 WHERE id IN (%s)" % ",".join("?" * len(selected)),
            selected) or 0
        result = OperationResult(
            name, not api_failures and remaining == 0, started,
            datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"requested": len(selected), "removed": len(removed),
             "api_failures": api_failures, "remaining": remaining},
            "deleted %d session(s)" % len(removed))
        self.audit(name, {"ids": selected}, not api_failures and remaining == 0, result.message)
        return result

    def delete_projects(self, ids: Iterable[str], filters: dict | None = None,
                        delete_sessions: bool = False, confirm: bool = False) -> OperationResult:
        """Delete project rows.

        `session_v2.project_id` and `session.project_id` are ON DELETE CASCADE to
        `project.id`, so deleting a project destroys every session that belongs
        to it. The operation therefore refuses to touch a project that still
        owns sessions unless `delete_sessions` is set explicitly.
        """
        name = "delete_projects"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("delete_projects requires confirm=true")
        selected = [i for i in ids if i]
        if filters is not None:
            selected = list(dict.fromkeys(selected + self.resolve_project_ids(filters)))
        if not selected:
            raise OperationError("no projects selected")

        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        try:
            existing = {r[0]: r[1] for r in con.execute(
                "SELECT id, worktree FROM project WHERE id IN (%s)"
                % ",".join("?" * len(selected)), selected)}
            missing = [pid for pid in selected if pid not in existing]
            selected = [pid for pid in selected if pid in existing]
            if not selected:
                raise OperationError("no matching projects: %s" % ", ".join(missing[:5]))
            placeholders = ",".join("?" * len(selected))
            owned = [
                {"project_id": r[0], "worktree": r[1], "sessions": r[2]}
                for r in con.execute(
                    "SELECT v.project_id, p.worktree, COUNT(*) FROM session_v2 v "
                    "JOIN project p ON p.id = v.project_id "
                    "WHERE v.project_id IN (%s) GROUP BY v.project_id" % placeholders,
                    selected)]
            # the legacy `session` mirror cascades too, so it counts as ownership
            legacy_owned = [
                {"project_id": r[0], "worktree": r[1], "sessions": r[2]}
                for r in con.execute(
                    "SELECT s.project_id, p.worktree, COUNT(*) FROM session s "
                    "JOIN project p ON p.id = s.project_id "
                    "WHERE s.project_id IN (%s) GROUP BY s.project_id" % placeholders,
                    selected)]
        finally:
            con.close()

        total_sessions = sum(o["sessions"] for o in owned)
        total_legacy = sum(o["sessions"] for o in legacy_owned)
        per_project: dict[str, dict] = {}
        for row in owned:
            per_project[row["project_id"]] = {
                "worktree": row["worktree"], "sessions": row["sessions"], "legacy": 0}
        for row in legacy_owned:
            entry = per_project.setdefault(
                row["project_id"],
                {"worktree": row["worktree"], "sessions": 0, "legacy": 0})
            entry["legacy"] = row["sessions"]
        if per_project and not delete_sessions:
            details = ", ".join(
                "%s (%d sessions%s)" % (
                    e["worktree"], e["sessions"],
                    ", %d legacy" % e["legacy"] if e["legacy"] else "")
                for e in list(per_project.values())[:5])
            raise OperationError(
                "%d selected project(s) still own %d session(s) (%d legacy mirror rows): %s. "
                "Move the sessions to another project first, or repeat the delete "
                "with delete_sessions=true to destroy them as well."
                % (len(per_project), total_sessions, total_legacy, details))

        api_failures = []
        session_ids = [r["id"] for r in self.db.query(
            "SELECT id FROM session_v2 WHERE project_id IN (%s)" % placeholders, selected)]
        for sid in session_ids:
            ok, error = self.remove_session(sid)
            if not ok:
                api_failures.append({"id": sid, "error": error})

        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                # the legacy mirror has no API: remove it directly (children cascade)
                legacy_ids = [r[0] for r in con.execute(
                    "SELECT id FROM session WHERE project_id IN (%s)" % placeholders,
                    selected)]
                child_tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('message', 'todo', 'session_share')")]
                for sid in legacy_ids:
                    for table in child_tables:
                        con.execute("DELETE FROM %s WHERE session_id=?" % table, (sid,))
                    con.execute("DELETE FROM session WHERE id=?", (sid,))
                # the API removes the session and its children; clean up anything
                # it left behind so no orphan rows survive the project delete
                session_children = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ("
                    "'session_message', 'session_part', 'instruction_entry', "
                    "'instruction_state', 'session_inbox', 'blob_ref', 'schedule')")]
                for sid in session_ids:
                    for table in session_children:
                        con.execute("DELETE FROM %s WHERE session_id=?" % table, (sid,))
                    con.execute("DELETE FROM event_sequence WHERE aggregate_id=?", (sid,))
                for table in ("permission", "project_directory", "worktree"):
                    if not con.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone():
                        continue
                    con.execute("DELETE FROM %s WHERE project_id IN (%s)" % (table, placeholders),
                                selected)
                con.execute("DELETE FROM project WHERE id IN (%s)" % placeholders, selected)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

        remaining = self.db.scalar(
            "SELECT COUNT(*) FROM project WHERE id IN (%s)" % placeholders, selected) or 0
        result = OperationResult(
            name, not api_failures and remaining == 0, started,
            datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"projects": [{"id": pid, "worktree": existing.get(pid)} for pid in selected],
             "sessions_destroyed": total_sessions,
             "legacy_sessions_destroyed": total_legacy,
             "api_failures": api_failures, "remaining": remaining},
            "deleted %d project(s), %d session(s) destroyed"
            % (len(selected), total_sessions + total_legacy))
        self.audit(name, {"ids": selected, "delete_sessions": delete_sessions},
                   not api_failures and remaining == 0, result.message)
        return result

    def export_markdown(self, ids: Iterable[str] | None = None, filters: dict | None = None,
                        all_sessions: bool = False, confirm: bool = False) -> OperationResult:
        """Write one Markdown file per session into the dashboard export folder."""
        name = "export_markdown"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("export_markdown requires confirm=true")
        wanted = set(ids or [])
        if filters is not None:
            wanted |= set(self.resolve_session_ids(filters))
        if not wanted and not all_sessions:
            raise OperationError("export_markdown requires ids, filters or all=true")
        out_dir = self.settings.export_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = self.db.query(
            "SELECT id, title, directory, time_created, project_id FROM session_v2 "
            "ORDER BY time_created")
        selected = [r for r in rows if all_sessions or r["id"] in wanted]
        manifest = []
        for row in selected:
            path = out_dir / ("%s.md" % row["id"])
            with self.db.read() as con:
                messages = con.execute(
                    "SELECT seq, type, time_created, data FROM session_message "
                    "WHERE session_id=? ORDER BY seq", (row["id"],)).fetchall()
            lines = ["# %s" % (row["title"] or row["id"]), "",
                     "| Field | Value |", "|---|---|",
                     "| Session ID | `%s` |" % row["id"],
                     "| Directory | %s |" % (row["directory"] or ""),
                     "| Project | `%s` |" % (row["project_id"] or ""),
                     "| Created | %s |" % _iso(row["time_created"]),
                     "| Messages | %d |" % len(messages), ""]
            for msg in messages:
                data = _safe_json(msg["data"])
                role = msg["type"]
                text = ""
                if isinstance(data, dict):
                    text = data.get("text") or ""
                    if not text and isinstance(data.get("content"), list):
                        text = "\n".join(c.get("text", "") for c in data["content"]
                                         if isinstance(c, dict) and c.get("type") == "text")
                lines.append("### %s · seq %s · %s" % (role, msg["seq"], _iso(msg["time_created"])))
                lines.append("")
                lines.append(text or ("```json\n%s\n```" % json.dumps(data, indent=2,
                                                                       ensure_ascii=False)[:4000]))
                lines.append("")
            path.write_text("\n".join(lines), encoding="utf-8")
            manifest.append({"id": row["id"], "file": path.name, "title": row["title"],
                             "messages": len(messages)})
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"directory": str(out_dir), "sessions": len(selected)},
            "exported %d sessions" % len(selected))
        self.audit(name, {"count": len(selected)}, True, result.message)
        return result

    def export_bundle(self, session_id: str, confirm: bool = False) -> OperationResult:
        """Save one session as a self-contained JSON bundle that can be restored."""
        name = "export_bundle"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("export_bundle requires confirm=true")
        with self.db.read() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM session_v2 WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise OperationError("session not found: %s" % session_id)
            session = dict(row)
            messages = [dict(r) for r in con.execute(
                "SELECT * FROM session_message WHERE session_id=? ORDER BY seq", (session_id,))]
        bundle = {
            "format": "opencode-session-bundle/1",
            "exported": datetime.now().isoformat(timespec="seconds"),
            "source_db": str(self.db.path),
            "session_columns": list(session.keys()),
            "message_columns": list(messages[0].keys()) if messages else [],
            "session": session,
            "messages": messages,
        }
        self.settings.bundle_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.bundle_dir / ("%s.json" % session_id)
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"path": str(path), "bytes": path.stat().st_size, "messages": len(messages)},
            "bundle written")
        self.audit(name, {"session": session_id}, True, result.message)
        return result

    def restore_bundle(self, path: str, confirm: bool = False) -> OperationResult:
        """Restore a session from a bundle, advancing event_sequence so prompts work."""
        name = "restore_bundle"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("restore_bundle requires confirm=true")
        bundle_path = Path(path).expanduser()
        if not bundle_path.is_file():
            raise OperationError("bundle not found: %s" % bundle_path)
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        if not str(bundle.get("format", "")).startswith("opencode-session-bundle/"):
            raise OperationError("not an opencode session bundle")
        session = bundle["session"]
        messages = bundle["messages"]
        s_cols = bundle["session_columns"]
        m_cols = bundle["message_columns"]
        sid = session["id"]
        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            live_cols = [r[1] for r in con.execute("PRAGMA table_info('session_v2')")]
            live_mcols = [r[1] for r in con.execute("PRAGMA table_info('session_message')")]
            if live_cols != s_cols or live_mcols != m_cols:
                raise OperationError("schema mismatch between bundle and live database")
            exists = con.execute("SELECT COUNT(*) FROM session_v2 WHERE id=?", (sid,)).fetchone()[0]
            if exists:
                raise OperationError("session already exists: %s" % sid)
            max_seq = max([m.get("seq") or 0 for m in messages] or [0])
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "INSERT INTO session_v2 (%s) VALUES (%s)" % (
                        ",".join('"%s"' % c for c in s_cols), ",".join("?" * len(s_cols))),
                    [session.get(c) for c in s_cols])
                for message in messages:
                    con.execute(
                        "INSERT INTO session_message (%s) VALUES (%s)" % (
                            ",".join('"%s"' % c for c in m_cols), ",".join("?" * len(m_cols))),
                        [message.get(c) for c in m_cols])
                con.execute(
                    "INSERT INTO event_sequence (aggregate_id, seq, owner_id) VALUES (?, ?, NULL) "
                    "ON CONFLICT(aggregate_id) DO UPDATE SET seq=excluded.seq", (sid, max_seq))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"session": sid, "messages": len(messages), "event_sequence": max_seq},
            "session restored")
        self.audit(name, {"bundle": str(bundle_path)}, True, result.message)
        return result

    def repair_sequences(self, ids: Iterable[str] | None = None,
                         confirm: bool = False) -> OperationResult:
        """Align event_sequence.seq with MAX(session_message.seq).

        A session restored by direct SQL keeps its messages but loses the
        counter, so the next prompt collides with UNIQUE(session_id, seq) and
        the session cannot send prompts.
        """
        name = "repair_sequences"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("repair_sequences requires confirm=true")
        wanted = set(ids or [])
        rows = self.db.query(
            "SELECT v.id, es.seq AS counter, "
            "  (SELECT MAX(m.seq) FROM session_message m WHERE m.session_id = v.id) AS max_seq "
            "FROM session_v2 v LEFT JOIN event_sequence es ON es.aggregate_id = v.id")
        broken = [r for r in rows
                  if r["max_seq"] is not None and (r["counter"] is None or r["counter"] < r["max_seq"])
                  and (not wanted or r["id"] in wanted)]
        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                for row in broken:
                    con.execute(
                        "INSERT INTO event_sequence (aggregate_id, seq, owner_id) VALUES (?, ?, NULL) "
                        "ON CONFLICT(aggregate_id) DO UPDATE SET seq=excluded.seq",
                        (row["id"], row["max_seq"]))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            probes = []
            for row in broken:
                probe_seq = row["max_seq"] + 1
                con.execute("BEGIN IMMEDIATE")
                try:
                    con.execute(
                        "INSERT INTO session_message (id, session_id, type, seq, time_created, "
                        "time_updated, data) VALUES (?,?,?,?,?,?,?)",
                        ("msg_zzprobe0000000000000000000", row["id"], "system", probe_seq,
                         0, 0, "{}"))
                    probes.append({"id": row["id"], "probe_seq": probe_seq, "ok": True})
                except sqlite3.Error as exc:
                    probes.append({"id": row["id"], "probe_seq": probe_seq, "ok": False,
                                   "error": str(exc)[:120]})
                finally:
                    con.execute("ROLLBACK")
        finally:
            con.close()
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"repaired": [{"id": r["id"], "counter": r["counter"], "max_seq": r["max_seq"]}
                          for r in broken], "probes": probes},
            "repaired %d counters" % len(broken))
        self.audit(name, {"count": len(broken)}, True, result.message)
        return result

    def prune_event_sequences(self, confirm: bool = False) -> OperationResult:
        """Delete the counters of sessions that no longer exist.

        The OpenCode server leaves the `event_sequence` row behind when a
        session is removed. The row is dead weight and, if the id is ever
        reused, it would hand the next session a stale seq counter.

        Only session counters are touched: `event_sequence` also holds the
        counters of project aggregates and of the global aggregate, and those
        have no foreign key to a session. Counters that still have `event`
        rows are kept as well - removing them would either break the foreign
        key or destroy the event history.
        """
        name = "prune_event_sequences"
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        if not confirm:
            raise OperationError("prune_event_sequences requires confirm=true")
        con = sqlite3.connect(str(self.db.path), timeout=300, isolation_level=None)
        con.execute("PRAGMA busy_timeout=300000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            has_events = bool(con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event'").fetchone())
            con.execute("BEGIN IMMEDIATE")
            try:
                orphans = [r[0] for r in con.execute(
                    "SELECT es.aggregate_id FROM event_sequence es "
                    "WHERE es.aggregate_id LIKE 'ses_%' AND NOT EXISTS "
                    "(SELECT 1 FROM session_v2 v WHERE v.id = es.aggregate_id)"
                    + (" AND NOT EXISTS (SELECT 1 FROM event e "
                       "WHERE e.aggregate_id = es.aggregate_id)" if has_events else ""))]
                for aggregate_id in orphans:
                    con.execute("DELETE FROM event_sequence WHERE aggregate_id=?", (aggregate_id,))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        result = OperationResult(
            name, True, started, datetime.now().isoformat(timespec="seconds"), time.time() - t0,
            {"removed": len(orphans), "aggregate_ids": orphans[:50]},
            "removed %d orphan session counter(s)" % len(orphans))
        self.audit(name, {"count": len(orphans)}, True, result.message)
        return result


def _iso(ms: Any) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
    except (ValueError, OSError, TypeError, OverflowError):
        return ""


def _safe_json(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": str(raw)[:2000]}
