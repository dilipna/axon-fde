"""Export the control tower as a static snapshot, for hosting without a server.

    uv run poe demo-trace        # record the run the snapshot will show
    uv run poe export-site       # writes site/

The site reads three read-only endpoints. This script asks the real API for
exactly what the page would ask it for - through the application, not by
re-deriving the payloads - and writes each response to the path the page
fetches, next to a copy of the page. The result runs from any static host,
which is how a reader without Docker, SQL Server or Postgres gets to see it.

**It is a snapshot, and it says so.** The page it serves is the recorded run
it was exported from, with that run's timestamp; nothing is fabricated to
fill a gap. If the trace or a recording is missing, the export fails rather
than publishing a page whose sections quietly say "not available".

Paths are relative (`api/v1/...`, `ui/app.js`) so the same page works at `/`
behind the API and under a sub-path such as GitHub Pages' `/axon-fde/`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "control_tower"
OUT = ROOT / "site"

#: What the page fetches besides the trace's own scenario: the two scenarios
#: in the "is the thermometer lying?" comparison. Named in app.js as COMPARED.
COMPARED = ("compressor_degradation_pharma_01", "sensor_drift_pharma_01")


def export(out: Path = OUT) -> list[Path]:
    """Write the snapshot. Returns the files written. Raises on any gap."""
    if out.exists():
        shutil.rmtree(out)
    (out / "ui").mkdir(parents=True)

    written: list[Path] = []
    with TestClient(create_app()) as client:

        def fetch(path: str) -> dict[str, object]:
            response = client.get(f"/api/v1/{path}")
            if response.status_code != 200:
                raise SystemExit(
                    f"refusing to export: /api/v1/{path} answered {response.status_code} "
                    f"{response.text[:300]}"
                )
            payload: dict[str, object] = response.json()
            target = out / "api" / "v1" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload), encoding="utf-8")
            written.append(target)
            return payload

        trace = fetch("control/trace")
        fetch("control/claims")
        scenarios = {str(trace["scenario_id"]), *COMPARED}
        for scenario in sorted(scenarios):
            fetch(f"control/telemetry/{scenario}")

    for name in ("app.css", "app.js"):
        shutil.copy2(UI / name, out / "ui" / name)
        written.append(out / "ui" / name)
    shutil.copy2(UI / "index.html", out / "index.html")
    written.append(out / "index.html")
    # GitHub Pages runs Jekyll by default, which ignores some paths; a static
    # site that is already built should be served as-is.
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return written


def main() -> int:
    files = export()
    print(f"Wrote {len(files)} files to {OUT.relative_to(ROOT)}/ - serve it from any static host.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
