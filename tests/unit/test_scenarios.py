"""Tests for IncidentForge scenario definitions.

A mislabelled scenario silently corrupts every benchmark number computed from
it, and unlike a crash it leaves no trace. The schema validation covered here
is what makes that failure mode loud instead of silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from backend.app.domain.enums import ActionType, RootCause
from simulator.incidentforge.scenarios import (
    DataFault,
    GroundTruth,
    Scenario,
    load_pack,
)

pytestmark = pytest.mark.unit

PACK_DIR = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "pack_v1"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_DIR)


# ---------------------------------------------------------------------------
# The shipped pack
# ---------------------------------------------------------------------------


def test_shipped_pack_loads_and_validates(pack):
    assert pack.pack_version
    assert len(pack.scenarios) >= 3


def test_pack_is_large_enough_for_the_claims_measured_against_it(pack):
    """The dataset requirements of C1 and C6, asserted where the dataset lives.

    This replaced an assertion that the pack version was the literal "1.0.0",
    which broke on the first minor bump while protecting nothing: `ScenarioPack`
    already refuses a pack whose scenarios disagree with its declared version.
    What can actually break silently is the *size* of the pack, and the failure
    is specific - delete nine breach scenarios and C1 drops back to
    `INSUFFICIENT_DATA` with nothing pointing at the change that did it.

    The numbers come from `docs/evaluation/claims.md` rather than from what the
    pack happens to hold, so the pack cannot satisfy this test by accident.
    """
    breaching = [s for s in pack.scenarios.values() if s.ground_truth.breach_occurs]
    controls = [s for s in pack.scenarios.values() if not s.ground_truth.breach_occurs]
    conflicts = [
        fault
        for scenario in pack.scenarios.values()
        for fault in scenario.data_faults
        if fault.sides is not None
    ]

    assert len(breaching) >= 40, (
        f"C1's method requires at least 40 true-breach scenarios; the pack has "
        f"{len(breaching)}. Lead time cannot be claimed without them."
    )
    assert controls, (
        "a pack of breaches alone gives a false-alarm rate of zero over a dataset "
        "with nothing to falsely alarm on, which is not a measurement"
    )
    assert len(conflicts) >= 20, (
        f"C6's method requires about 20 seeded conflicts; the pack seeds "
        f"{len(conflicts)}. Precision and recall over fewer are degenerate."
    )


def test_the_breaching_scenarios_span_several_generative_regimes(pack):
    """C1's median must not be one fault mode wearing different numbers.

    Forty reskins of compressor degradation would satisfy the size test above
    and measure that one regime rather than the detector.

    **Regime is the set of injected faults, not the declared root cause.**
    Those differ, and the difference is not pedantry: a compressor failing
    behind a sensor that has frozen at 5 C is the same declared root cause as a
    compressor failing in plain view, and a completely different thing to
    detect - in the first the threshold alarm never fires at all. Counting by
    root cause put compressor degradation at exactly 40% of the pack and hid
    five scenarios whose whole point is the lying instrument. `claims.md` says
    "generative regime" throughout, and this is what that phrase means.
    """
    regimes: dict[frozenset[str], int] = {}
    causes: set[RootCause] = set()
    for scenario in pack.scenarios.values():
        if not scenario.ground_truth.breach_occurs:
            continue
        key = frozenset(fault.type.value for fault in scenario.injected_faults)
        regimes[key] = regimes.get(key, 0) + 1
        causes.add(scenario.ground_truth.root_cause)

    assert len(causes) >= 4, f"breaches trace to only {sorted(causes)}"
    assert len(regimes) >= 6, f"only {len(regimes)} distinct fault combinations breach"

    breaching = sum(regimes.values())
    worst_key, worst = max(regimes.items(), key=lambda item: item[1])
    assert worst <= breaching * 0.4, (
        f"{sorted(worst_key)} supplies {worst} of {breaching} breaches; the "
        "median lead time would mostly be a property of that one regime"
    )


def test_pack_covers_fault_no_fault_and_data_only_cases(pack):
    """Three distinct shapes, each measuring something different."""
    ids = set(pack.scenarios)
    assert "compressor_degradation_pharma_01" in ids  # the flagship
    assert "normal_pharma_run_01" in ids  # false-alarm control
    assert "sensor_drift_pharma_01" in ids  # multimodal ablation


def test_pack_contains_a_no_fault_control(pack):
    """Without a healthy scenario there is no false-alarm rate, and without a
    false-alarm rate the lead-time claim (C1) is not quotable."""
    controls = [
        s for s in pack.scenarios.values() if s.ground_truth.root_cause is RootCause.NO_FAULT
    ]
    assert controls, "pack must contain at least one no-fault control scenario"
    for scenario in controls:
        assert not scenario.ground_truth.breach_occurs
        assert not scenario.injected_faults
        # Rerouting a healthy load is the expensive false positive.
        assert ActionType.REROUTE_TO_COLD_STORAGE in scenario.expected.forbidden_actions


def test_seeds_are_unique_across_the_pack(pack):
    """Shared seeds would correlate scenarios and quietly weaken the held-out
    split used for risk-model evaluation."""
    seeds = [s.seed for s in pack.scenarios.values()]
    assert len(seeds) == len(set(seeds))


def test_breaching_scenarios_are_identified(pack):
    """This is the population over which lead time is computed."""
    breaching = pack.breaching_scenarios
    assert breaching
    for scenario in breaching:
        assert scenario.ground_truth.breach_at_min is not None


# ---------------------------------------------------------------------------
# The flagship scenario carries the properties the demo depends on
# ---------------------------------------------------------------------------


def test_flagship_scenario_stays_in_spec_until_late(pack):
    """The whole premise: a threshold detector sees nothing for over two hours.

    The exact minute is derived from the calibrated thermal model rather than
    chosen; `tests/unit/test_physics.py` asserts the simulation still
    reproduces it.
    """
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    assert scenario.ground_truth.breach_at_min == 137
    assert scenario.ground_truth.breach_at_min > 120


def test_flagship_scenario_carries_the_erp_bol_conflict(pack):
    """Customer brief section 6.3, reproduced as an executable fixture."""
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    mismatches = [d for d in scenario.data_faults if d.type == "erp_bol_mismatch"]
    assert len(mismatches) == 1

    mismatch = mismatches[0]
    assert mismatch.field == "permitted_temp_max_c"
    assert mismatch.erp_value == 10.0
    assert mismatch.document_value == 8.0
    # The cargo spec must reflect the authoritative document, not the ERP.
    assert scenario.cargo.permitted_max_c == 8.0


def test_flagship_scenario_requires_approval(pack):
    """A reroute is consequential and can never be automatic."""
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    assert scenario.expected.approval_required
    assert ActionType.REROUTE_TO_COLD_STORAGE in scenario.expected.allowed_actions
    assert ActionType.CONTINUE_ROUTE in scenario.expected.forbidden_actions


def test_sensor_drift_scenario_has_no_real_breach(pack):
    """The cargo is fine; only the instrument is wrong. A system that reports a
    breach here has been fooled, and rerouting would waste real money."""
    scenario = pack.scenarios["sensor_drift_pharma_01"]
    assert scenario.ground_truth.root_cause is RootCause.SENSOR_MALFUNCTION
    assert not scenario.ground_truth.breach_occurs
    assert ActionType.REROUTE_TO_COLD_STORAGE in scenario.expected.forbidden_actions
    # Visual evidence is what distinguishes this from a genuine excursion.
    assert "panel_photo_temperature" in scenario.ground_truth.decisive_evidence


def test_no_scenario_permits_the_agent_to_execute_directly(pack):
    """Execution happens only through an approved action, never as a tool call."""
    for scenario in pack.scenarios.values():
        assert "execute_approved_action" in scenario.expected.forbidden_tools


# ---------------------------------------------------------------------------
# Ground truth must match what is actually injected
# ---------------------------------------------------------------------------


def _minimal_scenario(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "scenario_id": "test_case",
        "pack_version": "1.0.0",
        "description": "fixture",
        "seed": 1,
        "duration_minutes": 120,
        "vehicle_id": "AX-001",
        "shipment_id": "SH-0001",
        "cargo": {
            "cargo_class": "pharma_2_8",
            "value_usd": 1000.0,
            "permitted_min_c": 2.0,
            "permitted_max_c": 8.0,
        },
        "ambient": {"type": "diurnal_mild", "peak_c": 25.0, "min_c": 12.0},
        "injected_faults": [],
        "data_faults": [],
        "ground_truth": {
            "root_cause": "no_fault",
            "contributing": [],
            "breach_occurs": False,
            "correct_action": "continue_route",
        },
        "expected": {
            "risk_category": "low",
            "approval_required": False,
        },
    }
    base.update(overrides)
    return base


def test_declared_cause_must_be_produced_by_an_injected_fault():
    """The check that stops a mislabelled scenario corrupting the benchmark."""
    with pytest.raises(ValidationError, match="not produced by any injected fault"):
        Scenario.model_validate(
            _minimal_scenario(
                injected_faults=[{"type": "door_left_open", "start_min": 10}],
                ground_truth={
                    "root_cause": "compressor_degradation",  # nothing injected it
                    "breach_occurs": False,
                    "correct_action": "continue_route",
                },
            )
        )


def test_contributing_causes_must_also_be_explained():
    with pytest.raises(ValidationError, match="contributing causes"):
        Scenario.model_validate(
            _minimal_scenario(
                injected_faults=[{"type": "compressor_degradation", "start_min": 10}],
                ground_truth={
                    "root_cause": "compressor_degradation",
                    "contributing": ["route_delay"],  # never injected
                    "breach_occurs": False,
                    "correct_action": "continue_route",
                },
            )
        )


def test_scenario_with_faults_cannot_declare_no_fault():
    with pytest.raises(ValidationError, match="declares NO_FAULT"):
        Scenario.model_validate(
            _minimal_scenario(
                injected_faults=[{"type": "compressor_degradation", "start_min": 10}],
                ground_truth={
                    "root_cause": "no_fault",
                    "breach_occurs": False,
                    "correct_action": "continue_route",
                },
            )
        )


def test_scenario_without_faults_cannot_declare_a_breach():
    with pytest.raises(ValidationError, match="breach with no faults"):
        Scenario.model_validate(
            _minimal_scenario(
                ground_truth={
                    "root_cause": "no_fault",
                    "breach_occurs": True,
                    "breach_at_min": 60,
                    "correct_action": "continue_route",
                }
            )
        )


def test_fault_must_start_within_the_run():
    with pytest.raises(ValidationError, match="beyond the"):
        Scenario.model_validate(
            _minimal_scenario(
                injected_faults=[{"type": "compressor_degradation", "start_min": 500}],
                ground_truth={
                    "root_cause": "compressor_degradation",
                    "breach_occurs": False,
                    "correct_action": "continue_route",
                },
            )
        )


def test_breach_must_occur_within_the_run():
    """A breach after the run ends is not observable, so it cannot be graded."""
    with pytest.raises(ValidationError, match="not observable"):
        Scenario.model_validate(
            _minimal_scenario(
                injected_faults=[{"type": "compressor_degradation", "start_min": 10}],
                ground_truth={
                    "root_cause": "compressor_degradation",
                    "breach_occurs": True,
                    "breach_at_min": 9999,
                    "correct_action": "reroute_to_cold_storage",
                },
            )
        )


# ---------------------------------------------------------------------------
# Component-level validation
# ---------------------------------------------------------------------------


def test_breach_flag_and_time_must_agree():
    with pytest.raises(ValidationError, match="breach_at_min is not set"):
        GroundTruth.model_validate(
            {
                "root_cause": "compressor_degradation",
                "breach_occurs": True,
                "correct_action": "reroute_to_cold_storage",
            }
        )
    with pytest.raises(ValidationError, match="breach_occurs is false"):
        GroundTruth.model_validate(
            {
                "root_cause": "compressor_degradation",
                "breach_occurs": False,
                "breach_at_min": 60,
                "correct_action": "reroute_to_cold_storage",
            }
        )


def test_a_cause_cannot_be_both_root_and_contributing():
    with pytest.raises(ValidationError, match="both root cause and contributing"):
        GroundTruth.model_validate(
            {
                "root_cause": "compressor_degradation",
                "contributing": ["compressor_degradation"],
                "breach_occurs": False,
                "correct_action": "continue_route",
            }
        )


def test_erp_bol_mismatch_requires_two_differing_sides():
    with pytest.raises(ValidationError, match="requires"):
        DataFault.model_validate({"type": "erp_bol_mismatch", "field": "permitted_temp_max_c"})
    with pytest.raises(ValidationError, match="not a disagreement"):
        DataFault.model_validate(
            {
                "type": "erp_bol_mismatch",
                "field": "permitted_temp_max_c",
                "erp_value": 8.0,
                "document_value": 8.0,
            }
        )


def test_sensor_panel_disagreement_requires_two_differing_sides():
    """The same rule for the second two-sided fault, or C6's recall is inflated.

    A seeded conflict that was never actually two-sided still counts in the
    denominator of recall, so the detector is marked down for missing a
    conflict that does not exist.
    """
    with pytest.raises(ValidationError, match="requires"):
        DataFault.model_validate({"type": "sensor_panel_disagreement", "field": "cargo_temp_c"})
    with pytest.raises(ValidationError, match="not a disagreement"):
        DataFault.model_validate(
            {
                "type": "sensor_panel_disagreement",
                "field": "cargo_temp_c",
                "telemetry_value": 6.0,
                "panel_value": 6.0,
            }
        )


def test_a_value_declared_on_the_wrong_side_is_refused():
    """`erp_value` on a sensor-vs-panel disagreement would be silently dropped.

    The scenario would then read as seeding a conflict it does not seed. This
    is the failure the two-sided rule cannot catch on its own, because the
    fault still has its own two sides filled in.
    """
    with pytest.raises(ValidationError, match="belong to a different"):
        DataFault.model_validate(
            {
                "type": "sensor_panel_disagreement",
                "field": "cargo_temp_c",
                "telemetry_value": 7.4,
                "panel_value": 4.1,
                "erp_value": 10.0,
            }
        )


def test_sides_names_the_source_that_said_each_value():
    """The grader builds evidence from this, so a swap would misattribute both."""
    fault = DataFault.model_validate(
        {
            "type": "sensor_panel_disagreement",
            "field": "cargo_temp_c",
            "telemetry_value": 7.4,
            "panel_value": 4.1,
        }
    )
    assert fault.sides == (("telemetry", 7.4), ("visual_inspection", 4.1))

    erp = DataFault.model_validate(
        {
            "type": "erp_bol_mismatch",
            "field": "permitted_temp_max_c",
            "erp_value": 10.0,
            "document_value": 8.0,
        }
    )
    assert erp.sides == (("sql_legacy", 10.0), ("document_extraction", 8.0))


def test_a_one_sided_data_fault_has_no_sides():
    """A stale reading is an absence, not a disagreement, and says so.

    Returning an invented second value here would hand C6 a positive case that
    nothing seeded.
    """
    stale = DataFault.model_validate({"type": "stale_telemetry"})
    assert stale.sides is None


def test_cargo_envelope_must_be_increasing():
    with pytest.raises(ValidationError, match="must be below"):
        Scenario.model_validate(
            _minimal_scenario(
                cargo={
                    "cargo_class": "pharma_2_8",
                    "value_usd": 1000.0,
                    "permitted_min_c": 8.0,
                    "permitted_max_c": 2.0,
                }
            )
        )


def test_tools_cannot_be_both_required_and_forbidden():
    with pytest.raises(ValidationError, match="both required and forbidden"):
        Scenario.model_validate(
            _minimal_scenario(
                expected={
                    "risk_category": "low",
                    "approval_required": False,
                    "required_tools": ["get_shipment"],
                    "forbidden_tools": ["get_shipment"],
                }
            )
        )


def test_unknown_fields_are_rejected():
    """Catches typos in hand-authored YAML, which would otherwise be ignored."""
    with pytest.raises(ValidationError):
        Scenario.model_validate(_minimal_scenario(unexpected_key="oops"))


# ---------------------------------------------------------------------------
# Pack loading
# ---------------------------------------------------------------------------


def test_scenario_id_must_match_its_filename(tmp_path: Path):
    (tmp_path / "pack.yaml").write_text(
        yaml.safe_dump({"pack_version": "1.0.0", "description": "x"}), encoding="utf-8"
    )
    (tmp_path / "wrong_name.yaml").write_text(yaml.safe_dump(_minimal_scenario()), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match the filename"):
        load_pack(tmp_path)


def test_pack_version_mismatch_is_rejected(tmp_path: Path):
    """A scenario claiming a different pack version makes results incomparable."""
    (tmp_path / "pack.yaml").write_text(
        yaml.safe_dump({"pack_version": "2.0.0", "description": "x"}), encoding="utf-8"
    )
    (tmp_path / "test_case.yaml").write_text(yaml.safe_dump(_minimal_scenario()), encoding="utf-8")
    with pytest.raises(ValueError, match="different pack_version"):
        load_pack(tmp_path)


def test_empty_pack_is_rejected(tmp_path: Path):
    (tmp_path / "pack.yaml").write_text(
        yaml.safe_dump({"pack_version": "1.0.0", "description": "x"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="contains no scenarios"):
        load_pack(tmp_path)


def test_missing_pack_metadata_is_reported_clearly(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="pack metadata"):
        load_pack(tmp_path)
