"""Browser check of the bulk selection UI (Playwright).

    python scripts/ui_check.py                       # against 127.0.0.1:8791
    python scripts/ui_check.py --url http://... --out C:/temp/shots

The script only reads the dashboard: it selects rows, checks the bulk bar and
takes screenshots. It never clicks a destructive button.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8791")
    parser.add_argument("--out", default=str(Path.home() / "AppData/Local/Temp/opencode"))
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

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(args.url, timeout=30000)
        page.wait_for_selector(".view", timeout=15000)

        # --- sessions -----------------------------------------------------
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

        # --- projects -----------------------------------------------------
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

        browser.close()

    print("\nscreenshots in %s" % out)
    if failures:
        print("%d check(s) failed" % len(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
