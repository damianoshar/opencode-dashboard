"""FastAPI application factory for the OpenCode dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
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
