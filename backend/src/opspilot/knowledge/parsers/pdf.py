from typing import BinaryIO

import pymupdf

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


class PdfParser:
    def parse(self, stream: BinaryIO) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        with pymupdf.open(stream=stream.read(), filetype="pdf") as document:  # type: ignore[no-untyped-call]
            for page_number, page in enumerate(document, start=1):
                for text in page.get_text("blocks"):
                    content = str(text[4]).strip()
                    if content:
                        blocks.append(ParsedBlock(BlockKind.PARAGRAPH, content, page=page_number))
        return blocks
