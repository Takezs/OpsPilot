from dataclasses import dataclass

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


@dataclass(frozen=True)
class TextChunk:
    content: str
    section_path: list[str]
    token_count: int
    page: int | None = None


def _tokens(text: str) -> list[str]:
    return text.split()


def chunk_blocks(
    blocks: list[ParsedBlock], max_tokens: int = 650, overlap_tokens: int = 50
) -> list[TextChunk]:
    if max_tokens <= overlap_tokens:
        raise ValueError("max_tokens must exceed overlap_tokens")
    section_path: list[str] = []
    chunks: list[TextChunk] = []
    for block in blocks:
        if block.kind is BlockKind.HEADING:
            level = block.level or 1
            section_path = section_path[: level - 1]
            section_path.append(block.text)
            continue
        prefix = "\n".join(section_path)
        content = f"{prefix}\n{block.text}".strip()
        words = _tokens(content)
        if block.kind is BlockKind.TABLE or len(words) <= max_tokens:
            chunks.append(TextChunk(content, list(section_path), len(words), block.page))
            continue
        step = max_tokens - overlap_tokens
        for start in range(0, len(words), step):
            window = words[start : start + max_tokens]
            chunks.append(TextChunk(" ".join(window), list(section_path), len(window), block.page))
            if start + max_tokens >= len(words):
                break
    return chunks
