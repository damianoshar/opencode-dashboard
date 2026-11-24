# OpenCode Dashboard

A local dashboard for the OpenCode SQLite database (`~/.local/share/opencode/opencode.db`).
It exposes the same data and the same maintenance operations that were used to
recover, repair and reorganise the sessions in this environment - as a web UI
instead of one-off scripts.

## Install

```powershell
git clone https://github.com/damianoshar/opencode-dashboard.git
cd opencode-dashboard
python -m pip install -r requirements.txt
```

## Run

```powershell
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
| `OPENCODE_STATE_DIR` | OpenCode state dir (default `~/.local/state/opencode`) |
| `OPENCODE_BIN` | path to the `opencode` executable (default: found on `PATH`) |
| `DASHBOARD_DATA_DIR` | where backups, bundles and exports are written |
| `DASHBOARD_RECOVERY_DIR` | recovery workspaces to list as backups (default `~/opencode-recovery`) |
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

## Page routing

Every tab has its own URL, so a page can be bookmarked, reloaded and shared,
and the browser Back / Forward buttons work as expected:

| Route | View |
|---|---|
| `/` | Overview |
| `/sessions` | Sessions |
| `/sessions/<session-id>` | Sessions with the message drawer open |
| `/projects` | Projects |
| `/directories` | Directories |
| `/backups` | Backups |
| `/maintenance` | Maintenance |
| `/schema` | Schema |
| `/storage` | Storage & Logs |

* clicking a tab pushes the route with `history.pushState`, the Back and
  Forward buttons are handled with `popstate`;
* the active filters are part of the URL: `/sessions?search=CV&state=…` and
  `/projects?search=<name>` - reloading the page restores the same result
  set;
* a deep link such as `/sessions/ses_…` opens the drawer for that session
  directly;
* an unknown route falls back to the Overview;
* the server serves the app shell for every client-side path, but unknown
  `/api/…` paths still answer `404` instead of returning HTML.

## Bulk selection and delete

Both the **Sessions** and the **Projects** tab have a checkbox column:

* tick single rows, or use the header checkbox to select every row on the page;
* **Select all N matching** switches to a server-side selection: the active
  filters are sent with the request, so the selection covers every row that
  matches - not only the rows that happen to be on the page;
* the bulk bar shows the live count and offers **Export bundles**,
  **Export Markdown**, **Move to…**, **Delete selected** and **Clear**.

The server resolves the selection itself: `filters: null` means "no
selection", while an empty filter set (no active filter) means "everything
that matches" - which is every row.

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
C:/path/to/your/project (67 sessions).
Move the sessions to another project first, or repeat the delete with
delete_sessions=true to destroy them as well.
```

The legacy `session` mirror counts as ownership too - it cascades into
`message`, `todo` and `session_share`. The UI therefore asks for a **typed
confirmation**: the second step is a `window.prompt` in which the word
`DELETE` has to be typed out. Anything else (including an empty answer or
just `OK`) cancels the operation, so a stray click, an automation run or a
mis-typed confirmation can never destroy real sessions by accident.

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
    verify_live.py       read-only health report of the live database
    ui_check.py          Playwright check of the routing and the selection UI
    restore_sessions.py  puts sessions that went missing back into the live DB
    build_docs.py        renders the GitHub Pages site
  docs/                source of the GitHub Pages site (placeholders, no URLs)
  .github/workflows/   pages.yml publishes the site
  tests/
    conftest.py      shared fixtures (live client, scratch database)
    test_api.py      read-only API smoke tests            (marker: live)
    test_selection.py bulk selection / delete / cascade   (marker: scratch)
  pytest.ini         test paths, markers, import path
  run.ps1            start the server
  run-tests.ps1      run the test suite
  data/              backups, bundles, exports, audit.log (created on first run)
```

## The GitHub Pages site

`docs/` is the source of the landing page. It contains `{{PLACEHOLDER}}`
markers instead of absolute URLs, so no account or machine is baked into the
repository:

| Placeholder | Value |
|---|---|
| `{{REPO_URL}}` | `https://github.com/<owner>/<repo>` |
| `{{REPO_NAME}}` | `<owner>/<repo>` |
| `{{VERSION}}` | the latest git tag |
| `{{YEAR}}` | the current year |

```powershell
python scripts/build_docs.py          # docs/ -> site/
python scripts/build_docs.py --repo owner/name
```

`.github/workflows/pages.yml` runs the same script on every push to `main`
and publishes `site/` through GitHub Pages.

## Tests

```powershell
.\run-tests.ps1            # everything (42 tests)
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
python scripts/ui_check.py       # browser check of routing + selection UI (Playwright)
```

## Recovering deleted sessions

`scripts/restore_sessions.py` puts sessions that went missing back into the
live database from a dashboard backup:

```powershell
python scripts/restore_sessions.py --backup data\backups\opencode.db.backup-<stamp>-dashboard --dry-run
python scripts/restore_sessions.py --backup data\backups\opencode.db.backup-<stamp>-dashboard --min-messages 4
```

* it only **inserts** rows that are missing - it never deletes anything;
* a session travels with its messages, parts, instructions, inbox, legacy
  mirror rows and events;
* the `event_sequence` counter of every restored session is raised to
  `MAX(session_message.seq)`, otherwise the restored session cannot send a
  prompt (see the `event_sequence` trap above);
* `--min-messages N` skips the empty shells and `--ids a,b,c` restores a
  specific set;
* sessions whose project no longer exists are skipped - `session_v2.project_id`
  is a foreign key.

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

## License

MIT - see [LICENSE](LICENSE).
