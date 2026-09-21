"""Deciding which temperature envelope a shipment is judged against.

Pure, and in the evidence layer rather than beside the replay that first
needed it. That is not tidiness: ``incidents.replay`` imports the legacy
repository, which imports ``pyodbc``, so anything importing this function from
there dragged an ODBC driver behind it. The agent workflow needs the same
function, and an import-linter contract refused - correctly. A pure resolution
over evidence has no business being reachable only through a module that opens
database connections.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy
from backend.app.evidence.reconciliation import resolve_value

__all__ = ["resolve_envelope"]


def resolve_envelope(
    evidence: Sequence[Evidence],
    *,
    taxonomy: Taxonomy | None = None,
) -> TemperatureEnvelope | None:
    """Decide which temperature envelope applies, from the evidence itself.

    Returns ``None`` when no envelope is known. That is not an error and must
    not be defaulted: judging cargo against a guessed envelope would be
    fabricating the one number the whole judgement rests on. The caller is
    expected to degrade - say it cannot assess this shipment - rather than
    proceed on an invented limit.
    """
    tax = taxonomy or load_taxonomy()
    pool = list(evidence)
    minimum = resolve_value(pool, "permitted_temp_min_c", taxonomy=tax)
    maximum = resolve_value(pool, "permitted_temp_max_c", taxonomy=tax)
    if minimum is None or maximum is None:
        return None
    return TemperatureEnvelope(
        minimum_c=float(minimum.value),  # type: ignore[arg-type]
        maximum_c=float(maximum.value),  # type: ignore[arg-type]
        source_evidence_ids=(str(minimum.id), str(maximum.id)),
    )
