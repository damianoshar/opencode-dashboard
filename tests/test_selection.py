"""Tests for the bulk selection and delete paths.

The delete paths are exercised against a throwaway copy of the schema, never
the live database: the live DB is only read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dashboard.maintenance import OperationError

pytestmark = pytest.mark.scratch


# --------------------------------------------------------------- projects
def test_delete_project_refuses_when_sessions_exist(maintenance, scratch_db, row_counts):
    before = row_counts(scratch_db)
    with pytest.raises(OperationError) as excinfo:
        maintenance.delete_projects(["p1"], confirm=True)
    assert "still own" in str(excinfo.value)
    assert row_counts(scratch_db) == before


def test_delete_project_refuses_when_only_legacy_sessions_exist(maintenance, scratch_db,
                                                                 row_counts):
    """The legacy `session` mirror also cascades, so it must block the delete."""
    before = row_counts(scratch_db)
    with pytest.raises(OperationError) as excinfo:
        maintenance.delete_projects(["p2"], confirm=True)
    assert "still own" in str(excinfo.value)
    assert "legacy" in str(excinfo.value)
    assert row_counts(scratch_db) == before


def test_delete_project_cascade_flag_removes_legacy_rows(maintenance, scratch_db, row_counts):
    result = maintenance.delete_projects(["p2"], delete_sessions=True, confirm=True)
    assert result.ok
    assert result.details["legacy_sessions_destroyed"] == 1
    counts = row_counts(scratch_db)
    assert counts["project"] == 2
    assert counts["session_v2"] == 1
    assert counts["session"] == 0


def test_delete_empty_project_is_allowed(maintenance, scratch_db, row_counts):
    result = maintenance.delete_projects(["p3"], confirm=True)
    assert result.ok
    counts = row_counts(scratch_db)
    assert counts["project"] == 2
    assert counts["session_v2"] == 2


def test_delete_project_with_cascade_flag(maintenance, scratch_db, row_counts):
    result = maintenance.delete_projects(["p1"], delete_sessions=True, confirm=True)
    assert result.ok
    assert result.details["sessions_destroyed"] == 1
    counts = row_counts(scratch_db)
    assert counts["project"] == 2
    assert counts["session_v2"] == 1
    assert counts["session_message"] == 0
    assert counts["permission"] == 0
    assert counts["project_directory"] == 0
    assert counts["worktree"] == 0


def test_delete_project_requires_confirmation(maintenance, scratch_db):
    with pytest.raises(OperationError):
        maintenance.delete_projects(["p1"], confirm=False)


# --------------------------------------------------------------- sessions
def test_resolve_session_ids_by_filter(maintenance):
    ids = maintenance.resolve_session_ids({"project_id": "p1"})
    assert ids == ["s1"]
    assert maintenance.resolve_session_ids({"project_id": "nope"}) == []
    assert maintenance.resolve_session_ids(None) == []


def test_delete_sessions_resolves_filters(maintenance):
    ids = maintenance.resolve_session_ids({"search": "one"})
    assert ids == ["s1"]


def test_delete_sessions_removes_session_and_messages(maintenance, scratch_db):
    result = maintenance.delete_sessions(["s1"], confirm=True)
    assert result.ok
    assert result.details["remaining"] == 0
    con = sqlite3.connect(scratch_db)
    assert con.execute("SELECT COUNT(*) FROM session_v2 WHERE id='s1'").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM session_message WHERE session_id='s1'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM session_v2 WHERE id='s2'").fetchone()[0] == 1
    con.close()


def test_delete_sessions_keeps_sessions_when_the_api_fails(maintenance, scratch_db):
    """A failed API delete must not wipe the rows of a session that is still alive."""
    def boom(session_id):
        raise OperationError("server unreachable")

    maintenance.api_delete_session = boom
    result = maintenance.delete_sessions(["s1"], confirm=True)
    assert not result.ok
    assert result.details["api_failures"]
    con = sqlite3.connect(scratch_db)
    assert con.execute("SELECT COUNT(*) FROM session_v2 WHERE id='s1'").fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM session_message WHERE session_id='s1'").fetchone()[0] == 1
    con.close()


def test_delete_sessions_requires_confirmation(maintenance):
    with pytest.raises(OperationError):
        maintenance.delete_sessions(["s1"], confirm=False)


def test_delete_sessions_removes_the_event_counter(maintenance, scratch_db):
    maintenance.delete_sessions(["s1"], confirm=True)
    con = sqlite3.connect(scratch_db)
    assert con.execute(
        "SELECT COUNT(*) FROM event_sequence WHERE aggregate_id='s1'").fetchone()[0] == 0
    con.close()


# ------------------------------------------------------------- counters
def test_prune_event_sequences_removes_only_orphan_counters(maintenance, scratch_db):
    con = sqlite3.connect(scratch_db)
    con.execute("INSERT INTO event_sequence VALUES ('ses_gone', 7, NULL)")
    con.execute("INSERT INTO event_sequence VALUES ('global', 0, NULL)")
    con.commit()
    con.close()
    result = maintenance.prune_event_sequences(confirm=True)
    assert result.ok
    assert result.details["removed"] == 1
    con = sqlite3.connect(scratch_db)
    assert con.execute(
        "SELECT COUNT(*) FROM event_sequence WHERE aggregate_id='ses_gone'").fetchone()[0] == 0
    # the counter of a live session and the global counter must survive
    assert con.execute(
        "SELECT COUNT(*) FROM event_sequence WHERE aggregate_id='s1'").fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM event_sequence WHERE aggregate_id='global'").fetchone()[0] == 1
    con.close()


def test_prune_event_sequences_keeps_counters_with_events(maintenance, scratch_db):
    """A counter that still has event rows must survive the prune."""
    con = sqlite3.connect(scratch_db)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO event_sequence VALUES ('ses_with_event', 3, NULL)")
    con.execute("INSERT INTO event VALUES ('e1', 'ses_with_event', 1, 'x', '{}', 0)")
    con.commit()
    con.close()
    result = maintenance.prune_event_sequences(confirm=True)
    assert result.details["removed"] == 0
    con = sqlite3.connect(scratch_db)
    assert con.execute(
        "SELECT COUNT(*) FROM event_sequence WHERE aggregate_id='ses_with_event'").fetchone()[0] == 1
    con.close()


def test_prune_event_sequences_requires_confirmation(maintenance):
    with pytest.raises(OperationError):
        maintenance.prune_event_sequences(confirm=False)


# ------------------------------------------------------------------ moves
def test_move_sessions_requires_a_selection(maintenance, tmp_path):
    """Without ids, filters or all=true nothing may be moved."""
    with pytest.raises(OperationError):
        maintenance.move_sessions(target=str(tmp_path), confirm=True)


def test_move_sessions_moves_only_the_selected_ids(maintenance, tmp_path):
    moved = []
    maintenance.api_post = lambda route, body, timeout=300: moved.append(route)
    result = maintenance.move_sessions(target=str(tmp_path), ids=["s1"], confirm=True)
    assert result.ok
    assert result.details["moved"] == 1
    assert moved == ["/api/session/s1/move"]


# ---------------------------------------------------------------- exports
def test_export_markdown_requires_a_selection(maintenance):
    with pytest.raises(OperationError):
        maintenance.export_markdown(confirm=True)

def test_export_markdown_writes_one_file_per_session(maintenance, scratch_db):
    result = maintenance.export_markdown(ids=["s1"], confirm=True)
    assert result.ok
    assert result.details["sessions"] == 1
    out_dir = Path(result.details["directory"])
    assert (out_dir / "s1.md").is_file()
    assert (out_dir / "manifest.json").is_file()


# --------------------------------------------------------------- the API
@pytest.mark.live
def test_api_rejects_unknown_operation(client):
    response = client.post("/api/maintenance/not-an-op", json={"confirm": True})
    assert response.status_code == 404


@pytest.mark.live
def test_api_delete_projects_requires_confirm(client):
    response = client.post("/api/maintenance/delete-projects",
                            json={"ids": ["246b2bbfa3b8842959581986daab9238ad1f9875"]})
    assert response.status_code == 400
    assert "confirm" in response.json()["error"].lower()
