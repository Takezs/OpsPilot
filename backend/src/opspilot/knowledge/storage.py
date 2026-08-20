import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import BinaryIO, Protocol


class InvalidFile(ValueError):
    pass


class FileStorage(Protocol):
    async def save(self, stream: AsyncIterator[bytes], suffix: str) -> str: ...

    def open(self, storage_path: str) -> AbstractAsyncContextManager[BinaryIO]: ...


class VolumeFileStorage:
    allowed_suffixes = frozenset({".md", ".txt", ".pdf", ".docx"})

    def __init__(self, root: Path, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, stream: AsyncIterator[bytes], suffix: str) -> str:
        normalized_suffix = suffix.lower()
        if normalized_suffix not in self.allowed_suffixes:
            raise InvalidFile("unsupported extension")
        destination = (self.root / f"{uuid.uuid4()}{normalized_suffix}").resolve()
        if destination.parent != self.root:
            raise InvalidFile("invalid storage path")
        size = 0
        with destination.open("xb") as output:
            async for chunk in stream:
                size += len(chunk)
                if size > self.max_bytes:
                    output.close()
                    destination.unlink(missing_ok=True)
                    raise InvalidFile("file size exceeds 20 MB limit")
                output.write(chunk)
        return str(destination)

    async def save_bytes(self, content: bytes, suffix: str) -> str:
        async def chunks() -> AsyncIterator[bytes]:
            yield content

        return await self.save(chunks(), suffix)

    @asynccontextmanager
    async def open(self, storage_path: str) -> AsyncIterator[BinaryIO]:
        path = Path(storage_path).resolve()  # noqa: ASYNC240
        if path.parent != self.root:
            raise InvalidFile("storage path escapes configured root")
        handle = path.open("rb")
        try:
            yield handle
        finally:
            handle.close()
