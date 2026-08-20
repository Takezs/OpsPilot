from typing import BinaryIO

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


class MarkdownParser:
    def parse(self, stream: BinaryIO) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                blocks.append(ParsedBlock(BlockKind.PARAGRAPH, " ".join(paragraph)))
                paragraph.clear()

        for raw_line in stream.read().decode("utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                flush()
            elif line.startswith("#"):
                flush()
                marker, _, title = line.partition(" ")
                blocks.append(ParsedBlock(BlockKind.HEADING, title or marker, len(marker)))
            else:
                paragraph.append(line)
        flush()
        return blocks
