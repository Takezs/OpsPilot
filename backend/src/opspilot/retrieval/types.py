"""Shared types and database-side scope predicate for hybrid retrieval."""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import and_, true
from sqlalchemy.sql.elements import ColumnElement

from opspilot.knowledge.models import KnowledgeBase
from opspilot.knowledge.schemas import KnowledgeScope


class RetrievalSource(StrEnum):
    DENSE = "dense"
    FTS = "fts"
    RRF = "rrf"


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk_id: str
    score: float
    rank: int
    source: RetrievalSource


@dataclass(frozen=True)
class RetrievalResult:
    dense: list[RetrievalCandidate]
    fts: list[RetrievalCandidate]
    rrf: list[RetrievalCandidate]


def scope_conditions(scope: KnowledgeScope) -> ColumnElement[bool]:
    """Build the predicate enforcing ``KnowledgeScope`` in candidate SQL.

    Both retrieval legs must apply this predicate in their database query so
    that unauthorized chunks are excluded before they ever reach Python. The
    semantics mirror ``KnowledgeScope.allows``: a ``"*"`` department matches
    every department, and the access level must not exceed the ceiling.
    """
    if "*" in scope.departments:
        department_match: ColumnElement[bool] = true()
    else:
        department_match = KnowledgeBase.department.in_(scope.departments)
    return and_(
        department_match,
        KnowledgeBase.access_level <= int(scope.max_access_level),
    )
