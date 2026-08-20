from typing import BinaryIO

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


class DocxParser:
    def parse(self, stream: BinaryIO) -> list[ParsedBlock]:
        document = Document(stream)
        blocks: list[ParsedBlock] = []
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if not text:
                    continue
                style = paragraph.style.name if paragraph.style else ""
                if style.startswith("Heading"):
                    level = int(style.removeprefix("Heading ") or "1")
                    blocks.append(ParsedBlock(BlockKind.HEADING, text, level=level))
                else:
                    blocks.append(ParsedBlock(BlockKind.PARAGRAPH, text))
            elif isinstance(child, CT_Tbl):
                table = Table(child, document)
                rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
                blocks.append(ParsedBlock(BlockKind.TABLE, "\n".join(rows)))
        return blocks
