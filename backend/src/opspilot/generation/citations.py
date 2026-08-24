"""Citation validation against the built context.

Validation is fail-closed: a factual answer that cites any reference absent
from the context (un-retrieved, outdated or nonexistent) must become
``insufficient_evidence`` in full, never silently keeping the valid references
alongside a confident answer. Valid citations carry a snapshot that locates the
generation-time document version, section and page.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from opspilot.generation.provider import GroundedAnswer
from opspilot.retrieval.context_builder import (
    BuiltContext,
    CitationSnapshot,
    ContextFragment,
    snapshot_for,
)


@dataclass(frozen=True)
class ValidatedAnswer:
    answer: str
    citations: tuple[str, ...]
    snapshots: tuple[CitationSnapshot, ...]
    insufficient_evidence: bool
    follow_up_question: str | None


def validate_citations(answer: GroundedAnswer, context: BuiltContext) -> ValidatedAnswer:
    by_id = {item.citation_id: item for item in context.fragments}

    if answer.follow_up_question is not None:
        valid = [citation for citation in answer.citations if citation in by_id]
        return _build(answer, valid, by_id, insufficient_evidence=False)

    if answer.insufficient_evidence or not answer.citations:
        return _insufficient(answer)

    valid = [citation for citation in answer.citations if citation in by_id]
    if len(valid) != len(answer.citations):
        # Fail-closed: any citation absent from the context degrades the whole
        # factual answer instead of silently keeping the valid references.
        return _insufficient(answer)

    return _build(answer, valid, by_id, insufficient_evidence=False)


def _insufficient(answer: GroundedAnswer) -> ValidatedAnswer:
    return ValidatedAnswer(
        answer=answer.answer,
        citations=(),
        snapshots=(),
        insufficient_evidence=True,
        follow_up_question=None,
    )


def _build(
    answer: GroundedAnswer,
    valid: list[str],
    by_id: Mapping[str, ContextFragment],
    *,
    insufficient_evidence: bool,
) -> ValidatedAnswer:
    return ValidatedAnswer(
        answer=answer.answer,
        citations=tuple(valid),
        snapshots=tuple(snapshot_for(by_id[citation]) for citation in valid),
        insufficient_evidence=insufficient_evidence,
        follow_up_question=answer.follow_up_question,
    )
