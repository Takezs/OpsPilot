import re
from dataclasses import dataclass

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock

TOKEN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]")


class TokenCounter:
    """Count each Han character, word, or punctuation mark as one estimated token."""

    def tokens(self, text: str) -> list[str]:
        return TOKEN_PATTERN.findall(text)

    def count(self, text: str) -> int:
        return len(self.tokens(text))

    def join(self, tokens: list[str]) -> str:
        result: list[str] = []
        for token in tokens:
            if result and result[-1][-1:].isalnum() and token[:1].isalnum():
                if result[-1][-1:].isascii() and token[:1].isascii():
                    result.append(" ")
            result.append(token)
        return "".join(result)


@dataclass(frozen=True)
class TextChunk:
    content: str
    section_path: list[str]
    token_count: int
    page: int | None = None


def chunk_blocks(
    blocks: list[ParsedBlock],
    max_tokens: int = 650,
    overlap_tokens: int = 50,
    counter: TokenCounter | None = None,
) -> list[TextChunk]:
    if max_tokens <= overlap_tokens:
        raise ValueError("max_tokens must exceed overlap_tokens")
    token_counter = counter or TokenCounter()
    section_path: list[str] = []
    chunks: list[TextChunk] = []
    pending: list[str] = []
    pending_page: int | None = None

    def prefix() -> str:
        return "\n".join(section_path)

    def build_content(parts: list[str]) -> str:
        return "\n".join(part for part in [prefix(), *parts] if part).strip()

    def append_content(content: str, page: int | None) -> None:
        tokens = token_counter.tokens(content)
        if len(tokens) <= max_tokens:
            chunks.append(TextChunk(content, list(section_path), len(tokens), page))
            return
        step = max_tokens - overlap_tokens
        for start in range(0, len(tokens), step):
            window = tokens[start : start + max_tokens]
            chunks.append(
                TextChunk(token_counter.join(window), list(section_path), len(window), page)
            )
            if start + max_tokens >= len(tokens):
                break

    def flush() -> None:
        nonlocal pending_page
        if pending:
            append_content(build_content(pending), pending_page)
            pending.clear()
            pending_page = None

    for block in blocks:
        if block.kind == BlockKind.HEADING:
            flush()
            level = block.level or 1
            section_path = section_path[: level - 1]
            section_path.append(block.text)
            continue
        if block.kind == BlockKind.TABLE:
            flush()
            table_parts: list[str] = []
            for row in block.text.splitlines():
                candidate = build_content([*table_parts, row])
                if table_parts and token_counter.count(candidate) > max_tokens:
                    append_content(build_content(table_parts), block.page)
                    table_parts = []
                if token_counter.count(build_content([row])) > max_tokens:
                    append_content(build_content([row]), block.page)
                else:
                    table_parts.append(row)
            if table_parts:
                append_content(build_content(table_parts), block.page)
            continue
        candidate = build_content([*pending, block.text])
        if pending and (pending_page != block.page or token_counter.count(candidate) > max_tokens):
            flush()
            candidate = build_content([block.text])
        if token_counter.count(candidate) > max_tokens:
            flush()
            append_content(candidate, block.page)
        else:
            pending.append(block.text)
            pending_page = block.page
    flush()
    return chunks
