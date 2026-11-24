"""Browser check of the dashboard: page routing and bulk selection.

    python scripts/ui_check.py                       # against 127.0.0.1:8791
    python scripts/ui_check.py --url http://... --out C:/temp/shots

The script drives the dashboard in a browser: it checks the routes, the
selection UI and the delete flow. The only destructive action is the deletion
of **one empty project** (a project without sessions) - that is what the
feature is for, and it removes a stale registry row.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8791")
    parser.add_argument("--out", default=str(Path.home() / "AppData/Local/Temp/opencode"))
    parser.add_argument("--skip-delete", action="store_true",
                        help="do not run the project delete check")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed: python -m pip install playwright && "
              "playwright install chromium", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        print("%-58s %s" % (message, "ok" if condition else "FAILED"))
        if not condition:
            failures.append(message)

    base = args.url.rstrip("/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        dialogs: list[str] = []

        def on_dialog(dialog):
            """Accept the first dialog, refuse everything after it.

            The second prompt of the project delete asks for the word DELETE
            before it destroys the sessions of a project. This script must never
            type it: a stray accept would wipe real sessions.
            """
            dialogs.append(dialog.message)
            if len(dialogs) == 1:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", on_dialog)
        page.goto(base + "/", timeout=30000)
        page.wait_for_selector("#view-overview:not(.hidden)", timeout=15000)

        # --- routing --------------------------------------------------------
        check(page.url.replace(base, "") == "/", "root route is /")

        page.click('.tab[data-view="sessions"]')
        page.wait_for_selector("#sessions-table table", timeout=15000)
        check(page.url.replace(base, "") == "/sessions", "tab click routes to /sessions")

        page.fill("#f-search", "CV")
        page.wait_for_timeout(900)
        check("search=CV" in page.url, "the filter ends up in the query string")

        rows = page.query_selector_all("#sessions-table tbody tr")
        if rows:
            rows[0].click()
            page.wait_for_timeout(1200)
            check("/sessions/" in page.url, "a session row routes to /sessions/<id>")
            check(page.is_visible("#drawer"), "the drawer opens on a deep link")
            page.click("#drawer-close")
            page.wait_for_timeout(400)
            check("/sessions/" not in page.url, "closing the drawer returns to /sessions")

        page.go_back()
        page.wait_for_timeout(600)
        check(page.is_visible("#sessions-table"), "the back button keeps the app alive")

        # a search term that exists in this database: take the worktree of the
        # first project and use its last path segment
        page.goto(base + "/projects", timeout=30000)
        page.wait_for_selector("#projects-table table", timeout=15000)
        worktree = page.query_selector("#projects-table [data-project]").get_attribute("title")
        search_term = (worktree or "").replace("\\", "/").rstrip("/").split("/")[-1]
        page.goto(base + "/projects?search=" + search_term, timeout=30000)
        page.wait_for_selector("#projects-table table", timeout=15000)
        check(page.input_value("#p-search") == search_term,
              "the project search is restored from the URL")

        page.goto(base + "/nonsense", timeout=30000)
        page.wait_for_timeout(700)
        check(page.is_visible("#view-overview"), "an unknown route falls back to overview")

        # --- selection ------------------------------------------------------
        page.click('.tab[data-view="sessions"]')
        page.wait_for_selector("#sessions-table table", timeout=15000)
        boxes = page.query_selector_all("#sessions-table .sel-box")
        check(len(boxes) > 0, "session rows expose a checkbox (%d)" % len(boxes))
        check(not page.is_visible("#session-bulkbar"), "bulk bar hidden before a selection")

        page.check("#sessions-table .sel-box")
        page.wait_for_timeout(300)
        check(page.is_visible("#session-bulkbar"), "bulk bar appears after selecting a row")
        count = page.inner_text("#session-selection-count")
        check("1 selected" in count, "bulk bar reports the selection (%r)" % count)
        page.screenshot(path=str(out / "ui-sessions-selection.png"))

        page.click('#session-bulkbar [data-bulk="select-all"]')
        page.wait_for_timeout(600)
        count = page.inner_text("#session-selection-count")
        check("all matching" in count, "select-all matching works (%r)" % count)
        page.screenshot(path=str(out / "ui-sessions-select-all.png"))

        page.click('#session-bulkbar [data-bulk="clear"]')
        page.wait_for_timeout(300)
        check(not page.is_visible("#session-bulkbar"), "clear hides the bulk bar")

        for mode in ("grid", "compact"):
            page.click('[data-mode="%s"]' % mode)
            page.wait_for_timeout(500)
            boxes = page.query_selector_all("#sessions-table .sel-box")
            check(len(boxes) > 0, "%s view keeps the checkboxes (%d)" % (mode, len(boxes)))
            page.screenshot(path=str(out / ("ui-sessions-%s.png" % mode)))
        page.click('[data-mode="table"]')
        page.wait_for_timeout(300)

        # --- projects -------------------------------------------------------
        page.click('.tab[data-view="projects"]')
        page.wait_for_selector("#projects-table table", timeout=15000)
        pboxes = page.query_selector_all("#projects-table .project-sel")
        check(len(pboxes) > 0, "project rows expose a checkbox (%d)" % len(pboxes))
        page.check("#projects-table .project-sel")
        page.wait_for_timeout(300)
        check(page.is_visible("#project-bulkbar"), "project bulk bar appears")
        count = page.inner_text("#project-selection-count")
        check("1 selected" in count, "project bulk bar reports the selection (%r)" % count)
        page.screenshot(path=str(out / "ui-projects-selection.png"))

        # --- delete an empty project ----------------------------------------
        if not args.skip_delete:
            # drop the selection of the check above: it points at the first
            # row, which is the project that owns the sessions
            if page.is_visible('#project-bulkbar [data-bulk="clear"]'):
                page.click('#project-bulkbar [data-bulk="clear"]')
                page.wait_for_timeout(600)

            empty = None
            for row in page.query_selector_all("#projects-table tbody tr"):
                cells = row.query_selector_all("td")
                if len(cells) > 3 and cells[3].inner_text().strip() == "0":
                    empty = row
                    break
            if empty is None:
                print("%-58s skipped" % "delete an empty project (no empty project left)")
            else:
                project_id = empty.query_selector(".project-sel").get_attribute("data-id")
                empty.query_selector(".project-sel").check()
                page.wait_for_timeout(200)
                dialogs.clear()
                page.click('#project-bulkbar [data-bulk="delete"]')
                page.wait_for_timeout(2500)
                check(bool(dialogs), "the delete asks for a confirmation")
                check(len(dialogs) == 1,
                      "an empty project is deleted without a cascade prompt (%d dialogs)"
                      % len(dialogs))
                result = page.inner_text("#projects-result")
                check('"ok": true' in result, "the empty project was deleted (%s)"
                      % result.replace("\n", " ")[:160])
                check(page.query_selector('[data-id="%s"]' % project_id) is None,
                      "the deleted project is gone from the table")
                page.screenshot(path=str(out / "ui-project-delete.png"))
        browser.close()

    print("\nscreenshots in %s" % out)
    if failures:
        print("%d check(s) failed" % len(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
