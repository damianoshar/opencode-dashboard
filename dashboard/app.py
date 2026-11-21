"""FastAPI application factory for the OpenCode dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import SETTINGS, Settings, load_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or SETTINGS
    settings.ensure_dirs()

    app = FastAPI(
        title="OpenCode Dashboard",
        description="Local dashboard for the OpenCode SQLite database: projects, sessions, "
                    "directories, backups and maintenance operations.",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.include_router(router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            """Serve the single page app for every client-side route.

            The dashboard routes in the browser: /sessions, /projects,
            /maintenance, /sessions/<id>, ... A deep link or a page reload has
            to end up in the same place as a click on the tab, so anything that
            is not an API call answers with the app shell.
            """
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(404, "unknown API route")
            return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = load_settings()
    print("OpenCode dashboard")
    print("  database : %s" % settings.db_path)
    print("  data dir : %s" % settings.data_dir)
    print("  listening: http://%s:%d" % (settings.host, settings.port))
    uvicorn.run("dashboard.app:app", host=settings.host, port=settings.port,
                reload=False, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
