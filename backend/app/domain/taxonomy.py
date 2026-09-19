"""Loader and accessor for the canonical observation taxonomy.

The taxonomy is the reason contradiction detection in this system is a
deterministic value comparison rather than a judgement handed to a language
model. It declares, per observation type, the value shape, the tolerance
beyond which two sources are considered to disagree, how quickly the reading
goes stale, the plausible physical range, and how much each source is trusted.

Everything here is pure: no I/O beyond reading the YAML file once, and no
dependency on any other layer of the application.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.domain.enums import EvidenceSource, FreshnessState, ObservationKind

__all__ = [
    "DEFAULT_SOURCE_RELIABILITY",
    "EXACT",
    "ObservationSpec",
    "Taxonomy",
    "UnknownObservationTypeError",
    "load_taxonomy",
]

# Sources not named in an observation's reliability map are trusted at this
# level. Deliberately low: an unlisted source is an unvetted one.
DEFAULT_SOURCE_RELIABILITY = 0.5

#: Sentinel tolerance for non-numeric types, where any difference is a conflict.
EXACT = "exact"

ToleranceT = float | Literal["exact"]


class UnknownObservationTypeError(KeyError):
    """Raised when an observation type is not declared in the taxonomy.

    This is intentionally fatal rather than a silent default. An undeclared
    observation type means some adapter is inventing vocabulary, which would
    quietly disable conflict detection for that value.
    """

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        super().__init__(
            f"Unknown observation type {name!r}. Declared types: {', '.join(sorted(known))}"
        )


class FreshnessPenalty(BaseModel):
    """Confidence multipliers applied as an observation ages past its TTL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fresh: float = Field(ge=0.0, le=1.0)
    aging: float = Field(ge=0.0, le=1.0)
    stale: float = Field(ge=0.0, le=1.0)
    aging_threshold_multiplier: float = Field(gt=0.0)
    stale_threshold_multiplier: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        if not self.fresh >= self.aging >= self.stale:
            raise ValueError(
                "freshness penalties must be non-increasing: "
                f"fresh={self.fresh} aging={self.aging} stale={self.stale}"
            )
        if self.stale_threshold_multiplier <= self.aging_threshold_multiplier:
            raise ValueError("stale_threshold_multiplier must exceed aging_threshold_multiplier")
        return self


class ObservationSpec(BaseModel):
    """Declared rules for a single observation type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: ObservationKind
    unit: str | None = None
    safety_critical: bool = False
    conflict_tolerance: ToleranceT
    ttl_seconds: int | None = Field(default=None, ge=0)
    plausible_range: tuple[float, float] | None = None
    allowed_values: list[str] | None = None
    source_reliability: dict[EvidenceSource, float] = Field(default_factory=dict)
    authority_order: list[EvidenceSource] = Field(default_factory=list)
    description: str | None = None

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> Self:
        if self.kind is ObservationKind.NUMERIC:
            if self.plausible_range is None:
                raise ValueError(
                    f"{self.name}: numeric observations require a plausible_range; "
                    "without one, an absurd reading becomes evidence"
                )
            if isinstance(self.conflict_tolerance, str):
                raise ValueError(
                    f"{self.name}: numeric observations need a numeric "
                    f"conflict_tolerance, got {self.conflict_tolerance!r}"
                )
            if self.conflict_tolerance < 0:
                raise ValueError(f"{self.name}: conflict_tolerance must be >= 0")
        else:
            if self.conflict_tolerance != EXACT:
                raise ValueError(
                    f"{self.name}: non-numeric observations must use conflict_tolerance: {EXACT!r}"
                )
            if self.plausible_range is not None:
                raise ValueError(f"{self.name}: plausible_range applies to numeric types only")

        if self.kind is ObservationKind.CATEGORICAL and not self.allowed_values:
            raise ValueError(f"{self.name}: categorical observations require allowed_values")

        if self.plausible_range is not None:
            low, high = self.plausible_range
            if low >= high:
                raise ValueError(
                    f"{self.name}: plausible_range must be increasing, got {low}..{high}"
                )

        for source, reliability in self.source_reliability.items():
            if not 0.0 <= reliability <= 1.0:
                raise ValueError(
                    f"{self.name}: source_reliability[{source}]={reliability} outside [0, 1]"
                )

        unknown_authority = [s for s in self.authority_order if s not in self.source_reliability]
        if unknown_authority:
            raise ValueError(
                f"{self.name}: authority_order names sources with no declared "
                f"reliability: {unknown_authority}"
            )

        return self

    @property
    def ages(self) -> bool:
        """Whether this observation type loses value over time.

        A contractual temperature range does not; a sensor reading does.
        """
        return self.ttl_seconds is not None

    def is_plausible(self, value: float) -> bool:
        """Whether a numeric reading falls inside the declared physical range.

        Implausible values are rejected at ingestion rather than stored with a
        low confidence, so a misread gauge never becomes evidence at all.
        """
        if self.plausible_range is None:
            return True
        low, high = self.plausible_range
        return low <= value <= high


class Taxonomy(BaseModel):
    """The whole declared vocabulary, loaded once and treated as immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    freshness_penalty: FreshnessPenalty
    stale_evidence_confidence_cap: float = Field(ge=0.0, le=1.0)
    observations: dict[str, ObservationSpec]

    # -- lookup ------------------------------------------------------------

    def spec(self, observation_type: str) -> ObservationSpec:
        try:
            return self.observations[observation_type]
        except KeyError:
            raise UnknownObservationTypeError(observation_type, list(self.observations)) from None

    def reliability(self, observation_type: str, source: EvidenceSource) -> float:
        spec = self.spec(observation_type)
        return spec.source_reliability.get(source, DEFAULT_SOURCE_RELIABILITY)

    # -- freshness ---------------------------------------------------------

    def freshness(self, observation_type: str, age_seconds: float) -> FreshnessState:
        """Classify an observation's age against its declared TTL."""
        spec = self.spec(observation_type)
        if spec.ttl_seconds is None:
            return FreshnessState.NOT_APPLICABLE
        if age_seconds < 0:
            # A reading from the future is a clock-skew bug, not a fresh value.
            raise ValueError(
                f"{observation_type}: negative age {age_seconds}s (observed_at is in the future)"
            )

        penalty = self.freshness_penalty
        if age_seconds <= spec.ttl_seconds * penalty.aging_threshold_multiplier:
            return FreshnessState.FRESH
        if age_seconds <= spec.ttl_seconds * penalty.stale_threshold_multiplier:
            return FreshnessState.AGING
        return FreshnessState.STALE

    def freshness_multiplier(self, state: FreshnessState) -> float:
        penalty = self.freshness_penalty
        match state:
            case FreshnessState.FRESH | FreshnessState.NOT_APPLICABLE:
                return penalty.fresh
            case FreshnessState.AGING:
                return penalty.aging
            case FreshnessState.STALE:
                return penalty.stale

    # -- confidence --------------------------------------------------------

    def confidence(
        self,
        observation_type: str,
        source: EvidenceSource,
        age_seconds: float,
        extraction_confidence: float = 1.0,
    ) -> float:
        """Compute an evidence confidence score.

        ``confidence = source_reliability x extraction_confidence x freshness``

        This is the only place a confidence number is produced. No model output
        contributes to it except ``extraction_confidence``, which is a per-field
        score reported by an extraction model and bounded to [0, 1] here.
        """
        if not 0.0 <= extraction_confidence <= 1.0:
            raise ValueError(
                f"extraction_confidence must be in [0, 1], got {extraction_confidence}"
            )
        state = self.freshness(observation_type, age_seconds)
        return (
            self.reliability(observation_type, source)
            * extraction_confidence
            * self.freshness_multiplier(state)
        )

    # -- conflict ----------------------------------------------------------

    def conflicts(
        self,
        observation_type: str,
        left: object,
        right: object,
    ) -> bool:
        """Whether two values of the same observation type disagree.

        Numeric types compare against the declared tolerance; every other kind
        compares exactly. Set-valued types compare as unordered sets so that
        ``["AL17", "AL02"]`` and ``["AL02", "AL17"]`` agree.
        """
        spec = self.spec(observation_type)

        if spec.kind is ObservationKind.NUMERIC:
            if not isinstance(left, int | float) or not isinstance(right, int | float):
                raise TypeError(
                    f"{observation_type} is numeric but received "
                    f"{type(left).__name__} and {type(right).__name__}"
                )
            tolerance = spec.conflict_tolerance
            assert not isinstance(tolerance, str)  # guaranteed by the validator
            return abs(float(left) - float(right)) > tolerance

        if spec.kind is ObservationKind.SET:
            if not isinstance(left, list | set | tuple) or not isinstance(
                right, list | set | tuple
            ):
                raise TypeError(
                    f"{observation_type} is set-valued but received "
                    f"{type(left).__name__} and {type(right).__name__}"
                )
            return set(left) != set(right)

        return left != right

    def authoritative_source(
        self, observation_type: str, sources: list[EvidenceSource]
    ) -> EvidenceSource | None:
        """Pick which source wins when several disagree.

        Returns ``None`` when the type declares no authority order, in which
        case the conflict is surfaced without the system silently choosing a
        side. Note that authority decides which value is *used*; the conflict
        itself always stays visible to the dispatcher.
        """
        spec = self.spec(observation_type)
        for candidate in spec.authority_order:
            if candidate in sources:
                return candidate
        return None


def _default_taxonomy_path() -> Path:
    """Locate ``data/taxonomy/observations.yaml``.

    ``AXON_TAXONOMY_PATH`` overrides the default, which tests use to load
    deliberately malformed fixtures.
    """
    override = os.environ.get("AXON_TAXONOMY_PATH")
    if override:
        return Path(override)
    # backend/app/domain/taxonomy.py -> backend/app/domain -> app -> backend -> root
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "data" / "taxonomy" / "observations.yaml"


def _parse(raw: object, source: Path) -> Taxonomy:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: taxonomy must be a mapping, got {type(raw).__name__}")

    observations_raw = raw.get("observations")
    if not isinstance(observations_raw, dict) or not observations_raw:
        raise ValueError(f"{source}: taxonomy declares no observations")

    # The name is the mapping key in YAML; inject it so each spec is
    # self-describing in error messages.
    observations = {
        name: ObservationSpec.model_validate({"name": name, **(body or {})})
        for name, body in observations_raw.items()
    }
    return Taxonomy.model_validate({**raw, "observations": observations})


@lru_cache(maxsize=4)
def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load and validate the taxonomy, cached per path.

    Validation failures are raised eagerly at import/startup rather than when a
    particular observation type is first used, so a malformed taxonomy cannot
    silently degrade conflict detection at runtime.
    """
    resolved = path or _default_taxonomy_path()
    if not resolved.is_file():
        raise FileNotFoundError(f"Observation taxonomy not found at {resolved}")
    return _parse(yaml.safe_load(resolved.read_text(encoding="utf-8")), resolved)
