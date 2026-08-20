from opspilot.knowledge.chunking import chunk_blocks
from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


def test_heading_path_and_table_integrity_are_preserved() -> None:
    blocks = [
        ParsedBlock(BlockKind.HEADING, "3 退款", level=1),
        ParsedBlock(BlockKind.HEADING, "3.2 七天无理由", level=2),
        ParsedBlock(BlockKind.PARAGRAPH, "用户可在签收后七天内申请退款。"),
        ParsedBlock(BlockKind.TABLE, "条件 | 处理\n未拆封 | 全额退款"),
    ]

    chunks = chunk_blocks(blocks, max_tokens=30, overlap_tokens=5)
    target = next(chunk for chunk in chunks if "七天无理由" in chunk.content)

    assert target.section_path == ["3 退款", "3.2 七天无理由"]
    assert target.token_count <= 30
    assert any("条件 | 处理\n未拆封 | 全额退款" in chunk.content for chunk in chunks)


def test_chunks_have_bounded_overlap() -> None:
    blocks = [ParsedBlock(BlockKind.PARAGRAPH, " ".join(f"word{i}" for i in range(30)))]

    chunks = chunk_blocks(blocks, max_tokens=12, overlap_tokens=3)

    assert len(chunks) >= 3
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert previous.content.split()[-3:] == current.content.split()[:3]
