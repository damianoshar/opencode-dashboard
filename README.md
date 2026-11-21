# OpenCode Dashboard

A local dashboard for the OpenCode SQLite database (`~/.local/share/opencode/opencode.db`).
It exposes the same data and the same maintenance operations that were used to
recover, repair and reorganise the sessions in this environment - as a web UI
instead of one-off scripts.

```
C:\Users\Damianos\Documents\Default Project\opencode-dashboard
```

## Run

```powershell
cd "C:\Users\Damianos\Documents\Default Project\opencode-dashboard"
.\run.ps1                 # http://127.0.0.1:8791
.\run.ps1 -Port 9000      # different port
.\run.ps1 -ReadOnly       # refuse every mutating endpoint
```

or directly:

```powershell
python -m dashboard.app
```

Requirements: Python 3.10+, `fastapi`, `uvicorn` (see `requirements.txt`).
The server binds to `127.0.0.1` only - it is a local tool.

Environment overrides:

| Variable | Meaning |
|---|---|
| `OPENCODE_DB` | database file (default `~/.local/share/opencode/opencode.db`) |
| `OPENCODE_DATA_DIR` | OpenCode data dir (logs, storage, snapshots) |
| `DASHBOARD_DATA_DIR` | where backups, bundles and exports are written |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | bind address |
| `DASHBOARD_READ_ONLY` | `1` blocks all maintenance endpoints |

## What it shows

| Tab | Contents |
|---|---|
| **Overview** | tiles (sessions, messages, projects, events, DB size, free pages, tokens, cost), WAL/page stats, health (orphan messages, orphan events, **seq-lag sessions**), sessions-per-day chart, message-type / model / agent / directory breakdowns, row counts for every table |
| **Sessions** | the `session_v2` table with filters (project, directory, agent, model, state, root/child/fork, date range, free text), sortable columns, table / grid / compact views, paginated, live aggregates, CSV + JSON export of the filtered set, click-through to a message drawer, **checkbox column with bulk actions** |
| **Projects** | the `project` registry: worktree, VCS, session and message counts, cost, whether the directory still exists on disk, click a directory to filter the session list, **checkbox column with bulk actions** |
| **Directories** | every directory the app knows about, merged from `session_v2`, legacy `session`, `project`, `project_directory` and `worktree`, with orphan detection and on-disk checks |
| **Backups** | every backup file found (OpenCode data dir, dashboard backups, recovery workspaces) with size and mtime, one-click `quick_check` + `foreign_key_check` on any backup, and a backup creator |
| **Maintenance** | backup, verify, checkpoint, vacuum, prune projects, prune orphan counters, move sessions, export Markdown, export/restore a session bundle, repair event sequences, delete sessions and projects - each behind an explicit `confirm` and recorded in the audit log |
| **Schema** | every table with its columns, indexes and foreign keys, a row browser, and a read-only SQL console (`SELECT` / `WITH` / `PRAGMA` only) |
| **Storage & Logs** | size and file counts for the OpenCode data dir, logs, storage, snapshots, worktrees, tool output and the dashboard's own folders, plus a log tail viewer |

## Bulk selection and delete

Both the **Sessions** and the **Projects** tab have a checkbox column:

* tick single rows, or use the header checkbox to select every row on the page;
* **Select all N matching** switches to a server-side selection: the active
  filters are sent with the request, so the selection covers every row that
  matches - not only the rows that happen to be on the page;
* the bulk bar shows the live count and offers **Export bundles**,
  **Export Markdown**, **Move to…**, **Delete selected** and **Clear**.

### Deleting sessions

`delete-sessions` removes the session through the OpenCode API
(`opencode api session.remove --param sessionID=…`) and only then purges the
legacy mirror rows (`session`, `message`, `part`, …) and the event counter.
If the API call fails, **nothing is deleted**: the session keeps its messages,
the failure is listed in `details.api_failures` and the operation reports
`ok: false`. This matters - an earlier version purged the rows first and left
empty shells behind.

### Deleting projects - the cascade warning

`session_v2.project_id` and `session.project_id` are `ON DELETE CASCADE` to
`project.id`: **deleting a project destroys every session that belongs to it**.

`delete-projects` therefore refuses to touch a project that still owns
sessions, and reports what it found:

```
1 selected project(s) still own 67 session(s) (0 legacy mirror rows):
C:/Users/Damianos/Documents/Default Project (67 sessions).
Move the sessions to another project first, or repeat the delete with
delete_sessions=true to destroy them as well.
```

The legacy `session` mirror counts as ownership too - it cascades into
`message`, `todo` and `session_share`. The UI asks for a second, explicit
confirmation before it repeats the delete with `delete_sessions: true`.

Every mutating endpoint needs `{"confirm": true}` and is written to
`data/audit.log`.

## Maintenance operations

All of them refuse to run without `confirm: true`, are logged to
`data/audit.log`, and never stop the OpenCode service.

| Operation | What it does | Notes |
|---|---|---|
| `backup` | SQLite online backup into `data/backups` (or the OpenCode data dir) | writes a sidecar `.json` manifest with row counts |
| `checkpoint` | `PRAGMA wal_checkpoint(TRUNCATE)` | folds the WAL into the main file |
| `vacuum` | checkpoint + `secure_delete` + `VACUUM` | reclaims free pages; irreversible for deleted text |
| `prune-projects` | keeps one project, repoints legacy rows, deletes `permission` / `project_directory` / `worktree` | there is no project-delete API, so this is direct SQL in one transaction |
| `move-sessions` | `POST /api/session/{id}/move` for every session | needs `ids`, `filters` or `all: true` - never "everything" by accident |
| `delete-sessions` | `session.remove` via the API, then purges the legacy mirror rows | irreversible; nothing is purged when the API fails |
| `delete-projects` | deletes project rows | refuses projects that own sessions unless `delete_sessions: true` |
| `export-markdown` | one Markdown file per session + `manifest.json` | needs `ids`, `filters` or `all: true` |
| `export-bundle` | a self-contained JSON bundle for one session | restores later without a full DB backup |
| `restore-bundle` | re-inserts a session from a bundle | also advances `event_sequence.seq` |
| `repair-sequences` | aligns `event_sequence.seq` with `MAX(session_message.seq)` | see below |
| `prune-event-sequences` | removes the counters of deleted sessions | only session counters, and only when no `event` row still points at them |

### The `event_sequence` trap

`session_message` has `UNIQUE(session_id, seq)` and the app takes the next seq
from `event_sequence.seq` for the session's aggregate. A session restored with
plain `INSERT ... SELECT` keeps its messages but loses the counter, so the next
prompt tries a seq that already exists, every insert fails
(`Failed query: insert into "session_message"`), and **the session cannot send
prompts**.

Both `restore-bundle` and `repair-sequences` fix this, and the Overview tab
lists any session whose counter is behind its highest message seq.

`event_sequence` is not only a session table: it also holds the counters of
project aggregates and of a `global` aggregate. Only rows whose `aggregate_id`
starts with `ses_` and that have no session left are ever pruned - and only
when no `event` row still references them (otherwise the cascade would delete
the event history).

## Layout

```
opencode-dashboard/
  dashboard/
    config.py        paths and settings
    db.py            SQLite access (read-only by default, schema introspection)
    queries.py       projects, sessions, directories, stats, raw tables
    maintenance.py   backup, verify, shrink, prune, move, delete, export, repair
    api.py           HTTP routes
    app.py           FastAPI factory + static mount
    static/          index.html, app.js, styles.css
  scripts/
    verify_live.py   read-only health report of the live database
    ui_check.py      Playwright check of the selection UI
  tests/
    conftest.py      shared fixtures (live client, scratch database)
    test_api.py      read-only API smoke tests            (marker: live)
    test_selection.py bulk selection / delete / cascade   (marker: scratch)
  pytest.ini         test paths, markers, import path
  run.ps1            start the server
  run-tests.ps1      run the test suite
  data/              backups, bundles, exports, audit.log (created on first run)
```

## Tests

```powershell
.\run-tests.ps1            # everything (38 tests)
.\run-tests.ps1 -Live      # only the tests that read the live database
.\run-tests.ps1 -Scratch   # only the tests that use a temp database
.\run-tests.ps1 -Verbose   # full pytest output
```

`python -m pytest` works as well - `pytest.ini` points at `tests/` and puts the
project root on the import path.

Two kinds of tests:

* **live** - they talk to the real OpenCode database through the API, but only
  through read-only endpoints or with operations that are refused because
  `confirm` is missing;
* **scratch** - they build a miniature database in a temp directory with the
  real cascade rules and drive the maintenance code directly. The OpenCode CLI
  calls are faked, so a unit test can never reach the running service.

Extra checks that live outside pytest:

```powershell
python scripts/verify_live.py    # counts, quick_check, foreign_key_check, seq lag
python scripts/ui_check.py       # browser check of the selection UI (Playwright)
```

## Notes and pitfalls

- **Never run `opencode service stop`** from inside an OpenCode session - it
  kills the session doing the work. VACUUM and backups are safe with the
  service running.
- The OpenAPI operation for removing a session is **`session.remove`**.
  `session.delete` does not exist: the server answers "Operation not found",
  the delete silently does nothing, and anything that cleans up rows afterwards
  leaves an empty shell behind.
- The project registry **regrows**: any process that resolves a directory
  re-registers it. Pruning is a snapshot, not a policy.
- Old paths survive inside message text and inside backup files. VACUUM only
  clears free pages in the live file.
- `secure_delete=ON` + `VACUUM` destroys recoverability of deleted rows.
- The dashboard never deletes or moves anything on its own; every mutating
  endpoint needs an explicit `confirm`.
