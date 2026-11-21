"""Smoke tests for the dashboard API.

These run against the real OpenCode database in read-only mode, so they are
safe: no endpoint under test mutates anything.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert "database" in payload
    assert payload["database"].endswith(".db")


def test_overview(client):
    payload = client.get("/api/overview").json()
    assert payload["db"]["exists"] is True
    assert payload["sessions"]["total"] >= 0
    assert "counts" in payload
    assert isinstance(payload["timeline"], list)


def test_facets(client):
    payload = client.get("/api/facets").json()
    for key in ("projects", "directories", "agents", "models", "states"):
        assert key in payload


def test_sessions_pagination(client):
    payload = client.get("/api/sessions?limit=5&offset=0").json()
    assert "items" in payload
    assert len(payload["items"]) <= 5
    assert payload["total"] >= len(payload["items"])
    item = payload["items"][0]
    for key in ("id", "title", "directory", "state", "kind", "msg_count", "model_label"):
        assert key in item


def test_sessions_filters(client):
    payload = client.get("/api/sessions?state=idle&kind=root&limit=10").json()
    for item in payload["items"]:
        assert item["state"] == "idle"
        assert item["kind"] == "root"

    search = client.get("/api/sessions?search=zzzz-no-such-session").json()
    assert search["total"] == 0


def test_session_detail_and_messages(client):
    listing = client.get("/api/sessions?limit=1").json()
    if not listing["items"]:
        pytest.skip("no sessions in the database")
    session_id = listing["items"][0]["id"]
    detail = client.get("/api/sessions/%s" % session_id).json()
    assert detail["id"] == session_id
    messages = client.get("/api/sessions/%s/messages?limit=5" % session_id).json()
    assert "items" in messages
    assert len(messages["items"]) <= 5


def test_projects_and_directories(client):
    projects = client.get("/api/projects?limit=5").json()
    assert "items" in projects
    directories = client.get("/api/directories").json()
    assert "items" in directories
    for entry in directories["items"]:
        assert "directory" in entry
        assert "sources" in entry


def test_stats(client):
    payload = client.get("/api/stats").json()
    assert "sessions" in payload
    assert "by_directory" in payload


def test_tables_and_rows(client):
    tables = client.get("/api/tables").json()
    assert tables["total"] > 10
    rows = client.get("/api/tables/session_v2/rows?limit=3").json()
    assert len(rows["items"]) <= 3


def test_readonly_query(client):
    payload = client.post("/api/query", json={"sql": "SELECT COUNT(*) AS n FROM session_v2"}).json()
    assert payload["items"][0]["n"] >= 0
    bad = client.post("/api/query", json={"sql": "DELETE FROM session_v2"})
    assert bad.status_code == 400


def test_backups_and_audit(client):
    backups = client.get("/api/backups").json()
    assert "items" in backups
    audit = client.get("/api/audit?limit=10").json()
    assert isinstance(audit["items"], list)


def test_maintenance_requires_confirmation(client):
    response = client.post("/api/maintenance/backup", json={"confirm": False})
    assert response.status_code == 400
    assert "confirm" in response.json()["error"].lower()


def test_delete_projects_requires_confirmation(client):
    """The destructive endpoint must refuse to run without an explicit confirm."""
    projects = client.get("/api/projects?limit=1").json()["items"]
    if not projects:
        pytest.skip("no projects in the database")
    response = client.post("/api/maintenance/delete-projects",
                            json={"ids": [projects[0]["id"]], "confirm": False})
    assert response.status_code == 400
    assert "confirm" in response.json()["error"].lower()


def test_delete_sessions_requires_confirmation(client):
    listing = client.get("/api/sessions?limit=1").json()
    if not listing["items"]:
        pytest.skip("no sessions in the database")
    response = client.post("/api/maintenance/delete-sessions",
                            json={"ids": [listing["items"][0]["id"]], "confirm": False})
    assert response.status_code == 400
    assert "confirm" in response.json()["error"].lower()


def test_move_sessions_requires_a_selection(client, tmp_path):
    """Without ids, filters or all=true nothing may be moved."""
    response = client.post("/api/maintenance/move-sessions",
                            json={"target": str(tmp_path), "confirm": True})
    assert response.status_code == 400
    assert "ids" in response.json()["error"].lower()


def test_static_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "OpenCode Dashboard" in response.text


def test_client_routes_serve_the_app_shell(client):
    """Every client-side route has to answer with the app, not with a 404."""
    for path in ("/sessions", "/projects", "/directories", "/backups", "/maintenance",
                 "/schema", "/storage", "/sessions/ses_does_not_exist", "/nonsense"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "OpenCode Dashboard" in response.text


def test_unknown_api_route_is_a_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404


def test_static_assets_expose_the_selection_ui(client):
    """The bulk bars and the checkbox markup must be served to the browser."""
    index = client.get("/").text
    assert 'id="session-bulkbar"' in index
    assert 'id="project-bulkbar"' in index
    script = client.get("/static/app.js").text
    assert "sel-box" in script
    assert "select-all" in script
