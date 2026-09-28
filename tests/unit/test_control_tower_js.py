"""The control tower's JavaScript, tested.

Until 2026-09-28 the JS had no automated test at all - it was checked by eye
and by a one-off DOM shim. Two things on the page are load-bearing enough
that "looks right" is not a check:

- **C1 may never be shown without its false-alarm rate.** The card and the
  hero band both refuse; `c1Pair` returning ``None`` is what makes them refuse.
- **The four comparison figures must equal the register's.** They are
  recomputed in the browser from served telemetry, over the 71-100 window
  `claims.md` uses. A changed window convention moved one of them by 0.4
  rpm/min once, silently (B15a).

The harness runs the shipped `app.js` functions under Node, not copies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1 import control
from backend.app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "apps" / "control_tower" / "app.js"
HARNESS = Path(__file__).with_name("tower_harness.mjs")

#: claims.md's figures for the lying-sensor comparison (the "Two shipments"
#: panel), at the precision the page prints them.
REGISTER = {
    "compressor_degradation_pharma_01": {"cargo_temp_c": 0.016, "compressor_rpm": -9.2},
    "sensor_drift_pharma_01": {"cargo_temp_c": 0.039, "compressor_rpm": 2.0},
}

NODE = shutil.which("node")


def _recordings_exist() -> bool:
    return all(
        (control.GENERATED_DIR / scenario / "telemetry.parquet").is_file() for scenario in REGISTER
    )


def _run_harness(tmp_path: Path, telemetry: dict[str, list[dict[str, object]]]) -> dict:  # type: ignore[type-arg]
    assert NODE is not None
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps({"telemetry": telemetry}), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(HARNESS), str(APP_JS), str(payload)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)  # type: ignore[no-any-return]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_c1_is_refused_without_its_false_alarm_rate(tmp_path: Path) -> None:
    out = _run_harness(tmp_path, {})
    assert out["c1"]["without_far"] is None
    assert out["c1"]["required"] is True
    assert "30%" in out["c1"]["with_far"] and "20%" in out["c1"]["with_far"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_terminal_escape_codes_are_stripped(tmp_path: Path) -> None:
    assert _run_harness(tmp_path, {})["ansi"] == "CS-13: full"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.skipif(not _recordings_exist(), reason="run `poe forge run-all` first")
def test_the_comparison_figures_equal_the_register(tmp_path: Path) -> None:
    with TestClient(create_app()) as client:
        telemetry = {
            scenario: client.get(f"/api/v1/control/telemetry/{scenario}").json()["points"]
            for scenario in REGISTER
        }
    out = _run_harness(tmp_path, telemetry)
    assert out["window"] == [71, 100], "the register's window is the 30 readings ending at 100"
    for scenario, expected in REGISTER.items():
        got = out["slopes"][scenario]
        assert round(got["cargo_temp_c"], 3) == pytest.approx(expected["cargo_temp_c"])
        assert round(got["compressor_rpm"], 1) == pytest.approx(expected["compressor_rpm"])
