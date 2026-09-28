"""The public static snapshot (`site/`) must not drift from the repository.

`site/` is what GitHub Pages serves - the version of this project most people
will actually see. It is exported by `poe export-site` and committed, so it
can go stale silently: a new benchmark run gets published, the register moves,
and the public page keeps showing the old numbers. These tests make that a red
build instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
UI = ROOT / "apps" / "control_tower"
PUBLISHED = ROOT / "benchmarks" / "results" / "published"

pytestmark = pytest.mark.skipif(not SITE.is_dir(), reason="no site/ snapshot exported")

CRLF = chr(13) + chr(10)
LF = chr(10)


def _text(path: Path) -> str:
    """Line endings normalised: git normalises both files on commit, and a
    Windows checkout can differ from an export in CRLF alone."""
    return path.read_text(encoding="utf-8").replace(CRLF, LF)


def test_the_snapshot_shows_the_newest_published_run() -> None:
    snapshot = json.loads((SITE / "api/v1/control/claims").read_text(encoding="utf-8"))
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in PUBLISHED.glob("*.json")]
    newest = max(runs, key=lambda run: run["completed_at"])
    assert snapshot["provenance"]["run_id"] == newest["provenance"]["run_id"], (
        "site/ is stale: re-run `uv run poe demo-trace && uv run poe export-site`"
    )
    assert snapshot["provenance"]["reproducible"] is True


@pytest.mark.parametrize("name", ["index.html", "app.css", "app.js"])
def test_the_snapshot_page_is_the_current_page(name: str) -> None:
    copy = SITE / name if name == "index.html" else SITE / "ui" / name
    assert _text(copy) == _text(UI / name), (
        f"site/ carries an old {name}: re-run `uv run poe export-site`"
    )


def test_the_snapshot_serves_no_ground_truth() -> None:
    """I8 holds on the public copy too, not only on the live endpoint."""
    for path in (SITE / "api/v1/control/telemetry").iterdir():
        points = json.loads(path.read_text(encoding="utf-8"))["points"]
        assert points
        assert all("true_cargo_temp_c" not in point for point in points)
