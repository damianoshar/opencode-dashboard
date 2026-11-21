"""Shared fixtures for the dashboard test suite.

Two kinds of tests live in this project:

* ``live``   - they read the real OpenCode database through the API. Every
  endpoint they touch is read-only, or is called without ``confirm`` so the
  server refuses it before anything can change.
* ``scratch`` - they build a miniature database in a temp directory and drive
  the maintenance code directly. Nothing here can touch the live database.

Run one kind only with ``-m live`` or ``-m scratch`` (see ``run-tests.ps1``).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.app import create_app  # noqa: E402
from dashboard.config import Settings, load_settings  # noqa: E402
from dashboard.db import Database  # noqa: E402
from dashboard.maintenance import Maintenance  # noqa: E402


@pytest.fixture(scope="session")
def live_settings():
    """Settings pointing at the real OpenCode database."""
    return load_settings()


@pytest.fixture(scope="session")
def client(live_settings):
    """A TestClient bound to the live database (read-only endpoints only)."""
    app = create_app(live_settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def scratch_db(tmp_path):
    """A miniature opencode.db with the project/session cascade rules."""
    path = tmp_path / "opencode.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE project (
          id TEXT PRIMARY KEY, worktree TEXT NOT NULL, vcs TEXT, name TEXT,
          time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
          time_initialized INTEGER, sandboxes TEXT NOT NULL, commands TEXT,
          icon_url TEXT, icon_color TEXT, icon_url_override TEXT,
          time_active INTEGER NOT NULL, time_archived INTEGER);
        CREATE TABLE session_v2 (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
          title TEXT, slug TEXT, agent TEXT, model TEXT, parent_id TEXT,
          fork_session_id TEXT, time_created INTEGER NOT NULL,
          time_updated INTEGER NOT NULL, time_busy INTEGER, time_idle INTEGER,
          time_archived INTEGER, time_compacting INTEGER, time_suspended INTEGER,
          FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE);
        CREATE TABLE session (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
          title TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
          FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE);
        CREATE TABLE session_message (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
          seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
          time_updated INTEGER NOT NULL, data TEXT NOT NULL,
          UNIQUE (session_id, seq),
          FOREIGN KEY (session_id) REFERENCES session_v2(id) ON DELETE CASCADE);
        CREATE TABLE event_sequence (aggregate_id TEXT PRIMARY KEY, seq INTEGER NOT NULL,
                                     owner_id TEXT);
        CREATE TABLE event (
          id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, seq INTEGER NOT NULL,
          type TEXT NOT NULL, data TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY (aggregate_id) REFERENCES event_sequence(aggregate_id) ON DELETE CASCADE);
        CREATE TABLE permission (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
          action TEXT NOT NULL, resource TEXT NOT NULL,
          time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
          FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE);
        CREATE TABLE project_directory (project_id TEXT NOT NULL, directory TEXT NOT NULL,
          type TEXT, strategy TEXT, time_created INTEGER NOT NULL,
          PRIMARY KEY (project_id, directory),
          FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE);
        CREATE TABLE worktree (project_id TEXT NOT NULL, worktree TEXT NOT NULL,
          time_created INTEGER NOT NULL, PRIMARY KEY (project_id, worktree),
          FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE);
        CREATE TABLE todo (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          content TEXT NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL,
          position INTEGER NOT NULL, time_created INTEGER NOT NULL,
          time_updated INTEGER NOT NULL);
        """)
    con.execute("INSERT INTO project VALUES ('p1','C:/tmp/one',NULL,NULL,1,1,NULL,'[]',NULL,"
                "NULL,NULL,NULL,1,NULL)")
    con.execute("INSERT INTO project VALUES ('p2','C:/tmp/two',NULL,NULL,1,1,NULL,'[]',NULL,"
                "NULL,NULL,NULL,1,NULL)")
    con.execute("INSERT INTO project VALUES ('p3','C:/tmp/three',NULL,NULL,1,1,NULL,'[]',NULL,"
                "NULL,NULL,NULL,1,NULL)")
    con.execute("INSERT INTO session_v2 VALUES ('s1','p1','C:/tmp/one','one','slug-one','build',"
                "NULL,NULL,NULL,1,1,NULL,NULL,NULL,NULL,NULL)")
    con.execute("INSERT INTO session_v2 VALUES ('s2','p2','C:/tmp/two','two','slug-two','build',"
                "NULL,NULL,NULL,1,1,NULL,NULL,NULL,NULL,NULL)")
    con.execute("INSERT INTO session VALUES ('ls1','p2','C:/tmp/two','legacy',1,1)")
    con.execute("INSERT INTO session_message VALUES ('m1','s1','user',1,1,1,'{}')")
    con.execute("INSERT INTO event_sequence VALUES ('s1', 1, NULL)")
    con.execute("INSERT INTO permission VALUES ('perm1','p1','edit','C:/tmp/one',1,1)")
    con.execute("INSERT INTO project_directory VALUES ('p1','C:/tmp/one','worktree',NULL,1)")
    con.execute("INSERT INTO worktree VALUES ('p1','C:/tmp/one',1)")
    con.commit()
    con.close()
    return path


@pytest.fixture
def maintenance(scratch_db):
    """A Maintenance instance bound to the scratch database.

    The OpenCode CLI calls are replaced by local fakes: a unit test must never
    talk to the running service.
    """
    settings = Settings(db_path=scratch_db, data_dir=scratch_db.parent / "data")
    settings.ensure_dirs()
    mgr = Maintenance(settings, Database(scratch_db))

    def fake_delete(session_id):
        con = sqlite3.connect(scratch_db)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("DELETE FROM session_message WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM session_v2 WHERE id=?", (session_id,))
        con.commit()
        con.close()

    mgr.api_delete_session = fake_delete
    return mgr


@pytest.fixture
def row_counts():
    """Count the rows of the tables the delete paths touch."""
    def counts(path):
        con = sqlite3.connect(path)
        out = {}
        for table in ("project", "session_v2", "session", "session_message", "permission",
                      "project_directory", "worktree"):
            out[table] = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        con.close()
        return out
    return counts
