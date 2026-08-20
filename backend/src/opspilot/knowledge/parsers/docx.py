from pathlib import Path

from docx import Document

from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock


class DocxParser:
    def parse(self, path: Path) -> list[ParsedBlock]:
        document = Document(str(path))
        blocks: list[ParsedBlock] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style = paragraph.style.name if paragraph.style else ""
            if style.startswith("Heading"):
                level = int(style.removeprefix("Heading ") or "1")
                blocks.append(ParsedBlock(BlockKind.HEADING, text, level=level))
            else:
                blocks.append(ParsedBlock(BlockKind.PARAGRAPH, text))
        for table in document.tables:
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
            blocks.append(ParsedBlock(BlockKind.TABLE, "\n".join(rows)))
        return blocks
