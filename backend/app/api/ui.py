"""Serving the control tower.

A single static page, mounted at `/`. No build step, no bundler, no npm - the
page is HTML, CSS and one module of vanilla JavaScript, which is a deliberate
choice rather than a limitation.

**Why no framework.** The UI reads three JSON endpoints and renders them. A
toolchain would add a lockfile, a node version, a build task in CI and a
`dist/` nobody reviews, in exchange for conveniences this page does not need.
It would also make the UI the only part of the repository that cannot be run
straight from a checkout, which is a bad property for the one artifact whose
whole job is to be shown to somebody.

The page is served from the API process so there is a single thing to start.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

__all__ = ["UI_DIR", "mount_control_tower"]

UI_DIR = Path(__file__).resolve().parents[3] / "apps" / "control_tower"


def mount_control_tower(app: FastAPI) -> None:
    """Mount the control tower at `/`, if it is present.

    Absence is not fatal. The API is useful without a UI, and a missing
    directory should not stop the process that serves `/health` - which is the
    endpoint an orchestrator uses to decide whether this container is alive.
    """
    if not UI_DIR.is_dir():
        return

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    # Mounted after the API routers, so a static file can never shadow an
    # endpoint: `/api/v1/...` is matched first whatever lands in this folder.
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="control-tower")
