"""Replay the frozen release faults without a database, network or model."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.app.actions.effects import EffectKind, ExpectedEffect
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.verification.outcome import evaluate_effect

PACK = Path(__file__).resolve().parents[1] / "data/scenarios/verification_release_v1.json"


def measure() -> dict[str, Any]:
    """Keep truth/expected labels outside the application grading arguments."""
    # Git normalizes checkout line endings. Bind identity to UTF-8/LF bytes.
    content = PACK.read_bytes().replace(b"\r\n", b"\n")
    pack = json.loads(content)
    start = datetime(2026, 10, 21, tzinfo=UTC)
    end = start + timedelta(minutes=pack["window_minutes"])
    rows = []
    for case in pack["cases"]:
        result = evaluate_effect(
            ExpectedEffect(EffectKind(case["kind"]), pack["window_minutes"]),
            readings=[(start + timedelta(minutes=t), v) for t, v in case["readings"]],
            envelope=TemperatureEnvelope(*pack["envelope"]),
            window_start=start,
            window_end=end,
        )
        rows.append({k: case[k] for k in ("id", "truth", "expected", "group")})
        rows[-1].update(
            verdict=result.verdict.value, detail=result.detail, observed=result.observed
        )
    failures = [r for r in rows if r["truth"] == "failed"]
    successes = [r for r in rows if r["truth"] == "confirmed"]
    false_confirmations = sum(r["verdict"] == "confirmed" for r in failures)
    missed_recoveries = sum(r["verdict"] != "confirmed" for r in successes)
    abstentions = sum(r["verdict"] == "inconclusive" for r in rows)
    return {
        "pack_sha256": hashlib.sha256(content).hexdigest(),
        "pack_hash_method": "utf8_lf_bytes_sha256",
        "cases": len(rows),
        "true_failures": len(failures),
        "true_successes": len(successes),
        "false_confirmations": false_confirmations,
        "false_confirmation_rate": false_confirmations / len(failures),
        "missed_recoveries": missed_recoveries,
        "missed_recovery_rate": missed_recoveries / len(successes),
        "abstentions": abstentions,
        "abstention_rate": abstentions / len(rows),
        "expected_verdict_matches": sum(r["verdict"] == r["expected"] for r in rows),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(measure(), indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
