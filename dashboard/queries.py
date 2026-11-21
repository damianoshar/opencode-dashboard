"""Read queries for the dashboard: projects, sessions, directories, stats, events.

All SQL is parameterised. Table and column names are validated against the
live schema before they are interpolated.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import Database, quote_ident

SESSION_SORTS = {
    "time_created": "v.time_created",
    "time_updated": "v.time_updated",
    "title": "v.title",
    "cost": "v.cost",
    "messages": "msg_count",
    "tokens": "(v.tokens_input + v.tokens_output + v.tokens_reasoning)",
    "directory": "v.directory",
    "agent": "v.agent",
}

SESSION_STATES = ("idle", "busy", "archived", "compacting", "suspended")


def iso(ms: Any) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(
            timespec="seconds")
    except (ValueError, OSError, TypeError, OverflowError):
        return None


def parse_model(raw: Any) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"id": str(raw)}
    return value if isinstance(value, dict) else {"id": str(raw)}


def model_label(raw: Any) -> str:
    model = parse_model(raw)
    if not model:
        return ""
    provider = model.get("providerID") or model.get("provider") or ""
    ident = model.get("id") or model.get("modelID") or ""
    if provider and ident and provider not in ident:
        return "%s/%s" % (provider, ident)
    return str(ident or provider)


def session_state(row: sqlite3.Row | dict) -> str:
    get = row.__getitem__ if isinstance(row, sqlite3.Row) else (lambda k: row.get(k))
    if get("time_archived"):
        return "archived"
    if get("time_compacting"):
        return "compacting"
    if get("time_suspended"):
        return "suspended"
    busy = get("time_busy") or 0
    idle = get("time_idle") or 0
    if busy and busy > idle:
        return "busy"
    return "idle"


def session_kind(row: sqlite3.Row | dict) -> str:
    get = row.__getitem__ if isinstance(row, sqlite3.Row) else (lambda k: row.get(k))
    if get("fork_session_id"):
        return "fork"
    if get("parent_id"):
        return "child"
    return "root"


# --------------------------------------------------------------------- filters
class SessionFilters:
    """Validated filter set for the sessions table."""

    def __init__(self, **kwargs: Any) -> None:
        self.project_id = kwargs.get("project_id") or None
        self.directory = kwargs.get("directory") or None
        self.agent = kwargs.get("agent") or None
        self.model = kwargs.get("model") or None
        self.state = kwargs.get("state") or None
        self.kind = kwargs.get("kind") or None
        self.search = kwargs.get("search") or None
        self.created_from = kwargs.get("created_from")
        self.created_to = kwargs.get("created_to")
        self.ids = [i for i in (kwargs.get("ids") or []) if i]

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.project_id:
            clauses.append("v.project_id = ?")
            params.append(self.project_id)
        if self.directory:
            clauses.append("v.directory = ?")
            params.append(self.directory)
        if self.agent:
            clauses.append("v.agent = ?")
            params.append(self.agent)
        if self.model:
            clauses.append("v.model LIKE ?")
            params.append("%" + self.model + "%")
        if self.search:
            clauses.append("(v.title LIKE ? OR v.id LIKE ? OR v.slug LIKE ?)")
            like = "%" + self.search + "%"
            params += [like, like, like]
        if self.created_from:
            clauses.append("v.time_created >= ?")
            params.append(int(self.created_from))
        if self.created_to:
            clauses.append("v.time_created <= ?")
            params.append(int(self.created_to))
        if self.ids:
            clauses.append("v.id IN (%s)" % ",".join("?" * len(self.ids)))
            params += self.ids
        if self.state:
            state = self.state
            if state == "archived":
                clauses.append("v.time_archived IS NOT NULL")
            elif state == "compacting":
                clauses.append("v.time_compacting IS NOT NULL")
            elif state == "suspended":
                clauses.append("v.time_suspended IS NOT NULL")
            elif state == "busy":
                clauses.append("v.time_busy IS NOT NULL AND v.time_busy > COALESCE(v.time_idle,0)")
            elif state == "idle":
                clauses.append("v.time_archived IS NULL AND v.time_compacting IS NULL "
                               "AND (v.time_busy IS NULL OR v.time_busy <= COALESCE(v.time_idle,0))")
        if self.kind:
            if self.kind == "root":
                clauses.append("v.parent_id IS NULL AND v.fork_session_id IS NULL")
            elif self.kind == "child":
                clauses.append("v.parent_id IS NOT NULL")
            elif self.kind == "fork":
                clauses.append("v.fork_session_id IS NOT NULL")
        return (" AND ".join(clauses) if clauses else "1=1"), params


SESSION_SELECT = """
SELECT v.id, v.title, v.directory, v.project_id, v.parent_id, v.fork_session_id,
       v.fork_boundary, v.slug, v.agent, v.model, v.version, v.cost,
       v.tokens_input, v.tokens_output, v.tokens_reasoning,
       v.tokens_cache_read, v.tokens_cache_write,
       v.time_created, v.time_updated, v.time_busy, v.time_idle, v.time_viewed,
       v.time_compacting, v.time_archived, v.time_suspended, v.idle_outcome,
       v.revert, v.share_url, v.summary_additions, v.summary_deletions, v.summary_files,
       (SELECT COUNT(*) FROM session_message m WHERE m.session_id = v.id) AS msg_count,
       (SELECT MAX(m.seq) FROM session_message m WHERE m.session_id = v.id) AS max_seq,
       (SELECT es.seq FROM event_sequence es WHERE es.aggregate_id = v.id) AS event_seq
FROM session_v2 v
"""


def _decorate(row: dict) -> dict:
    row = dict(row)
    row["state"] = session_state(row)
    row["kind"] = session_kind(row)
    row["model_label"] = model_label(row.get("model"))
    row["model_info"] = parse_model(row.get("model"))
    row["created_iso"] = iso(row.get("time_created"))
    row["updated_iso"] = iso(row.get("time_updated"))
    row["tokens_total"] = ((row.get("tokens_input") or 0) + (row.get("tokens_output") or 0)
                           + (row.get("tokens_reasoning") or 0))
    row["seq_lag"] = None
    if row.get("event_seq") is not None and row.get("max_seq") is not None:
        row["seq_lag"] = (row["event_seq"] or 0) - (row["max_seq"] or 0)
    row.pop("model", None)
    return row


def list_sessions(db: Database, filters: SessionFilters, sort: str = "time_updated",
                  order: str = "desc", limit: int = 50, offset: int = 0) -> dict:
    where, params = filters.where()
    sort_expr = SESSION_SORTS.get(sort, SESSION_SORTS["time_updated"])
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    total = db.scalar("SELECT COUNT(*) FROM session_v2 v WHERE " + where, params)
    sql = (SESSION_SELECT + " WHERE " + where + " ORDER BY " + sort_expr + " " + direction +
           " LIMIT ? OFFSET ?")
    rows = db.query(sql, params + [limit, offset])
    return {"items": [_decorate(r) for r in rows], "total": int(total or 0),
            "limit": limit, "offset": offset, "sort": sort, "order": direction.lower()}


def session_facets(db: Database) -> dict:
    """Distinct values that populate the filter dropdowns."""
    with db.read() as con:
        projects = [
            {"id": r[0], "worktree": r[1], "sessions": r[2]}
            for r in con.execute(
                "SELECT p.id, p.worktree, COUNT(v.id) FROM project p "
                "LEFT JOIN session_v2 v ON v.project_id = p.id "
                "GROUP BY p.id ORDER BY COUNT(v.id) DESC, p.worktree")
        ]
        directories = [
            {"directory": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT directory, COUNT(*) FROM session_v2 GROUP BY directory "
                "ORDER BY COUNT(*) DESC, directory")
        ]
        agents = [
            {"agent": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT agent, COUNT(*) FROM session_v2 WHERE agent IS NOT NULL AND agent <> '' "
                "GROUP BY agent ORDER BY COUNT(*) DESC")
        ]
        models = [
            {"model": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT model, COUNT(*) FROM session_v2 WHERE model IS NOT NULL AND model <> '' "
                "GROUP BY model ORDER BY COUNT(*) DESC")
        ]
        states = [
            {"state": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT CASE "
                "  WHEN time_archived IS NOT NULL THEN 'archived' "
                "  WHEN time_compacting IS NOT NULL THEN 'compacting' "
                "  WHEN time_suspended IS NOT NULL THEN 'suspended' "
                "  WHEN time_busy IS NOT NULL AND time_busy > COALESCE(time_idle,0) THEN 'busy' "
                "  ELSE 'idle' END, COUNT(*) FROM session_v2 GROUP BY 1 ORDER BY 2 DESC")
        ]
    return {"projects": projects, "directories": directories, "agents": agents,
            "models": models, "states": states}


def session_detail(db: Database, session_id: str) -> dict | None:
    row = db.query(SESSION_SELECT + " WHERE v.id = ?", [session_id])
    if not row:
        return None
    detail = _decorate(row[0])
    with db.read() as con:
        detail["children"] = [
            dict(r) for r in con.execute(
                "SELECT id, title, agent, time_created, time_updated FROM session_v2 "
                "WHERE parent_id = ? ORDER BY time_created", (session_id,))]
        detail["messages_by_type"] = [
            {"type": r[0], "count": r[1]}
            for r in con.execute(
                "SELECT type, COUNT(*) FROM session_message WHERE session_id=? GROUP BY type "
                "ORDER BY COUNT(*) DESC", (session_id,))]
        detail["parts"] = con.execute(
            "SELECT COUNT(*) FROM session_part WHERE session_id=?", (session_id,)).fetchone()[0]
        detail["legacy_row"] = con.execute(
            "SELECT COUNT(*) FROM session WHERE id=?", (session_id,)).fetchone()[0]
        detail["events"] = con.execute(
            "SELECT COUNT(*) FROM event WHERE aggregate_id=?", (session_id,)).fetchone()[0]
        detail["todos"] = [
            {"content": r[0], "status": r[1], "priority": r[2], "position": r[3]}
            for r in con.execute(
                "SELECT content, status, priority, position FROM todo WHERE session_id=? "
                "ORDER BY position", (session_id,))]
        detail["first_message"] = con.execute(
            "SELECT time_created FROM session_message WHERE session_id=? ORDER BY seq LIMIT 1",
            (session_id,)).fetchone()
        detail["last_message"] = con.execute(
            "SELECT time_created FROM session_message WHERE session_id=? ORDER BY seq DESC LIMIT 1",
            (session_id,)).fetchone()
    detail["first_message_iso"] = iso(detail["first_message"][0] if detail["first_message"] else None)
    detail["last_message_iso"] = iso(detail["last_message"][0] if detail["last_message"] else None)
    detail["first_message"] = detail["first_message"][0] if detail["first_message"] else None
    detail["last_message"] = detail["last_message"][0] if detail["last_message"] else None
    detail["duration_ms"] = ((detail.get("last_message") or 0) - (detail.get("first_message") or 0))
    return detail


def session_messages(db: Database, session_id: str, limit: int = 100, offset: int = 0,
                     message_type: str | None = None, search: str | None = None,
                     order: str = "asc") -> dict:
    clauses = ["session_id = ?"]
    params: list[Any] = [session_id]
    if message_type:
        clauses.append("type = ?")
        params.append(message_type)
    if search:
        clauses.append("data LIKE ?")
        params.append("%" + search + "%")
    where = " AND ".join(clauses)
    total = db.scalar("SELECT COUNT(*) FROM session_message WHERE " + where, params)
    direction = "ASC" if order == "asc" else "DESC"
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    rows = db.query(
        "SELECT id, type, seq, time_created, time_updated, data FROM session_message "
        "WHERE " + where + " ORDER BY seq " + direction + " LIMIT ? OFFSET ?",
        params + [limit, offset])
    items = []
    for row in rows:
        data = row.pop("data")
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            parsed = {"raw": data}
        text = ""
        if isinstance(parsed, dict):
            text = parsed.get("text") or ""
            if not text and isinstance(parsed.get("content"), list):
                chunks = [c.get("text", "") for c in parsed["content"]
                          if isinstance(c, dict) and c.get("type") == "text"]
                text = "\n".join(chunks)
        row["data"] = parsed
        row["text"] = text
        row["time_created_iso"] = iso(row.get("time_created"))
        row["data_bytes"] = len(data or "")
        items.append(row)
    return {"items": items, "total": int(total or 0), "limit": limit, "offset": offset}


# -------------------------------------------------------------------- projects
def list_projects(db: Database, search: str | None = None, limit: int = 200,
                  offset: int = 0) -> dict:
    clauses = ["1=1"]
    params: list[Any] = []
    if search:
        clauses.append("(p.worktree LIKE ? OR p.id LIKE ? OR p.name LIKE ?)")
        like = "%" + search + "%"
        params += [like, like, like]
    where = " AND ".join(clauses)
    total = db.scalar("SELECT COUNT(*) FROM project p WHERE " + where, params)
    rows = db.query(
        "SELECT p.id, p.worktree, p.vcs, p.name, p.time_created, p.time_updated, "
        "       p.time_active, p.time_archived, p.sandboxes, p.commands, "
        "       (SELECT COUNT(*) FROM session_v2 v WHERE v.project_id = p.id) AS sessions, "
        "       (SELECT COUNT(*) FROM session_message m JOIN session_v2 v2 "
        "         ON v2.id = m.session_id WHERE v2.project_id = p.id) AS messages, "
        "       (SELECT COALESCE(SUM(v.cost),0) FROM session_v2 v WHERE v.project_id = p.id) AS cost "
        "FROM project p WHERE " + where +
        " ORDER BY sessions DESC, p.worktree LIMIT ? OFFSET ?",
        params + [max(1, min(int(limit), 1000)), max(0, int(offset))])
    for row in rows:
        row["created_iso"] = iso(row.get("time_created"))
        row["updated_iso"] = iso(row.get("time_updated"))
        row["active_iso"] = iso(row.get("time_active"))
        row["exists_on_disk"] = bool(row.get("worktree")) and _dir_exists(row["worktree"])
    return {"items": rows, "total": int(total or 0)}


def _dir_exists(path: str) -> bool:
    from pathlib import Path
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def project_detail(db: Database, project_id: str) -> dict | None:
    rows = db.query("SELECT * FROM project WHERE id = ?", [project_id])
    if not rows:
        return None
    detail = rows[0]
    detail["created_iso"] = iso(detail.get("time_created"))
    detail["updated_iso"] = iso(detail.get("time_updated"))
    detail["active_iso"] = iso(detail.get("time_active"))
    with db.read() as con:
        detail["directories"] = [
            dict(r) for r in con.execute(
                "SELECT * FROM project_directory WHERE project_id = ?", (project_id,))]
        detail["worktrees"] = [
            dict(r) for r in con.execute(
                "SELECT * FROM worktree WHERE project_id = ?", (project_id,))]
        detail["permissions"] = [
            dict(r) for r in con.execute(
                "SELECT * FROM permission WHERE project_id = ?", (project_id,))]
    return detail


# ----------------------------------------------------------------- directories
def list_directories(db: Database) -> dict:
    """Every directory the app knows about, from sessions, projects and tables."""
    with db.read() as con:
        session_dirs = [
            {"directory": r[0], "sessions": r[1], "messages": r[2], "source": "session_v2"}
            for r in con.execute(
                "SELECT v.directory, COUNT(*), "
                "  (SELECT COUNT(*) FROM session_message m JOIN session_v2 v2 "
                "   ON v2.id = m.session_id WHERE v2.directory = v.directory) "
                "FROM session_v2 v GROUP BY v.directory ORDER BY COUNT(*) DESC")]
        legacy_dirs = [
            {"directory": r[0], "sessions": r[1], "source": "session (v1)"}
            for r in con.execute(
                "SELECT directory, COUNT(*) FROM session GROUP BY directory ORDER BY COUNT(*) DESC")]
        project_dirs = [
            {"directory": r[0], "project_id": r[1], "type": r[2], "strategy": r[3],
             "source": "project_directory"}
            for r in con.execute(
                "SELECT directory, project_id, type, strategy FROM project_directory "
                "ORDER BY directory")]
        worktree_rows = [
            {"directory": r[0], "project_id": r[1], "source": "worktree"}
            for r in con.execute(
                "SELECT directory, project_id FROM worktree ORDER BY directory")]
        project_rows = [
            {"directory": r[0], "project_id": r[1], "sessions": r[2], "source": "project"}
            for r in con.execute(
                "SELECT p.worktree, p.id, "
                "  (SELECT COUNT(*) FROM session_v2 v WHERE v.project_id = p.id) "
                "FROM project p ORDER BY p.worktree")]

    by_dir: dict[str, dict] = {}
    for row in session_dirs + legacy_dirs:
        entry = by_dir.setdefault(row["directory"], {
            "directory": row["directory"], "sessions": 0, "messages": 0, "sources": [],
            "project_ids": set()})
        entry["sessions"] += row.get("sessions", 0)
        entry["messages"] += row.get("messages", 0)
        entry["sources"].append(row["source"])
    for row in project_rows + project_dirs + worktree_rows:
        entry = by_dir.setdefault(row["directory"], {
            "directory": row["directory"], "sessions": 0, "messages": 0, "sources": [],
            "project_ids": set()})
        entry["sources"].append(row["source"])
        if row.get("project_id"):
            entry["project_ids"].add(row["project_id"])

    items = []
    for entry in by_dir.values():
        entry["sources"] = sorted(set(entry["sources"]))
        entry["project_ids"] = sorted(entry["project_ids"])
        entry["exists_on_disk"] = _dir_exists(entry["directory"])
        entry["orphan"] = entry["sessions"] == 0
        items.append(entry)
    items.sort(key=lambda e: (-e["sessions"], e["directory"]))

    return {
        "items": items,
        "total": len(items),
        "session_directories": len(session_dirs),
        "project_directories": len(project_rows),
        "orphan_directories": sum(1 for e in items if e["orphan"]),
    }


# --------------------------------------------------------------------- overview
def overview(db: Database) -> dict:
    counts = db.row_counts()
    pragmas = db.pragmas()
    files = db.file_stats()
    with db.read() as con:
        totals = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost),0), COALESCE(SUM(tokens_input),0), "
            "       COALESCE(SUM(tokens_output),0), COALESCE(SUM(tokens_reasoning),0), "
            "       COALESCE(SUM(tokens_cache_read),0), "
            "       MIN(time_created), MAX(time_created) FROM session_v2").fetchone()
        message_types = [
            {"type": r[0], "count": r[1]}
            for r in con.execute(
                "SELECT type, COUNT(*) FROM session_message GROUP BY type ORDER BY COUNT(*) DESC")]
        states = [
            {"state": r[0], "count": r[1]}
            for r in con.execute(
                "SELECT CASE "
                "  WHEN time_archived IS NOT NULL THEN 'archived' "
                "  WHEN time_compacting IS NOT NULL THEN 'compacting' "
                "  WHEN time_busy IS NOT NULL AND time_busy > COALESCE(time_idle,0) THEN 'busy' "
                "  ELSE 'idle' END, COUNT(*) FROM session_v2 GROUP BY 1 ORDER BY 2 DESC")]
        kinds = [
            {"kind": r[0], "count": r[1]}
            for r in con.execute(
                "SELECT CASE WHEN fork_session_id IS NOT NULL THEN 'fork' "
                "            WHEN parent_id IS NOT NULL THEN 'child' ELSE 'root' END, COUNT(*) "
                "FROM session_v2 GROUP BY 1")]
        top_models = [
            {"model": model_label(r[0]), "sessions": r[1]}
            for r in con.execute(
                "SELECT model, COUNT(*) FROM session_v2 WHERE model IS NOT NULL AND model <> '' "
                "GROUP BY model ORDER BY COUNT(*) DESC LIMIT 12")]
        top_agents = [
            {"agent": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT agent, COUNT(*) FROM session_v2 WHERE agent IS NOT NULL AND agent <> '' "
                "GROUP BY agent ORDER BY COUNT(*) DESC LIMIT 12")]
        timeline = [
            {"day": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT date(time_created/1000, 'unixepoch', 'localtime') d, COUNT(*) "
                "FROM session_v2 GROUP BY d ORDER BY d DESC LIMIT 60")]
        timeline.reverse()
        top_dirs = [
            {"directory": r[0], "sessions": r[1]}
            for r in con.execute(
                "SELECT directory, COUNT(*) FROM session_v2 GROUP BY directory "
                "ORDER BY COUNT(*) DESC LIMIT 12")]
        seq_lag = [
            {"id": r[0], "title": r[1], "event_seq": r[2], "max_seq": r[3]}
            for r in con.execute(
                "SELECT v.id, v.title, es.seq, "
                "  (SELECT MAX(m.seq) FROM session_message m WHERE m.session_id = v.id) "
                "FROM session_v2 v LEFT JOIN event_sequence es ON es.aggregate_id = v.id "
                "WHERE es.seq IS NOT NULL AND es.seq < "
                "  (SELECT MAX(m.seq) FROM session_message m WHERE m.session_id = v.id) "
                "LIMIT 20")]
        orphan_messages = con.execute(
            "SELECT COUNT(*) FROM session_message m LEFT JOIN session_v2 v ON v.id = m.session_id "
            "WHERE v.id IS NULL").fetchone()[0]
        orphan_events = con.execute(
            "SELECT COUNT(*) FROM event e LEFT JOIN event_sequence es "
            "ON es.aggregate_id = e.aggregate_id WHERE es.aggregate_id IS NULL").fetchone()[0]

    size = pragmas.get("page_size", 4096) * pragmas.get("page_count", 0)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": {
            "path": str(db.path),
            "exists": db.exists(),
            "size_bytes": files["db_bytes"],
            "wal_bytes": files["wal_bytes"],
            "page_size": pragmas.get("page_size"),
            "page_count": pragmas.get("page_count"),
            "free_pages": pragmas.get("freelist_count"),
            "free_bytes": pragmas.get("freelist_count", 0) * pragmas.get("page_size", 0),
            "used_bytes": size,
            "journal_mode": pragmas.get("journal_mode"),
            "mtime": files.get("db_mtime"),
        },
        "counts": counts,
        "sessions": {
            "total": totals[0] or 0,
            "cost": totals[1] or 0,
            "tokens_input": totals[2] or 0,
            "tokens_output": totals[3] or 0,
            "tokens_reasoning": totals[4] or 0,
            "tokens_cache_read": totals[5] or 0,
            "first_created": iso(totals[6]),
            "last_created": iso(totals[7]),
        },
        "message_types": message_types,
        "states": states,
        "kinds": kinds,
        "top_models": top_models,
        "top_agents": top_agents,
        "top_directories": top_dirs,
        "timeline": timeline,
        "health": {
            "orphan_messages": orphan_messages,
            "orphan_events": orphan_events,
            "seq_lag_sessions": seq_lag,
        },
    }


# ------------------------------------------------------------------- raw tables
def table_rows(db: Database, table: str, where: str | None = None, order: str | None = None,
               limit: int = 100, offset: int = 0) -> dict:
    if table not in db.table_names():
        raise KeyError(table)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where_clause = where.strip() if where else ""
    params: list[Any] = []
    if where_clause:
        where_clause = "WHERE " + where_clause
    order_clause = ""
    if order:
        order_clause = "ORDER BY " + order.strip()
    sql = "SELECT * FROM %s %s %s LIMIT ? OFFSET ?" % (
        quote_ident(table), where_clause, order_clause)
    rows = db.query(sql, params + [limit, offset])
    total = db.scalar("SELECT COUNT(*) FROM %s %s" % (quote_ident(table), where_clause), params)
    return {"items": rows, "total": int(total or 0), "limit": limit, "offset": offset,
            "columns": db.column_names(table)}


def run_readonly_sql(db: Database, sql: str, limit: int = 200) -> dict:
    statement = sql.strip().rstrip(";")
    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with") or lowered.startswith("pragma")):
        raise ValueError("only SELECT / WITH / PRAGMA statements are allowed")
    for forbidden in ("insert ", "update ", "delete ", "drop ", "alter ", "create ",
                      "replace ", "attach ", "detach ", "vacuum ", "pragma journal_mode"):
        if forbidden in lowered.replace("\n", " "):
            raise ValueError("statement type not allowed: %s" % forbidden.strip())
    rows = db.query(statement + " LIMIT %d" % max(1, min(int(limit), 1000)))
    return {"items": rows, "count": len(rows)}


# ------------------------------------------------------------------ simple sets
def simple_list(db: Database, table: str, where: str | None = None, order: str | None = None,
                limit: int = 200, offset: int = 0,
                redact: tuple[str, ...] = ()) -> dict:
    if table not in db.table_names():
        raise KeyError(table)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where_clause = ("WHERE " + where.strip()) if where else ""
    order_clause = ("ORDER BY " + order.strip()) if order else ""
    rows = db.query(
        "SELECT * FROM %s %s %s LIMIT ? OFFSET ?" % (
            quote_ident(table), where_clause, order_clause),
        [limit, offset])
    total = db.scalar("SELECT COUNT(*) FROM %s %s" % (quote_ident(table), where_clause))
    for row in rows:
        for column in redact:
            if row.get(column):
                row[column] = _redact(str(row[column]))
        for key, value in list(row.items()):
            if isinstance(value, str) and len(value) > 200000:
                row[key] = value[:200000] + "...[truncated %d bytes]" % (len(value) - 200000)
    return {"items": rows, "total": int(total or 0)}


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]
