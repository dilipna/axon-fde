"""What an approval is bound to, and what breaks the binding.

The binding is the mechanism behind invariant I5. These tests exist to prove
it covers what an approver actually saw - not a convenient subset of it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.approvals.binding import BINDING_VERSION, ApprovalContext, compute_bound_hash
from backend.app.domain.enums import ActionType

INCIDENT = uuid4()


def context(**overrides: object) -> ApprovalContext:
    """A realistic flagship-shipment context, with one field swapped out."""
    base: dict[str, object] = {
        "incident_id": INCIDENT,
        "action": ActionType.REROUTE_TO_COLD_STORAGE,
        "target_ref": "FAC-DAYTON-01",
        "evidence_hashes": ["aa" * 32, "bb" * 32, "cc" * 32],
        "risk_probability": 0.78,
        "baseline_probability": 0.41,
        "baseline_name": "rule_prior",
        "model_version": "slope_extrapolation@1",
        "horizon_minutes": 60,
        "degraded": False,
    }
    base.update(overrides)
    return ApprovalContext(**base)  # type: ignore[arg-type]


class TestWhatTheHashCovers:
    """Every field that could change a human's answer must break the hash."""

    def test_a_forty_percent_decision_cannot_be_executed_against_an_eighty_five_percent_world(
        self,
    ) -> None:
        """The failure the binding exists to prevent, named as such.

        Omitting the risk probability from the binding is the easy mistake:
        the evidence hashes are obviously in scope, and the probability feels
        like something derived from them rather than something separate. It is
        not. A recalibration, a model version bump or a changed horizon moves
        the number without touching a single observation, and an approval
        bound only to evidence would survive all three.
        """
        approved = context(risk_probability=0.40)
        reality = context(risk_probability=0.85)

        assert approved.bound_hash() != reality.bound_hash()
        assert reality.differences_from(approved) == ("risk.probability",)

    def test_the_baseline_is_bound_as_well_as_the_prediction(self) -> None:
        """A prediction that agrees with its baseline is a different thing to approve.

        Same headline number, different story: at 0.78 predicted against a
        0.41 baseline the model is claiming to see something the rule cannot,
        and an approver who was shown that divergence approved the divergence.
        Binding only the headline would let the baseline move underneath it.
        """
        assert context(baseline_probability=0.41).bound_hash() != (
            context(baseline_probability=0.77).bound_hash()
        )

    def test_a_fallback_to_the_rule_baseline_breaks_the_binding(self) -> None:
        """Approving a model's number is not approving a rule's guess.

        `degraded` can flip without any number changing at all - the primary
        estimator becomes unavailable and the baseline is served in its place
        at coincidentally the same value. The decision is different even when
        the arithmetic is not.
        """
        assert context(degraded=False).bound_hash() != context(degraded=True).bound_hash()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("action", ActionType.TRAILER_SWAP),
            ("target_ref", "FAC-COLUMBUS-02"),
            ("evidence_hashes", ["aa" * 32, "bb" * 32]),
            ("model_version", "slope_extrapolation@2"),
            ("baseline_name", "static_prior"),
            ("horizon_minutes", 120),
            ("incident_id", uuid4()),
        ],
    )
    def test_every_covered_field_changes_the_hash(self, field: str, value: object) -> None:
        assert context(**{field: value}).bound_hash() != context().bound_hash()

    def test_approving_a_reroute_to_dayton_does_not_approve_one_to_columbus(self) -> None:
        """Stated separately from the parametrised case because it is the point.

        The target is the difference between an approved action and a
        different action with the same name.
        """
        dayton = context(target_ref="FAC-DAYTON-01")
        columbus = context(target_ref="FAC-COLUMBUS-02")
        assert dayton.bound_hash() != columbus.bound_hash()
        assert columbus.differences_from(dayton) == ("target_ref",)


class TestStability:
    """The binding must not break for reasons that are not changes."""

    def test_the_same_world_hashes_the_same_way_twice(self) -> None:
        assert context().bound_hash() == context().bound_hash()

    def test_evidence_read_in_a_different_order_binds_identically(self) -> None:
        """Otherwise every approval would be stale as soon as a query plan changed.

        Evidence arrives from a repository whose ordering is not guaranteed
        beyond what the query asks for. A binding sensitive to that would fail
        intermittently and look like tampering.
        """
        forward = context(evidence_hashes=["aa" * 32, "bb" * 32, "cc" * 32])
        reversed_ = context(evidence_hashes=["cc" * 32, "bb" * 32, "aa" * 32])
        assert forward.bound_hash() == reversed_.bound_hash()

    def test_a_probability_that_round_trips_through_json_still_matches(self) -> None:
        """The binding survives storage and retrieval.

        A float64 read back from Postgres is the same float64, and Python's
        repr of a float is one-to-one, so the canonical form is byte-identical
        either side of a round trip. If this ever failed, every approval would
        be stale the moment it was reloaded, and the staleness check would be
        indistinguishable from a broken one.
        """
        import json

        payload = json.loads(json.dumps(context().canonical()))
        assert payload == context().canonical()


class TestWhatTheBindingRefuses:
    def test_an_approval_cannot_be_bound_to_no_evidence(self) -> None:
        """A binding over an empty set survives every change to the world.

        It would pass review - there *is* a hash, and it *is* checked - while
        protecting nothing at all.
        """
        with pytest.raises(ValueError, match="at least one piece of evidence"):
            context(evidence_hashes=[])

    @pytest.mark.parametrize("probability", [-0.01, 1.01])
    def test_an_impossible_probability_is_refused(self, probability: float) -> None:
        with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
            context(risk_probability=probability)


def test_the_binding_version_is_part_of_the_hash() -> None:
    """Widening what is covered must invalidate every outstanding approval.

    Without the version in the hash, an approval granted under a narrower
    definition of "the world" would keep matching after the definition was
    widened - bound to less than the new rule requires, and silently so.
    """
    canonical = context().canonical()
    assert canonical["binding_version"] == BINDING_VERSION

    widened = dict(canonical)
    widened["binding_version"] = BINDING_VERSION + 1
    import hashlib
    import json

    bumped = hashlib.sha256(
        json.dumps(widened, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert bumped != compute_bound_hash(context())
