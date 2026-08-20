from pathlib import Path

import pymupdf

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


class PdfParser:
    def parse(self, path: Path) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        with pymupdf.open(path) as document:
            for page_number, page in enumerate(document, start=1):
                for text in page.get_text("blocks"):
                    content = str(text[4]).strip()
                    if content:
                        blocks.append(ParsedBlock(BlockKind.PARAGRAPH, content, page=page_number))
        return blocks
