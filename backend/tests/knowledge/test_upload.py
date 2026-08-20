from io import BytesIO
from pathlib import Path

import pytest

from opspilot.knowledge.schemas import AccessLevel, KnowledgeScope, UploadMetadata
from opspilot.knowledge.storage import InvalidFile, VolumeFileStorage


def test_knowledge_scope_rejects_cross_department() -> None:
    scope = KnowledgeScope(
        departments=frozenset({"support"}), max_access_level=AccessLevel.INTERNAL
    )

    assert scope.allows("support", AccessLevel.INTERNAL)
    assert not scope.allows("finance", AccessLevel.INTERNAL)
    assert not scope.allows("support", AccessLevel.CONFIDENTIAL)


@pytest.mark.asyncio
async def test_volume_storage_uses_uuid_name_and_preserves_content(tmp_path: Path) -> None:
    storage = VolumeFileStorage(tmp_path)
    stored_path = await storage.save_bytes(b"# Refund policy", ".md")

    path = Path(stored_path)
    assert path.parent == tmp_path.resolve()  # noqa: ASYNC240
    assert path.suffix == ".md"
    assert path.stem != "refund-policy"
    assert path.read_bytes() == b"# Refund policy"  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_upload_validation_rejects_extension_and_oversize(tmp_path: Path) -> None:
    storage = VolumeFileStorage(tmp_path, max_bytes=10)

    with pytest.raises(InvalidFile, match="extension"):
        await storage.save_bytes(b"data", ".exe")
    with pytest.raises(InvalidFile, match="20 MB|size"):
        await storage.save_bytes(b"too many bytes", ".md")


def test_upload_metadata_hash_is_stable_and_queue_payload_is_id_only() -> None:
    first = UploadMetadata.from_file("policy", "support", AccessLevel.INTERNAL, BytesIO(b"same"))
    second = UploadMetadata.from_file("policy", "support", AccessLevel.INTERNAL, BytesIO(b"same"))

    assert first.content_sha256 == second.content_sha256
    assert first.queue_payload("550e8400-e29b-41d4-a716-446655440000") == {
        "document_id": "550e8400-e29b-41d4-a716-446655440000"
    }
