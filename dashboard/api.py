"""HTTP API for the OpenCode dashboard.

Read endpoints are safe by default. Mutating endpoints live under
/api/maintenance and all require ``{"confirm": true}``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import queries
from .config import Settings
from .db import Database
from .maintenance import Maintenance, OperationError
from .queries import SessionFilters

router = APIRouter(prefix="/api")

_storage_cache: dict[str, Any] = {}


def _settings() -> Settings:
    from .config import SETTINGS
    return SETTINGS


def _db() -> Database:
    from .config import SETTINGS
    if not hasattr(SETTINGS, "_db_handle"):
        SETTINGS._db_handle = Database(SETTINGS.db_path)  # type: ignore[attr-defined]
    return SETTINGS._db_handle  # type: ignore[attr-defined]


def _maintenance() -> Maintenance:
    return Maintenance(_settings(), _db())


# ------------------------------------------------------------------- meta
@router.get("/health")
def health() -> dict:
    settings = _settings()
    db = _db()
    return {
        "ok": db.exists(),
        "database": str(db.path),
        "database_exists": db.exists(),
        "size_bytes": db.file_stats()["db_bytes"] if db.exists() else 0,
        "opencode_dir": str(settings.opencode_dir),
        "data_dir": str(settings.data_dir),
        "read_only": settings.read_only,
        "time": datetime.now().isoformat(timespec="seconds"),
    }


@router.get("/overview")
def overview() -> dict:
    return queries.overview(_db())


@router.get("/facets")
def facets() -> dict:
    return queries.session_facets(_db())


# --------------------------------------------------------------- projects
@router.get("/projects")
def projects(search: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    return queries.list_projects(_db(), search=search, limit=limit, offset=offset)


@router.get("/projects/{project_id}")
def project(project_id: str) -> dict:
    detail = queries.project_detail(_db(), project_id)
    if not detail:
        raise HTTPException(404, "project not found")
    return detail


# --------------------------------------------------------------- sessions
@router.get("/sessions")
def sessions(
    project_id: str | None = None,
    directory: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    search: str | None = None,
    created_from: int | None = None,
    created_to: int | None = None,
    sort: str = "time_updated",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    filters = SessionFilters(
        project_id=project_id, directory=directory, agent=agent, model=model, state=state,
        kind=kind, search=search, created_from=created_from, created_to=created_to)
    if state and state not in queries.SESSION_STATES:
        raise HTTPException(400, "unknown state: %s" % state)
    return queries.list_sessions(_db(), filters, sort=sort, order=order, limit=limit, offset=offset)


@router.get("/sessions/{session_id}")
def session(session_id: str) -> dict:
    detail = queries.session_detail(_db(), session_id)
    if not detail:
        raise HTTPException(404, "session not found")
    return detail


@router.get("/sessions/{session_id}/messages")
def session_messages(
    session_id: str,
    limit: int = 100,
    offset: int = 0,
    type: str | None = None,
    search: str | None = None,
    order: str = "asc",
) -> dict:
    return queries.session_messages(_db(), session_id, limit=limit, offset=offset,
                                    message_type=type, search=search, order=order)


@router.get("/stats")
def stats(
    project_id: str | None = None,
    directory: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    search: str | None = None,
    created_from: int | None = None,
    created_to: int | None = None,
) -> dict:
    """Aggregates over the currently filtered session set."""
    filters = SessionFilters(
        project_id=project_id, directory=directory, agent=agent, model=model, state=state,
        kind=kind, search=search, created_from=created_from, created_to=created_to)
    where, params = filters.where()
    db = _db()
    with db.read() as con:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost),0), COALESCE(SUM(tokens_input),0), "
            "       COALESCE(SUM(tokens_output),0), COALESCE(SUM(tokens_reasoning),0), "
            "       COALESCE(SUM(tokens_cache_read),0), "
            "       MIN(time_created), MAX(time_created) FROM session_v2 v WHERE " + where,
            params).fetchone()
        by_dir = [{"key": r[0], "count": r[1]}
                  for r in con.execute(
                      "SELECT directory, COUNT(*) FROM session_v2 v WHERE " + where +
                      " GROUP BY directory ORDER BY COUNT(*) DESC LIMIT 20", params)]
        by_agent = [{"key": r[0], "count": r[1]}
                    for r in con.execute(
                        "SELECT agent, COUNT(*) FROM session_v2 v WHERE " + where +
                        " GROUP BY agent ORDER BY COUNT(*) DESC LIMIT 20", params)]
        by_model = [{"key": queries.model_label(r[0]), "count": r[1]}
                    for r in con.execute(
                        "SELECT model, COUNT(*) FROM session_v2 v WHERE " + where +
                        " GROUP BY model ORDER BY COUNT(*) DESC LIMIT 20", params)]
        by_state = [{"key": r[0], "count": r[1]}
                    for r in con.execute(
                        "SELECT CASE WHEN time_archived IS NOT NULL THEN 'archived' "
                        "  WHEN time_compacting IS NOT NULL THEN 'compacting' "
                        "  WHEN time_busy IS NOT NULL AND time_busy > COALESCE(time_idle,0) "
                        "    THEN 'busy' ELSE 'idle' END, COUNT(*) "
                        "FROM session_v2 v WHERE " + where + " GROUP BY 1 ORDER BY 2 DESC", params)]
        timeline = [{"key": r[0], "count": r[1]}
                    for r in con.execute(
                        "SELECT date(time_created/1000, 'unixepoch', 'localtime'), COUNT(*) "
                        "FROM session_v2 v WHERE " + where +
                        " GROUP BY 1 ORDER BY 1 DESC LIMIT 90", params)]
    timeline.reverse()
    return {
        "sessions": row[0] or 0,
        "cost": row[1] or 0,
        "tokens_input": row[2] or 0,
        "tokens_output": row[3] or 0,
        "tokens_reasoning": row[4] or 0,
        "tokens_cache_read": row[5] or 0,
        "first_created": queries.iso(row[6]),
        "last_created": queries.iso(row[7]),
        "by_directory": by_dir,
        "by_agent": by_agent,
        "by_model": by_model,
        "by_state": by_state,
        "timeline": timeline,
    }


# ------------------------------------------------------------ directories
@router.get("/directories")
def directories() -> dict:
    return queries.list_directories(_db())


# ------------------------------------------------------------------ tables
@router.get("/tables")
def tables() -> dict:
    db = _db()
    counts = db.row_counts()
    items = []
    for name in db.table_names():
        items.append({
            "name": name,
            "rows": counts.get(name, -1),
            "columns": db.columns(name),
            "indexes": db.indexes(name),
            "foreign_keys": db.foreign_keys(name),
        })
    return {"items": items, "total": len(items)}


@router.get("/tables/{table}/rows")
def table_rows(table: str, where: str | None = None, order: str | None = None,
               limit: int = 100, offset: int = 0) -> dict:
    try:
        return queries.table_rows(_db(), table, where=where, order=order, limit=limit, offset=offset)
    except KeyError:
        raise HTTPException(404, "unknown table: %s" % table)


@router.post("/query")
def query(payload: dict = Body(...)) -> dict:
    sql = str(payload.get("sql", ""))
    try:
        return queries.run_readonly_sql(_db(), sql, limit=int(payload.get("limit", 200)))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "query failed: %s" % exc)


# ------------------------------------------------------------ simple lists
SIMPLE_TABLES = {
    "events": ("event", "seq DESC", ("data",)),
    "event-sequences": ("event_sequence", "seq DESC", ()),
    "todos": ("todo", "session_id, position", ()),
    "kv": ("kv", "key", ("value",)),
    "credentials": ("credential", "label", ("value",)),
    "accounts": ("account", "time_created DESC", ("access_token", "refresh_token")),
    "control-accounts": ("control_account", "email", ("access_token", "refresh_token")),
    "jobs": ("job", "time_created DESC", ()),
    "schedules": ("schedule", "time_created DESC", ()),
    "worktrees": ("worktree", "worktree", ()),
    "project-directories": ("project_directory", "directory", ()),
    "permissions": ("permission", "time_created DESC", ()),
    "shares": ("session_share", "time_created DESC", ("secret",)),
    "inbox": ("session_inbox", "time_created DESC", ()),
    "blobs": ("blob", "time_created DESC", ("data",)),
    "workspaces": ("workspace", "id", ()),
    "migrations": ("migration", "time_completed DESC", ()),
    "instructions": ("instruction_blob", "hash", ("value",)),
    "instruction-entries": ("instruction_entry", "time_created DESC", ()),
    "instruction-state": ("instruction_state", "session_id", ()),
    "legacy-sessions": ("session", "time_created DESC", ()),
    "legacy-messages": ("message", "time_created DESC", ("data",)),
    "legacy-parts": ("part", "time_created DESC", ("data",)),
    "session-parts": ("session_part", "time_created DESC", ("data",)),
    "drizzle-migrations": ("__drizzle_migrations", "id", ()),
}


@router.get("/tables-simple/{name}")
def simple_table(name: str, limit: int = 200, offset: int = 0, where: str | None = None,
                 order: str | None = None) -> dict:
    if name not in SIMPLE_TABLES:
        raise HTTPException(404, "unknown collection: %s" % name)
    table, default_order, redact = SIMPLE_TABLES[name]
    try:
        return queries.simple_list(_db(), table, where=where, order=order or default_order,
                                   limit=limit, offset=offset, redact=redact)
    except KeyError:
        raise HTTPException(404, "table missing in database: %s" % table)


# ----------------------------------------------------------------- backups
@router.get("/backups")
def backups() -> dict:
    return _maintenance().list_backups()


@router.get("/backups/status")
def backup_status(path: str) -> dict:
    try:
        return _maintenance().backup_status(path)
    except OperationError as exc:
        raise HTTPException(404, str(exc))


@router.get("/audit")
def audit(limit: int = 100) -> dict:
    entries = _maintenance().audit_entries(limit=limit)
    return {"items": entries, "total": len(entries)}


# ------------------------------------------------------------- maintenance
MAINTENANCE_OPS = {
    "backup", "checkpoint", "vacuum", "prune-projects", "move-sessions", "delete-sessions",
    "delete-projects", "export-markdown", "export-bundle", "restore-bundle", "repair-sequences",
    "prune-event-sequences",
}


@router.post("/maintenance/{operation}")
def maintenance(operation: str, payload: dict = Body(default={})) -> dict:
    if operation not in MAINTENANCE_OPS:
        raise HTTPException(404, "unknown operation: %s" % operation)
    if _settings().read_only:
        raise HTTPException(403, "the dashboard is running in read-only mode")
    confirm = bool(payload.get("confirm"))
    mgr = _maintenance()
    try:
        if operation == "backup":
            result = mgr.create_backup(label=str(payload.get("label", "")),
                                       to_opencode_dir=bool(payload.get("to_opencode_dir")),
                                       confirm=confirm)
        elif operation == "checkpoint":
            result = mgr.checkpoint(truncate=bool(payload.get("truncate", True)), confirm=confirm)
        elif operation == "vacuum":
            result = mgr.vacuum(secure=bool(payload.get("secure", True)), confirm=confirm)
        elif operation == "prune-projects":
            result = mgr.prune_projects(keep=str(payload.get("keep", "")), confirm=confirm)
        elif operation == "move-sessions":
            result = mgr.move_sessions(target=str(payload.get("target", "")),
                                       ids=payload.get("ids") or None,
                                       filters=payload.get("filters") or None,
                                       all_sessions=bool(payload.get("all")), confirm=confirm)
        elif operation == "delete-sessions":
            result = mgr.delete_sessions(ids=payload.get("ids") or [],
                                         filters=payload.get("filters") or None, confirm=confirm)
        elif operation == "delete-projects":
            result = mgr.delete_projects(ids=payload.get("ids") or [],
                                         filters=payload.get("filters") or None,
                                         delete_sessions=bool(payload.get("delete_sessions")),
                                         confirm=confirm)
        elif operation == "export-markdown":
            result = mgr.export_markdown(ids=payload.get("ids") or None,
                                         filters=payload.get("filters") or None,
                                         all_sessions=bool(payload.get("all")), confirm=confirm)
        elif operation == "export-bundle":
            result = mgr.export_bundle(session_id=str(payload.get("session_id", "")), confirm=confirm)
        elif operation == "restore-bundle":
            result = mgr.restore_bundle(path=str(payload.get("path", "")), confirm=confirm)
        elif operation == "prune-event-sequences":
            result = mgr.prune_event_sequences(confirm=confirm)
        else:
            result = mgr.repair_sequences(ids=payload.get("ids") or None, confirm=confirm)
    except OperationError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return result.to_dict()


# ----------------------------------------------------------------- exports
def _csv(rows: list[dict], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


@router.get("/export/sessions.csv")
def export_sessions_csv(
    project_id: str | None = None,
    directory: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    search: str | None = None,
    created_from: int | None = None,
    created_to: int | None = None,
    sort: str = "time_updated",
    order: str = "desc",
    limit: int = 1000,
) -> PlainTextResponse:
    filters = SessionFilters(
        project_id=project_id, directory=directory, agent=agent, model=model, state=state,
        kind=kind, search=search, created_from=created_from, created_to=created_to)
    result = queries.list_sessions(_db(), filters, sort=sort, order=order, limit=limit, offset=0)
    columns = ["id", "title", "directory", "project_id", "state", "kind", "agent", "model_label",
               "msg_count", "max_seq", "event_seq", "cost", "tokens_input", "tokens_output",
               "tokens_reasoning", "tokens_cache_read", "created_iso", "updated_iso",
               "parent_id", "fork_session_id"]
    return PlainTextResponse(_csv(result["items"], columns), media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=opencode-sessions.csv"})


@router.get("/export/sessions.json")
def export_sessions_json(
    project_id: str | None = None,
    directory: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    search: str | None = None,
    created_from: int | None = None,
    created_to: int | None = None,
    limit: int = 1000,
) -> JSONResponse:
    filters = SessionFilters(
        project_id=project_id, directory=directory, agent=agent, model=model, state=state,
        kind=kind, search=search, created_from=created_from, created_to=created_to)
    result = queries.list_sessions(_db(), filters, limit=limit, offset=0)
    return JSONResponse(result)


@router.get("/export/projects.csv")
def export_projects_csv(search: str | None = None) -> PlainTextResponse:
    result = queries.list_projects(_db(), search=search, limit=1000)
    columns = ["id", "worktree", "vcs", "name", "sessions", "messages", "cost",
               "created_iso", "updated_iso", "active_iso", "exists_on_disk"]
    return PlainTextResponse(_csv(result["items"], columns), media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=opencode-projects.csv"})


@router.get("/export/manifest.json")
def export_manifest() -> JSONResponse:
    """Everything the dashboard knows, in one file."""
    db = _db()
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "overview": queries.overview(db),
        "projects": queries.list_projects(db, limit=1000),
        "sessions": queries.list_sessions(db, SessionFilters(), limit=1000),
        "directories": queries.list_directories(db),
        "backups": _maintenance().list_backups(),
    }
    return JSONResponse(payload)


# ------------------------------------------------------------ storage & logs
@router.get("/storage")
def storage() -> dict:
    settings = _settings()
    cached = _storage_cache.get("payload")
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    out = []
    for label, path in (
        ("opencode", settings.opencode_dir),
        ("logs", settings.log_dir),
        ("storage", settings.storage_dir),
        ("snapshots", settings.snapshot_dir),
        ("worktrees", settings.worktree_dir),
        ("repos", settings.repos_dir),
        ("tool output", settings.tool_output_dir),
        ("dashboard data", settings.data_dir),
        ("dashboard backups", settings.backup_dir),
        ("dashboard exports", settings.export_dir),
        ("dashboard bundles", settings.bundle_dir),
    ):
        entry: dict[str, Any] = {"label": label, "path": str(path), "exists": path.is_dir()}
        if path.is_dir():
            try:
                files = 0
                total = 0
                for root, dirs, names in os.walk(path):
                    depth = len(Path(root).relative_to(path).parts)
                    if depth >= 2:
                        dirs[:] = []
                    for name in names:
                        files += 1
                        if files > 200_000:
                            break
                        try:
                            total += (Path(root) / name).stat().st_size
                        except OSError:
                            pass
                    if files > 200_000:
                        break
                entry["files"] = files
                entry["bytes"] = total
                children = []
                for child in sorted(path.iterdir())[:200]:
                    try:
                        stat = child.stat()
                    except OSError:
                        continue
                    children.append({"name": child.name, "dir": child.is_dir(),
                                     "bytes": stat.st_size,
                                     "mtime_iso": datetime.fromtimestamp(stat.st_mtime)
                                     .isoformat(timespec="seconds")})
                entry["entries"] = children
            except OSError:
                pass
        out.append(entry)
    payload = {"items": out}
    _storage_cache["payload"] = (time.time(), payload)
    return payload


@router.get("/logs")
def logs(limit: int = 50) -> dict:
    settings = _settings()
    items = []
    if settings.log_dir.is_dir():
        for path in sorted(settings.log_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            stat = path.stat()
            items.append({"name": path.name, "path": str(path), "bytes": stat.st_size,
                          "mtime_iso": datetime.fromtimestamp(stat.st_mtime)
                          .isoformat(timespec="seconds")})
    return {"items": items[:limit], "total": len(items)}


@router.get("/logs/tail")
def logs_tail(path: str, lines: int = 200) -> dict:
    target = Path(path)
    if not target.is_file():
        raise HTTPException(404, "log not found")
    count = max(1, min(int(lines), 5000))
    with target.open("rb") as fh:
        try:
            fh.seek(-min(target.stat().st_size, 4_000_000), os.SEEK_END)
        except OSError:
            fh.seek(0)
        text = fh.read().decode("utf-8", "replace")
    return {"path": str(target), "lines": text.splitlines()[-count:]}
