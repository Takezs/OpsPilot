import asyncio
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

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.models import (
    Document,
    DocumentIndexOutbox,
    DocumentStatus,
    KnowledgeBase,
)
from opspilot.knowledge.schemas import AccessLevel
from opspilot.knowledge.storage import FileStorage, InvalidFile, VolumeFileStorage

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
upload_session_factory = async_session_factory


class DocumentQueue(Protocol):
    async def enqueue_document(
        self, document_id: str, job_id: str, retry_attempt: int = 0
    ) -> None: ...


class ArqDocumentQueue:
    def __init__(self, redis: ArqRedis) -> None:
        self.redis = redis

    async def enqueue_document(self, document_id: str, job_id: str, retry_attempt: int = 0) -> None:
        await self.redis.enqueue_job("index_document", document_id, retry_attempt, _job_id=job_id)


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    version: int
    status: DocumentStatus


class RetryRequest(BaseModel):
    reason: str


class RetryResponse(BaseModel):
    document_id: uuid.UUID
    attempt: int
    job_id: str


def get_file_storage() -> FileStorage:
    return VolumeFileStorage(Path(Settings().storage_root))


async def get_document_queue() -> AsyncIterator[DocumentQueue]:
    redis = await create_pool(RedisSettings.from_dsn(Settings().redis_url))
    try:
        yield ArqDocumentQueue(redis)
    finally:
        await redis.aclose()


async def _delete_owned_file(storage: FileStorage, storage_path: str) -> None:
    cleanup = asyncio.create_task(storage.delete(storage_path))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    cleanup.result()


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
) -> UploadResponse:
    async with upload_session_factory() as scope_session:
        knowledge_base = await scope_session.get(KnowledgeBase, knowledge_base_id)
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

    storage_path: str | None = None
    committed = False
    try:
        try:
            storage_path = await storage.save(stream(), Path(file.filename or "").suffix)
        except InvalidFile as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

        content_sha256 = digest.hexdigest()
        async with upload_session_factory() as session:
            knowledge_base = await session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
            access_level = AccessLevel(knowledge_base.access_level)
            if not principal.knowledge_scope.allows(knowledge_base.department, access_level):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "knowledge scope does not allow upload"
                )
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
            await session.commit()
            committed = True
        return UploadResponse(
            document_id=document.id,
            version=document.version,
            status=document.status,
        )
    except IntegrityError as error:
        if storage_path is not None and not committed:
            await _delete_owned_file(storage, storage_path)
        raise HTTPException(status.HTTP_409_CONFLICT, "document upload conflicts") from error
    except BaseException:
        if storage_path is not None and not committed:
            await _delete_owned_file(storage, storage_path)
        raise


@router.post(
    "/documents/{document_id}/retry",
    response_model=RetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_document(
    document_id: uuid.UUID,
    request: RetryRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> RetryResponse:
    if principal.role not in {Role.REVIEWER, Role.ADMIN}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reviewer or admin role required")
    async with upload_session_factory() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"document-retry:{document_id}"},
        )
        document = await session.get(Document, document_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
        knowledge_base = await session.get(KnowledgeBase, document.knowledge_base_id)
        if knowledge_base is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
        if not principal.knowledge_scope.allows(
            knowledge_base.department, AccessLevel(knowledge_base.access_level)
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "knowledge scope does not allow retry")
        if document.status != DocumentStatus.FAILED:
            raise HTTPException(status.HTTP_409_CONFLICT, "only failed documents can be retried")
        pending = await session.scalar(
            select(DocumentIndexOutbox.id).where(
                DocumentIndexOutbox.document_id == document_id,
                DocumentIndexOutbox.attempt > 0,
                DocumentIndexOutbox.completed_at.is_(None),
            )
        )
        if pending is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "document retry is already pending")
        latest_attempt = await session.scalar(
            select(func.coalesce(func.max(DocumentIndexOutbox.attempt), 0)).where(
                DocumentIndexOutbox.document_id == document_id
            )
        )
        attempt = int(latest_attempt or 0) + 1
        job_id = f"document-index:{document_id}:retry:{attempt}"
        session.add(
            DocumentIndexOutbox(
                document_id=document_id,
                job_id=job_id,
                attempt=attempt,
                requested_by=uuid.UUID(principal.user_id),
                audit_reason=request.reason[:500],
            )
        )
        await session.commit()
    return RetryResponse(document_id=document_id, attempt=attempt, job_id=job_id)
