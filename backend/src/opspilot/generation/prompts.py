"""Prompt templates for grounded answering and query rewrite."""

from opspilot.retrieval.context_builder import BuiltContext, ContextFragment

SYSTEM_PROMPT = (
    "You answer enterprise questions strictly from the provided context blocks. "
    "Every factual claim must cite the exact [DOC:<document_id>#<chunk_id>] ids "
    "present in the context. If the context has no evidence for a factual answer, "
    "set insufficient_evidence to true. If you need to ask for clarification, set "
    "follow_up_question instead. Respond with a single JSON object with keys "
    '"answer", "citations", "insufficient_evidence", "follow_up_question".'
)

REWRITE_SYSTEM_PROMPT = (
    "Rewrite the user query into a self-contained search query that preserves "
    "entities and intent from the conversation. Reply with only the rewritten "
    "query text, no explanation."
)


def render_user_prompt(query: str, context: BuiltContext) -> str:
    blocks = [_render_fragment(item) for item in context.fragments]
    body = "\n\n".join(blocks) if blocks else "(no context available)"
    return f"Query: {query}\n\nContext:\n{body}"


def _render_fragment(item: ContextFragment) -> str:
    page = item.page if item.page is not None else "N/A"
    section = "/".join(item.section_path)
    header = (
        f"{item.citation_id} title={item.title} version={item.document_version} "
        f"section={section} effective_at={item.effective_at.date()} page={page}"
    )
    return f"{header}\n{item.content}"
