"""The contractual temperature envelope a shipment must stay within.

In `domain` rather than beside the detector, because it is not a detection
concept: the risk features are built against it, the decision engine prices
breaching it, and the narrative cites it. Leaving it in `incidents.detection`
made `risk` and `incidents` import each other, which Python reports as a
circular import and which is really a sign the type was in the wrong layer.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TemperatureEnvelope"]


@dataclass(frozen=True, slots=True)
class TemperatureEnvelope:
    """The limits a shipment must stay within.

    Resolved from evidence rather than passed around as configuration,
    because which envelope applies is itself contested: the ERP and the signed
    shipping document disagree for the flagship shipment, and the answer has
    to come from reconciliation rather than from whichever source was read
    last.
    """

    minimum_c: float
    maximum_c: float
    #: Which evidence this came from, so a detection can cite the envelope it
    #: was judged against and not merely the reading that breached it.
    source_evidence_ids: tuple[str, ...] = ()

    def breaches(self, temperature: float) -> bool:
        """Whether a reading is outside the envelope.

        Inclusive at both limits: a contractual maximum of 8 C means 8 C is
        permitted. Off by one in the exclusive direction would raise an
        incident for cargo that is exactly in spec, and our alarm would
        disagree with the customer's on the boundary case most likely to come
        up in a dispute.
        """
        return temperature < self.minimum_c or temperature > self.maximum_c
