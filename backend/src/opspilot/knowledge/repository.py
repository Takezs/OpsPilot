import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.knowledge.models import Document


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, document_id: uuid.UUID) -> Document | None:
        return await self.session.scalar(select(Document).where(Document.id == document_id))

    async def add(self, document: Document) -> Document:
        self.session.add(document)
        await self.session.flush()
        return document
