"""Repositories for the application database.

One class per aggregate, each taking an ``AsyncSession`` and owning no
transaction of its own. Committing is the caller's job, because a single unit
of work routinely spans several repositories - persisting evidence, opening an
incident, and appending the audit event that records both must commit together
or not at all.
"""

from backend.app.db.app.repositories.audit import AuditRepository
from backend.app.db.app.repositories.evidence import EvidenceRepository
from backend.app.db.app.repositories.incident import IncidentRepository
from backend.app.db.app.repositories.risk import RiskRepository

__all__ = [
    "AuditRepository",
    "EvidenceRepository",
    "IncidentRepository",
    "RiskRepository",
]
