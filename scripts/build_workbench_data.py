"""Build a deterministic public workbench from already published simulation evidence.

No database, model call or new historical measurement. --check is read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backend.app.decision.catalogue import ACTION_COSTS, Feasibility
from backend.app.decision.engine import DELAY_COST_PER_MINUTE_USD, rank_options

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "trace": "site/api/v1/control/trace",
    "compressor": "site/api/v1/control/telemetry/compressor_degradation_pharma_01",
    "sensor": "site/api/v1/control/telemetry/sensor_drift_pharma_01",
    "verification": "site/ui/verification-release.json",
}


def normalized(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")


def render() -> dict[str, bytes]:
    data = {key: json.loads(normalized(ROOT / name)) for key, name in SOURCES.items()}
    for key in ("compressor", "sensor"):
        if any("true_cargo_temp_c" in point for point in data[key]["points"]):
            raise ValueError("public telemetry cannot expose simulator truth")
    policy = [{"id": action.value, **asdict(cost)} for action, cost in ACTION_COSTS.items()]
    receipts = [
        {"path": name, "sha256": hashlib.sha256(normalized(ROOT / name)).hexdigest()}
        for name in [
            *SOURCES.values(),
            "backend/app/decision/catalogue.py",
            "backend/app/decision/engine.py",
        ]
    ]
    payload: dict[str, Any] = {
        "schema": 1,
        "scope": "recorded synthetic logistics study; not a running fleet",
        "recorded_at": data["trace"]["recorded_at"],
        "sources": receipts,
        "trace": data["trace"],
        "telemetry": {"compressor": data["compressor"], "sensor": data["sensor"]},
        "verification": data["verification"],
        "planning_policy": policy,
        "delay_cost_per_minute_usd": DELAY_COST_PER_MINUTE_USD,
        "planning_scope": "illustrative cost assumptions; not calibrated probabilities or savings",
    }
    parity = []
    for probability in [0, 0.01, 0.25, 0.5, 0.9, 1]:
        for value in [0, 10000, 184000, 1000000]:
            for blocked in [[], ["reroute_to_cold_storage", "trailer_swap"]]:
                feasibility = {
                    action: Feasibility.no("practice unavailable")
                    for action in ACTION_COSTS
                    if action.value in blocked
                }
                result = rank_options(
                    probability=probability, cargo_value_usd=value, feasibility=feasibility
                )
                parity.append(
                    {
                        "probability": probability,
                        "value": value,
                        "blocked": blocked,
                        "options": [
                            {
                                "id": o.action.value,
                                "ev": o.expected_value_usd,
                                "feasible": o.feasible,
                            }
                            for o in result.options
                        ],
                    }
                )
    encoded = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
    manifest = {
        "schema": 1,
        "algorithm": "sha256-normalized-utf8-lf",
        "payload": "workbench-data.json",
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "scope": "Byte consistency, not an authenticity signature or field-performance claim.",
    }
    return {
        "workbench-data.json": encoded,
        "workbench-manifest.json": (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        "workbench-parity.json": (json.dumps(parity, sort_keys=True, indent=2) + "\n").encode(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, content in render().items():
        for parent in [ROOT / "apps/control_tower", ROOT / "site/ui"]:
            target = parent / name
            if args.check:
                if not target.is_file() or normalized(target) != content:
                    raise SystemExit(
                        "Workbench evidence is stale: " + str(target.relative_to(ROOT))
                    )
            else:
                target.write_bytes(content)


if __name__ == "__main__":
    main()
