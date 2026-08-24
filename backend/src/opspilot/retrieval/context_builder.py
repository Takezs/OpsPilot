"""Context assembly with a token budget and stable citation ids.

Each fragment carries the metadata needed to locate its evidence at generation
time (title, document version, section path, ``effective_at`` and page). The
stable citation id ``[DOC:<document_id>#<chunk_id>]`` is the only reference a
model is allowed to cite, so the same chunk always maps to the same id.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

# Per-fragment overhead for the citation header rendered in the prompt.
HEADER_TOKEN_OVERHEAD = 16


def citation_id(document_id: str, chunk_id: str) -> str:
    """Return the stable reference id embedded in prompts and citations."""
    return f"[DOC:{document_id}#{chunk_id}]"


@dataclass(frozen=True)
class ContextFragment:
    document_id: str
    chunk_id: str
    title: str
    document_version: int
    section_path: tuple[str, ...]
    effective_at: datetime
    page: int | None
    content: str
    token_count: int

    @property
    def citation_id(self) -> str:
        return citation_id(self.document_id, self.chunk_id)


@dataclass(frozen=True)
class BuiltContext:
    fragments: tuple[ContextFragment, ...]
    token_budget: int
    total_tokens: int
    truncated: bool

    @property
    def citation_ids(self) -> tuple[str, ...]:
        return tuple(item.citation_id for item in self.fragments)


def build_context(fragments: Sequence[ContextFragment], token_budget: int) -> BuiltContext:
    """Greedily include fragments in input order without exceeding the budget.

    Each fragment charges its content token count plus ``HEADER_TOKEN_OVERHEAD``
    for the citation block. Fragments that would overflow the budget are
    skipped. The input (reranked) ordering is preserved, so citation ids are
    deterministic across runs.
    """
    included: list[ContextFragment] = []
    running = 0
    total_possible = 0
    for item in fragments:
        cost = item.token_count + HEADER_TOKEN_OVERHEAD
        total_possible += cost
        if running + cost > token_budget:
            continue
        included.append(item)
        running += cost
    return BuiltContext(
        fragments=tuple(included),
        token_budget=token_budget,
        total_tokens=running,
        truncated=running < total_possible,
    )


@dataclass(frozen=True)
class CitationSnapshot:
    """Immutable location of generation-time evidence for a citation."""

    document_id: str
    document_version: int
    chunk_id: str
    section_path: tuple[str, ...]
    page: int | None


def snapshot_for(item: ContextFragment) -> CitationSnapshot:
    return CitationSnapshot(
        document_id=item.document_id,
        document_version=item.document_version,
        chunk_id=item.chunk_id,
        section_path=item.section_path,
        page=item.page,
    )
