"""Tests for simulation output and its reproducibility guarantee.

The golden digests below are the strongest reproducibility claim this project
makes. A change to the physics, the fault schedule, the sensor model or the
event schema will change them, and that is the point: such a change silently
invalidates every stored benchmark result, so it must be a deliberate act that
updates these constants rather than something that slips through unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from simulator.incidentforge.emitters.files import (
    GROUND_TRUTH_FILE,
    TELEMETRY_FILE,
    compute_digest,
    write_run,
)
from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.scenarios import load_pack

pytestmark = pytest.mark.unit

PACK_DIR = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "pack_v1"

#: Golden digests. Updating these is a deliberate act, not a fix.
#:
#: The three v1.0.0 scenarios keep the digests they had before pack v1.1.0
#: added the other 57. That is the check that the minor version bump was
#: honest: adding scenarios must not change what an existing one means, and
#: if any of those three had moved, every benchmark result recorded against
#: 1.0.0 would have stopped being comparable without anyone saying so.
GOLDEN_DIGESTS = {
    "ambient_heat_frozen_control_01": (
        "57507269ca838d8b781faeee8a346f967e61f2c28639a6ceee5adabff5d48a84"
    ),
    "ambient_heat_pharma_01": ("ba412f1f7fdb49b155277bcf7aa5e6145d385b6a6b31ee7b56cdc4b41f09fcdf"),
    "ambient_heat_pharma_02": ("1bb1fa7cf671375d5daad1e54d4709184516a57355085b96718629d29816d210"),
    "ambient_heat_pharma_03": ("ac376e3056a7cd5ec1ce5d3a7fe4a49605b76d9a11a8b3024f916fc5c75fa5b9"),
    "ambient_heat_pharma_control_01": (
        "eb0d8061fbe90a5e61c07d1784960525505062a7f61cd13884fb41d51167cdd2"
    ),
    "ambient_heat_produce_control_01": (
        "225314bfd382dc70ad1df46c3f602f2be126d6d7f6b7b6cb9b6e05e086f19634"
    ),
    "ambient_heat_vaccine_01": ("26885650db1145f1612cc292afca85a45a495dea46b9818ecfd310679bb05a4e"),
    "ambient_heat_vaccine_02": ("3b7cc1ef9bff04d60f68c5af6c42ed5ef1ae036815dd91251a3d8f9879ae8b10"),
    "ambient_heat_vaccine_03": ("372121e00ef3580fe5157996689273b4833a182d19980ebcc21ab53b3dfd16a8"),
    "ambient_heat_vaccine_control_01": (
        "51b8f7cab7dc672316f94f8f6b3e85a3172f0ea2fa6a120a484023dd8324ca1e"
    ),
    "compressor_degradation_frozen_01": (
        "da35380d3642fa53c1560f7644c01a7e642eaba77ddf2906c58126164c0fb18f"
    ),
    "compressor_degradation_frozen_02": (
        "0730f07f185026f24a0227a1ea1e5839bf9f6d68605a7cfe9ce136b7a9b6da52"
    ),
    "compressor_degradation_pharma_01": (
        "cd8de617ff569bd70ec6a2705c79621a486961f0fca682809bbb47f34583a600"
    ),
    "compressor_degradation_pharma_02": (
        "8e0b11925873c2b3742cc313fe55ccdec9ef5653ddc5d11da3f4e96dbef8c6da"
    ),
    "compressor_degradation_pharma_03": (
        "01f502838d3d8ceec74a1a4ea3ac47da4cae2591e067f83812b819830ab04558"
    ),
    "compressor_degradation_produce_01": (
        "24c0f11af0c09cb446f490baeab0d460d98037fe72ec53878c0662b461a2977c"
    ),
    "compressor_degradation_produce_02": (
        "3c640b37c66a71e892d5976cc78aebdda477f79355f46082ca5670c87d4bf45b"
    ),
    "compressor_degradation_vaccine_01": (
        "9a81bb93c3ece0e3824ba7cda67d7d8e3a22dd29c519f37ac9fa864da332966d"
    ),
    "compressor_degradation_vaccine_02": (
        "3cd7d732e7664e52e9b78c5aa3e7293de495c1c148edfcbab025508659959488"
    ),
    "compressor_degradation_vaccine_03": (
        "f86c57c710adfb15cd3ec62b486c58d231b5f0cd7fcab8f7f43f335d524f13c1"
    ),
    "compressor_degradation_vaccine_04": (
        "9ce69677919a99da6c832078b075e113a62fdc4f930f3df956e0ba0351189a4a"
    ),
    "compressor_wear_pharma_control_01": (
        "d735b465a5315e5b520ca7160d9b89558a209ec8133530fcc7e57d462ef5ce17"
    ),
    "compressor_wear_vaccine_control_01": (
        "0463e70ac84135c0d1d94708c5651333bde433206caa47d450b89e8cf31d2dcf"
    ),
    "door_open_frozen_01": ("285e2d8693e3d09428cd4392d324bcd49ec0e8ebad62571b59a2202667449779"),
    "door_open_frozen_02": ("ef76c8d325bd8b01cef6098bd7f3c3bd262b8f2c53bf0dec73c78fed22e7e5f9"),
    "door_open_pharma_01": ("981891a994db9f9d745a258dde9904932add351fc3ae0d9979eb6b68aec8a516"),
    "door_open_pharma_02": ("709a8858bf8d86525818b8f72c5c9121580524f6d3b6690aa4f83525ceeb1b53"),
    "door_open_pharma_03": ("4c37ccfe857e6bbc1b2328f692df44552cfcc0d8d00cb6c1e80197dbed810ae3"),
    "door_open_pharma_control_01": (
        "2dda260ee75e43b29c8a6e1c27e32360bafff3ff030cefe7ad5a98c05d9c83df"
    ),
    "door_open_produce_01": ("cf9d5c8663469595525731ba20b4ef927ece0e5abd48dc84123a28cfbef7f581"),
    "door_open_produce_02": ("5fa4fe2a128b566febbed4757b9b559766919bcdd0dc65acc9f821f400f653a6"),
    "door_open_vaccine_01": ("b688d50baea5f5651aca7c3b80738dfbff4c6db1542fea9b22690557c842b94e"),
    "door_open_vaccine_02": ("9365a9b1690f2e48d06b3338f71cc9283ac1ae1491d3c1e719123dc5cc76ac62"),
    "door_open_vaccine_03": ("fe9e485e69fda2516b4aa7d94ad9be1fb2bad17a416ae06c8eccbd1500efd185"),
    "door_open_vaccine_control_01": (
        "9fe7f49d8fa177fb51bf334056a2dc10c63d48f02dcdebb90ae5b9f145779ba8"
    ),
    "drifting_sensor_control_01": (
        "7f89f88d175712900164eafa3bfc9de7f373e8d9488acf74f5dd814c022604a2"
    ),
    "drifting_sensor_control_02": (
        "877eef1da1c8a123d6951c67bb97d2274afeb8abb3b3896ab986cf61ad47366b"
    ),
    "drifting_sensor_control_03": (
        "f893459695aac518e20ddc49e16d010607b8b03e923cd572ca8eee7c1fbd9d97"
    ),
    "drifting_sensor_real_breach_01": (
        "43443c3e43d796277dfe54c65f723cc8ec68e9826b3ab1965a3de02270048ec9"
    ),
    "drifting_sensor_real_breach_02": (
        "ce6cd271541034fc88ea831733b63a4380852048df8f56dc15ebe192e68f0ea1"
    ),
    "fuel_drain_pharma_control_01": (
        "ace829110dc07ff925bce4183e1da000888a832c929dfeef8006f1125f5f21b7"
    ),
    "fuel_drain_pharma_control_02": (
        "9e636f9b9cce560e9ca7b6ee27f261c7487f947092711621eea469cb1297df47"
    ),
    "fuel_drain_vaccine_control_01": (
        "39ae8fb5008ac024ef24e3e57fbc353a2cd0681f69d35e1928a564d17e90d894"
    ),
    "fuel_drain_vaccine_control_02": (
        "0addba4a697493f47ddd23ca5f1593a178b2719c81034c08994778b42f63d4a8"
    ),
    "fuel_exhaustion_frozen_01": (
        "d27ecd625add4b7f4c691b2d6abdfc42bff2126ae392cac2a61ab123a6030e16"
    ),
    "fuel_exhaustion_frozen_02": (
        "c921e6f83dbcafc66c56ea0abd05dd201bef1bbbf96107b19d78e1d1f8f86ca9"
    ),
    "fuel_exhaustion_pharma_01": (
        "cc27d74045186c0e4685339b37da952a98231b53afc6f69e362a1eb623f061ba"
    ),
    "fuel_exhaustion_pharma_02": (
        "b5d11961ee73fa91e7c09088a691b74454ff3965c699ffbabd0936cd81d3f985"
    ),
    "fuel_exhaustion_produce_01": (
        "5cf81e674bb712122f5d72d948cab6fd3edcc8c55061c5772e13a882891f5122"
    ),
    "fuel_exhaustion_produce_02": (
        "797408f3f15a29d31aaab83a2420adcf57ad1058a84598c5af8f00cc0f9fd7f8"
    ),
    "fuel_exhaustion_vaccine_01": (
        "9b893600e6076b09b183141101af75e9a2ebbce8bfaa75cb1d0b7c73aeb201bd"
    ),
    "fuel_exhaustion_vaccine_02": (
        "8427de070d0502edf972fa4f48b7c1191e6aa3a254ae7e0587ba5f0e548586eb"
    ),
    "normal_frozen_run_01": ("8882fe5f0520962597d33a8c85a14d61ff6fd3ad968fc5eb1ca2ca0e6672da05"),
    "normal_pharma_run_01": ("819347afb9e5a55858e8e5244b17f6cd4db0a38078dd76d145c00ed9fffc36b5"),
    "normal_produce_run_01": ("6664cfca937192b445444490043a1d1c4ed2dd399f32349fa9f19bd17b6b602e"),
    "sensor_drift_pharma_01": ("cfeab66fb408b14bf06c826335e4a9de5fc48f57990dfda1fd9efbfa097a85df"),
    "stuck_sensor_control_01": ("4dc30f4b6a8d692f6350a8212a46143ac5ebeaf05b88cf01063ab1f53ca03ad7"),
    "stuck_sensor_real_breach_01": (
        "d95732f007744375e402788e40f053bb72f85963f477466ec0bee3ae6179cc58"
    ),
    "stuck_sensor_real_breach_02": (
        "c5226bd2bbd8b3be96e5b9d0151616626d94468c1065f21798c1f39fd8a5645d"
    ),
    "stuck_sensor_real_breach_03": (
        "747d0f89404eb6f8edb6c9fa351cc48601336079403cee822fb11590bfd0d887"
    ),
}


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_DIR)


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", sorted(GOLDEN_DIGESTS))
def test_scenario_matches_its_golden_digest(pack, scenario_id: str):
    """If this fails, either the simulator changed or the machine disagrees.

    Either way, every benchmark result recorded before the change is no longer
    comparable with results recorded after it.
    """
    digest = compute_digest(run_scenario(pack.scenarios[scenario_id]))
    assert digest == GOLDEN_DIGESTS[scenario_id], (
        f"{scenario_id} digest changed. If this was intentional, update "
        "GOLDEN_DIGESTS and re-baseline any stored benchmark results."
    )


def test_every_scenario_in_the_pack_has_a_golden_digest(pack):
    """A new scenario without a digest would drift unnoticed."""
    assert set(pack.scenarios) == set(GOLDEN_DIGESTS)


def test_digest_covers_telemetry_only(pack):
    """Ground-truth bookkeeping must not affect the digest.

    Telemetry is the contract with the application. A change to internal
    diagnostics that leaves telemetry identical should not invalidate stored
    results.
    """
    result = run_scenario(pack.scenarios["normal_pharma_run_01"])
    mutated = type(result)(
        scenario_id=result.scenario_id,
        seed=result.seed,
        events=result.events,
        ground_truth=[],  # ground truth discarded entirely
        actual_breach_minute=result.actual_breach_minute,
        reported_breach_minute=result.reported_breach_minute,
        first_saturation_minute=result.first_saturation_minute,
    )
    assert compute_digest(mutated) == compute_digest(result)


# ---------------------------------------------------------------------------
# Written artifacts
# ---------------------------------------------------------------------------


def test_write_run_produces_three_separate_artifacts(pack, tmp_path: Path):
    result = run_scenario(pack.scenarios["compressor_degradation_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    assert artifacts.telemetry_path.is_file()
    assert artifacts.ground_truth_path.is_file()
    assert artifacts.manifest_path.is_file()
    # Separate files, so the application can be given one and not the other.
    assert artifacts.telemetry_path != artifacts.ground_truth_path


def test_written_telemetry_contains_no_ground_truth(pack, tmp_path: Path):
    """The boundary that keeps the risk model from training on the answer."""
    result = run_scenario(pack.scenarios["sensor_drift_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    columns = set(pq.read_table(artifacts.telemetry_path).column_names)
    forbidden = {
        "true_cargo_temp_c",
        "sensor_error_c",
        "compressor_health",
        "in_breach",
        "saturated",
        "duty_cycle",
        "cooling_output_w",
    }
    assert not (columns & forbidden)


def test_ground_truth_file_retains_the_true_values(pack, tmp_path: Path):
    result = run_scenario(pack.scenarios["sensor_drift_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    columns = set(pq.read_table(artifacts.ground_truth_path).column_names)
    assert {"true_cargo_temp_c", "sensor_error_c", "in_breach"} <= columns


def test_row_counts_match_the_run(pack, tmp_path: Path):
    scenario = pack.scenarios["normal_pharma_run_01"]
    artifacts = write_run(run_scenario(scenario), tmp_path, pack_version="1.0.0")

    assert pq.read_table(artifacts.telemetry_path).num_rows == scenario.duration_minutes
    assert pq.read_table(artifacts.ground_truth_path).num_rows == scenario.duration_minutes


def test_manifest_records_provenance_and_outcomes(pack, tmp_path: Path):
    result = run_scenario(pack.scenarios["compressor_degradation_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert manifest["scenario_id"] == "compressor_degradation_pharma_01"
    assert manifest["pack_version"] == "1.0.0"
    assert manifest["seed"] == result.seed
    assert manifest["telemetry_digest_sha256"] == artifacts.digest
    assert manifest["actual_breach_minute"] == result.actual_breach_minute


def test_manifest_names_the_application_visible_file(pack, tmp_path: Path):
    """Makes the data boundary explicit to anyone inspecting the output."""
    result = run_scenario(pack.scenarios["normal_pharma_run_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert manifest["application_may_read"] == [TELEMETRY_FILE]
    assert manifest["benchmark_only"] == [GROUND_TRUTH_FILE]


def test_rewriting_a_run_is_idempotent(pack, tmp_path: Path):
    """Runs are deterministic, so re-running rewrites identical content."""
    result = run_scenario(pack.scenarios["normal_pharma_run_01"])

    first = write_run(result, tmp_path, pack_version="1.0.0")
    first_bytes = first.telemetry_path.read_bytes()

    second = write_run(
        run_scenario(pack.scenarios["normal_pharma_run_01"]), tmp_path, pack_version="1.0.0"
    )
    assert second.telemetry_path.read_bytes() == first_bytes
    assert second.digest == first.digest
