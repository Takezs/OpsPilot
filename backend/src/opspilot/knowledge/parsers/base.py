from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"


@dataclass(frozen=True)
class ParsedBlock:
    kind: BlockKind
    text: str
    level: int | None = None
    page: int | None = None


class DocumentParser(Protocol):
    def parse(self, path: Path) -> list[ParsedBlock]: ...
