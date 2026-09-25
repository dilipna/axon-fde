"""The control tower's endpoints, and the invariant they are most likely to break.

The UI is the first place in this project where a number is shown to somebody
who will not read the code that produced it. Two properties therefore matter
more here than anywhere else, and both are asserted below:

**Nothing is a fixture.** Every endpoint reports absence as absence, with the
command that fixes it. A dashboard that renders sample data when the system has
not run looks identical to one rendering a healthy system, which makes it worse
than no dashboard.

**Ground truth stays out (I8).** `GroundTruthFrame` is benchmark-only. The
telemetry endpoint is the one place where serving it would be easy and would
look like a feature - a chart showing the true temperature beside the reported
one is exactly what the sensor-drift scenarios make tempting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1 import control
from backend.app.main import create_app

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = "compressor_degradation_pharma_01"

#: Every field of `GroundTruthFrame`. Listed exhaustively rather than spot
#: checked: a new field added upstream is exactly the way this leaks, and the
#: test that would have caught it is the one that checks all of them.
GROUND_TRUTH_FIELDS = (
    "true_cargo_temp_c",
    "reported_cargo_temp_c",
    "sensor_error_c",
    "compressor_health",
    "duty_cycle",
    "cooling_output_w",
    "heat_ingress_w",
    "saturated",
    "in_breach",
)


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def _recording_exists() -> bool:
    return (control.GENERATED_DIR / SCENARIO / "telemetry.parquet").is_file()


# ---------------------------------------------------------------------------
# Ground truth never reaches the application (I8)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _recording_exists(), reason="run `poe forge run-all` first")
def test_the_telemetry_endpoint_serves_no_ground_truth(client):
    """The chart gets what a sensor could report, and nothing else.

    On the sensor-drift scenarios the reported and true temperatures disagree
    by design, so an endpoint that leaked the true one would make the hardest
    case in the pack trivially easy - and would do it through the UI, where
    nobody is reading the diff.
    """
    body = client.get(f"/api/v1/control/telemetry/{SCENARIO}").json()
    serialised = json.dumps(body)

    for field in GROUND_TRUTH_FIELDS:
        assert field not in serialised, f"{field} reached the API"

    assert body["points"], "the recording produced no points"
    assert {"minute", "cargo_temp_c", "ambient_temp_c", "compressor_rpm", "fault_codes"} == set(
        body["points"][0]
    )


@pytest.mark.skipif(not _recording_exists(), reason="run `poe forge run-all` first")
def test_the_flagship_breaches_where_the_register_says_it_does(client):
    """Minute 137 is the first reading outside the envelope, through the API.

    The same minute is asserted against the physics by `poe forge verify`,
    against a real database by the replay integration test, and by AxonBench's
    lead-time grader. This is the fourth path to it - the one a viewer of the
    chart is actually looking at.
    """
    points = client.get(f"/api/v1/control/telemetry/{SCENARIO}").json()["points"]
    breaching = [p["minute"] for p in points if p["cargo_temp_c"] > 8.0]

    assert breaching, "the flagship never breaches its 8 C ceiling"
    assert breaching[0] == 137


# ---------------------------------------------------------------------------
# Absence is reported as absence
# ---------------------------------------------------------------------------


def test_an_unknown_scenario_is_a_404_that_says_how_to_fix_it(client):
    response = client.get("/api/v1/control/telemetry/no_such_scenario")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["available"] is False
    assert "forge run-all" in detail["fix"]


@pytest.mark.parametrize(
    "scenario_id",
    [
        "../../etc/passwd",
        "Uppercase_Id",
        "has-a-hyphen",
        "spaces here",
    ],
)
def test_a_scenario_id_that_is_not_one_is_refused(client, scenario_id: str):
    """The id becomes a path segment, so anything outside the schema is refused.

    Not because traversal is expected to work - it is rejected before the path
    is built - but because a request that could only be a typo or an attack
    should not reach the filesystem at all.
    """
    response = client.get(f"/api/v1/control/telemetry/{scenario_id}")
    assert response.status_code in (400, 404)
    assert "passwd" not in response.text


def test_the_claims_endpoint_serves_only_published_runs(client):
    """Exploratory runs may be recorded; only published ones may be shown.

    `benchmarks/results/*.json` is gitignored and full of runs nobody promoted.
    Serving the newest of *those* would put an unreviewed number on a screen,
    which is invariant I7 defeated by a convenience.
    """
    response = client.get("/api/v1/control/claims")
    if response.status_code == 404:
        pytest.skip("no published run in this checkout")

    body = response.json()
    published = {path.stem for path in control.PUBLISHED_DIR.glob("*.json")}
    assert f"{body['arm']}-{body['provenance']['run_id']}" in published


def test_a_measured_claim_carries_the_companions_it_may_not_be_quoted_without(client):
    """C1 without its false-alarm rate is a misuse, including in a UI.

    The card refuses to render the headline alone, so the API must actually
    supply the companion rather than the UI quietly omitting it.
    """
    response = client.get("/api/v1/control/claims")
    if response.status_code == 404:
        pytest.skip("no published run in this checkout")

    results = {r["claim_id"]: r for r in response.json()["results"]}
    c1 = results.get("C1")
    if c1 is None or c1["status"] != "MEASURED":
        pytest.skip("C1 is not measured in the published run")

    assert "false_alarm_rate" in c1["companions"]
    assert "baseline_false_alarm_rate" in c1["companions"]
    # The second median, which stops the headline overstating the real warning.
    assert "median_lead_time_after_fault_onset_min" in c1["companions"]
