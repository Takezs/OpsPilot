import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import get_session
from opspilot.knowledge.models import (
    Document,
    DocumentIndexOutbox,
    DocumentStatus,
    KnowledgeBase,
)
from opspilot.knowledge.schemas import AccessLevel
from opspilot.knowledge.storage import FileStorage, InvalidFile, VolumeFileStorage

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class DocumentQueue(Protocol):
    async def enqueue_document(self, document_id: str, job_id: str) -> None: ...


class ArqDocumentQueue:
    def __init__(self, redis: ArqRedis) -> None:
        self.redis = redis

    async def enqueue_document(self, document_id: str, job_id: str) -> None:
        await self.redis.enqueue_job("index_document", document_id, _job_id=job_id)


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    version: int
    status: DocumentStatus


def get_file_storage() -> FileStorage:
    return VolumeFileStorage(Path(Settings().storage_root))


async def get_document_queue() -> AsyncIterator[DocumentQueue]:
    redis = await create_pool(RedisSettings.from_dsn(Settings().redis_url))
    try:
        yield ArqDocumentQueue(redis)
    finally:
        await redis.aclose()


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    knowledge_base_id: uuid.UUID,
    title: Annotated[str, Form(min_length=1, max_length=255)],
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(get_current_principal)],
    storage: Annotated[FileStorage, Depends(get_file_storage)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UploadResponse:
    knowledge_base = await session.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    access_level = AccessLevel(knowledge_base.access_level)
    if not principal.knowledge_scope.allows(knowledge_base.department, access_level):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "knowledge scope does not allow upload")

    digest = hashlib.sha256()

    async def stream() -> AsyncIterator[bytes]:
        while chunk := await file.read(1024 * 1024):
            digest.update(chunk)
            yield chunk

    try:
        storage_path = await storage.save(stream(), Path(file.filename or "").suffix)
    except InvalidFile as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

    content_sha256 = digest.hexdigest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"document-upload:{knowledge_base_id}"},
    )
    duplicate = await session.scalar(
        select(Document.id).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.content_sha256 == content_sha256,
        )
    )
    if duplicate is not None:
        await session.rollback()
        await storage.delete(storage_path)
        raise HTTPException(status.HTTP_409_CONFLICT, "document content already exists")
    latest_version = await session.scalar(
        select(func.coalesce(func.max(Document.version), 0)).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.title == title,
        )
    )
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        knowledge_base_id=knowledge_base_id,
        title=title,
        version=int(latest_version or 0) + 1,
        content_sha256=content_sha256,
        storage_path=storage_path,
        status=DocumentStatus.UPLOADED,
    )
    session.add(document)
    session.add(
        DocumentIndexOutbox(
            document_id=document_id,
            job_id=f"document-index:{document_id}",
        )
    )
    try:
        await session.commit()
        await session.refresh(document)
    except IntegrityError as error:
        await session.rollback()
        await storage.delete(storage_path)
        raise HTTPException(status.HTTP_409_CONFLICT, "document upload conflicts") from error
    return UploadResponse(document_id=document.id, version=document.version, status=document.status)
