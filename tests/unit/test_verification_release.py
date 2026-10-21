"""Frozen faults plus lifecycle boundaries; no live provider calls."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.app.actions.effects import EffectKind, ExpectedEffect
from backend.app.db.app.models import Incident, OutcomeVerification
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.verification.service import VerificationService
from scripts import verification_release
from scripts.verification_release import measure


def test_frozen_fault_pack_and_explicit_costs() -> None:
    report = measure()
    assert (
        report["pack_sha256"] == "6eb4c2fbbb0db0ae8479a1eea3127f6f2486a6f48fe5e335c70f795f4ae88814"
    )
    assert report["cases"] == report["expected_verdict_matches"] == 24
    assert report["false_confirmations"] == 2  # nonconstant lies remain unidentifiable
    assert report["missed_recoveries"] == 4  # constant healthy and stale healthy
    assert report["abstentions"] == 12
    assert all(r["verdict"] != "confirmed" for r in report["rows"] if r["group"] == "target_fault")
    assert all(r["verdict"] == r["truth"] for r in report["rows"] if r["group"] == "honest_control")


def test_checkout_line_endings_do_not_change_frozen_inputs(tmp_path, monkeypatch) -> None:
    content = verification_release.PACK.read_bytes().replace(b"\r\n", b"\n")
    copy = tmp_path / "pack.json"
    monkeypatch.setattr(verification_release, "PACK", copy)
    copy.write_bytes(content)
    linux = measure()
    copy.write_bytes(content.replace(b"\n", b"\r\n"))
    assert measure() == linux


@pytest.mark.asyncio
async def test_early_request_does_not_consume_verdict_or_audit() -> None:
    start = datetime(2026, 10, 21, tzinfo=UTC)
    incident = Incident(id=uuid4(), status="verifying")
    verification = OutcomeVerification(
        id=uuid4(),
        incident_id=incident.id,
        execution_id=uuid4(),
        expected_effect=ExpectedEffect(EffectKind.TEMPERATURE_WITHIN_ENVELOPE, 90).as_payload(),
        window_start=start,
        window_end=start + timedelta(minutes=90),
    )
    audit, lifecycle = AsyncMock(), AsyncMock()
    service = VerificationService(AsyncMock(), audit, lifecycle)
    with pytest.raises(ValueError, match=r"window.*closed"):
        await service.verify(
            verification,
            incident=incident,
            evidence=[],
            envelope=TemperatureEnvelope(2, 8),
            at=start + timedelta(minutes=89, seconds=59),
        )
    assert verification.verdict is None
    assert incident.status == "verifying"
    audit.append.assert_not_awaited()
    lifecycle.transition.assert_not_awaited()
