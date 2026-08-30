"""Unit tests for citation validation against the built context.

A factual answer with no valid citation must become ``insufficient_evidence``
instead of silently dropping the invalid references. Valid citations keep a
snapshot that locates the generation-time document version, section and page.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from opspilot.generation.citations import ValidatedAnswer, validate_citations
from opspilot.generation.provider import (
    DeterministicGenerationProvider,
    GroundedAnswer,
)
from opspilot.generation.service import GenerationService
from opspilot.retrieval.context_builder import (
    BuiltContext,
    ContextFragment,
    build_context,
    citation_id,
)


def fragment(chunk_id: str, document_id: str, *, version: int = 1) -> ContextFragment:
    return ContextFragment(
        document_id=document_id,
        chunk_id=chunk_id,
        title="Policy Manual",
        document_version=version,
        section_path=("compliance", "refund"),
        effective_at=datetime(2026, 8, 24, tzinfo=UTC),
        page=7,
        content=f"content-{chunk_id}",
        token_count=30,
    )


def context_for(*fragments: ContextFragment) -> BuiltContext:
    return build_context(list(fragments), token_budget=500)


def grounded(
    *,
    answer: str = "The refund window is 30 days.",
    citations: list[str] | None = None,
    insufficient_evidence: bool = False,
    follow_up_question: str | None = None,
) -> GroundedAnswer:
    return GroundedAnswer(
        answer=answer,
        citations=citations or [],
        insufficient_evidence=insufficient_evidence,
        follow_up_question=follow_up_question,
    )


def test_valid_citations_are_kept_with_snapshot() -> None:
    doc_id = str(uuid.uuid4())
    target = citation_id(doc_id, "chunk-1")
    ctx = context_for(fragment("chunk-1", doc_id, version=3))
    answer = grounded(citations=[target])

    validated = validate_citations(answer, ctx)

    assert validated.insufficient_evidence is False
    assert validated.citations == (target,)
    snapshot = validated.snapshots[0]
    assert snapshot.document_id == doc_id
    assert snapshot.document_version == 3
    assert snapshot.chunk_id == "chunk-1"
    assert snapshot.section_path == ("compliance", "refund")
    assert snapshot.page == 7


def test_not_retrieved_citation_marks_factual_answer_insufficient() -> None:
    doc_id = str(uuid.uuid4())
    # The answer cites a chunk that was never part of the built context.
    ctx = context_for(fragment("chunk-1", doc_id))
    answer = grounded(citations=[citation_id(doc_id, "chunk-999")])

    validated = validate_citations(answer, ctx)

    assert validated.insufficient_evidence is True
    assert validated.citations == ()
    assert validated.snapshots == ()


def test_outdated_document_citation_marks_factual_answer_insufficient() -> None:
    # The chunk referenced by the answer belongs to a document version that is
    # no longer retrievable, so its id is absent from the current context.
    doc_id = str(uuid.uuid4())
    ctx = context_for(fragment("chunk-1", doc_id, version=2))
    answer = grounded(citations=[citation_id(doc_id, "chunk-old")])

    validated = validate_citations(answer, ctx)

    assert validated.insufficient_evidence is True
    assert validated.citations == ()


def test_nonexistent_citation_marks_factual_answer_insufficient() -> None:
    doc_id = str(uuid.uuid4())
    ctx = context_for(fragment("chunk-1", doc_id))
    answer = grounded(citations=["[DOC:unknown#missing]"])

    validated = validate_citations(answer, ctx)

    assert validated.insufficient_evidence is True
    assert validated.citations == ()


def test_any_invalid_citation_degrades_whole_factual_answer() -> None:
    doc_id = str(uuid.uuid4())
    valid = citation_id(doc_id, "chunk-1")
    ctx = context_for(fragment("chunk-1", doc_id))
    answer = grounded(citations=[valid, citation_id(doc_id, "chunk-999")])

    validated = validate_citations(answer, ctx)

    # Fail-closed: the mixed valid/invalid citation set degrades the whole
    # factual answer instead of keeping the valid reference.
    assert validated.insufficient_evidence is True
    assert validated.citations == ()
    assert validated.snapshots == ()


def test_follow_up_question_does_not_require_citations() -> None:
    doc_id = str(uuid.uuid4())
    ctx = context_for(fragment("chunk-1", doc_id))
    answer = grounded(answer="Which order number?", follow_up_question="Which order number?")

    validated = validate_citations(answer, ctx)

    assert validated.insufficient_evidence is False
    assert validated.citations == ()


def test_model_declared_insufficient_is_respected() -> None:
    doc_id = str(uuid.uuid4())
    ctx = context_for(fragment("chunk-1", doc_id))
    answer = grounded(insufficient_evidence=True)

    validated = validate_citations(answer, ctx)

    assert validated.insufficient_evidence is True
    assert isinstance(validated, ValidatedAnswer)


async def test_generation_service_validates_provider_answer() -> None:
    doc_id = str(uuid.uuid4())
    target = citation_id(doc_id, "chunk-1")
    ctx = context_for(fragment("chunk-1", doc_id))
    provider = DeterministicGenerationProvider(citations=[target])

    service = GenerationService(provider=provider)
    validated = await service.answer(query="refund window", context=ctx)

    assert validated.citations == (target,)
    assert validated.insufficient_evidence is False


async def test_generation_service_retries_invalid_citations_without_fabricating_them() -> None:
    doc_id = str(uuid.uuid4())
    target = citation_id(doc_id, "chunk-1")
    ctx = context_for(fragment("chunk-1", doc_id))

    class SequencedProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def answer(self, *, query: str, context: BuiltContext) -> GroundedAnswer:
            self.calls += 1
            if self.calls == 1:
                return grounded(answer="uncited fact", citations=[])
            if self.calls == 2:
                return grounded(answer="wrong citation", citations=["[DOC:fake#missing]"])
            return grounded(answer="verified fact", citations=[target])

    provider = SequencedProvider()
    validated = await GenerationService(provider=provider, max_validation_attempts=3).answer(
        query="refund window", context=ctx
    )

    assert provider.calls == 3
    assert validated.answer == "verified fact"
    assert validated.citations == (target,)
    assert validated.snapshots[0].chunk_id == "chunk-1"


async def test_generation_service_exhaustion_stays_insufficient_without_citation() -> None:
    doc_id = str(uuid.uuid4())
    ctx = context_for(fragment("chunk-1", doc_id))

    class AlwaysUncited:
        def __init__(self) -> None:
            self.calls = 0

        async def answer(self, *, query: str, context: BuiltContext) -> GroundedAnswer:
            self.calls += 1
            return grounded(answer=f"uncited fact {self.calls}", citations=[])

    provider = AlwaysUncited()
    validated = await GenerationService(provider=provider, max_validation_attempts=3).answer(
        query="refund window", context=ctx
    )

    assert provider.calls == 3
    assert validated.insufficient_evidence is True
    assert validated.citations == ()
    assert validated.snapshots == ()


async def test_generation_service_retries_are_bounded_below_worker_timeout() -> None:
    doc_id = str(uuid.uuid4())
    ctx = context_for(fragment("chunk-1", doc_id))

    class HangingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def answer(self, *, query: str, context: BuiltContext) -> GroundedAnswer:
            self.calls += 1
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    provider = HangingProvider()
    service = GenerationService(
        provider=provider,
        max_validation_attempts=3,
        attempt_timeout_seconds=0.01,
    )

    async with asyncio.timeout(0.2):
        validated = await service.answer(query="refund window", context=ctx)

    assert provider.calls == 3
    assert validated.insufficient_evidence is True
    assert validated.snapshots == ()
