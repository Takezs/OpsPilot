import hashlib
from dataclasses import dataclass
from enum import IntEnum
from typing import BinaryIO


class AccessLevel(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2


@dataclass(frozen=True)
class KnowledgeScope:
    departments: frozenset[str]
    max_access_level: AccessLevel

    def allows(self, department: str, access_level: AccessLevel) -> bool:
        return (
            department in self.departments or "*" in self.departments
        ) and access_level <= self.max_access_level


@dataclass(frozen=True)
class UploadMetadata:
    title: str
    department: str
    access_level: AccessLevel
    content_sha256: str

    @classmethod
    def from_file(
        cls,
        title: str,
        department: str,
        access_level: AccessLevel,
        file: BinaryIO,
    ) -> "UploadMetadata":
        digest = hashlib.sha256()
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
        file.seek(0)
        return cls(title, department, access_level, digest.hexdigest())

    def queue_payload(self, document_id: str) -> dict[str, str]:
        return {"document_id": document_id}
