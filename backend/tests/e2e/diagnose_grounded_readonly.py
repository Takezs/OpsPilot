"""Explicit live, non-evaluation diagnostic. Run inside the configured worker.

Uses existing file authentication and real providers. No Payment dependency.
Only objects created by this invocation are removed; model output is not logged.
"""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import httpx
from sqlalchemy import delete, select

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.evaluation.credentials import authenticate_file
from opspilot.execution.models import Operation
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider, embedding_cache_key
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.runs.models import Run


async def main() -> None:
    settings = Settings()
    embedding = BgeM3EmbeddingProvider(
        settings.bge_base_url, settings.bge_api_key, settings.bge_embedding_model
    )
    kb_id, doc_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    marker = "diag-" + uuid.uuid4().hex
    verification_code = uuid.uuid4().hex
    content = f"Diagnostic policy {marker}: the verification code is {verification_code}."
    run_id = None
    seeded = False
    reply_received = False
    try:
        async with httpx.AsyncClient(
            base_url=settings.evaluation_api_base_url, timeout=30
        ) as client:
            headers = await authenticate_file(
                client, Path(settings.evaluation_user_credentials_file), expected_role="USER"
            )
            response = await client.get("/auth/me", headers=headers)
            response.raise_for_status()
            principal = response.json()
            departments = principal["allowed_departments"]
            if not departments:
                raise ValueError("diagnostic user has no knowledge scope")
            department = marker if "*" in departments else departments[0]
            vector = (await embedding.embed([content]))[0]
            async with async_session_factory() as session:
                session.add(
                    KnowledgeBase(
                        id=kb_id,
                        name=marker,
                        department=department,
                        access_level=principal["max_access_level"],
                    )
                )
                await session.flush()
                session.add(
                    Document(
                        id=doc_id,
                        knowledge_base_id=kb_id,
                        title=marker,
                        version=1,
                        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                        storage_path="diagnostic:no-file",
                        status=DocumentStatus.READY,
                    )
                )
                await session.flush()
                session.add(
                    Chunk(
                        id=chunk_id,
                        document_id=doc_id,
                        position=0,
                        content=content,
                        token_count=80,
                        embedding=vector,
                        embedding_cache_key=embedding_cache_key(embedding.model, content),
                    )
                )
                await session.commit()
                seeded = True
            query = (
                f"Search the knowledge base for diagnostic policy {marker}. "
                "What is its verification code? Cite that document."
            )
            response = await client.post(
                "/retrieval/debug", headers=headers, json={"query": query, "top_k": 5}
            )
            response.raise_for_status()
            retrieval = response.json()
            candidates = retrieval.get("reranker") or retrieval.get("rrf") or []
            exact_candidate = any(row["chunk_id"] == str(chunk_id) for row in candidates)
            response = await client.post("/runs", headers=headers)
            response.raise_for_status()
            run_id = uuid.UUID(response.json()["run_id"])
            response = await client.post(
                f"/runs/{run_id}/messages", headers=headers, json={"content": query}
            )
            response.raise_for_status()
            async with asyncio.timeout(180):
                while True:
                    response = await client.get(f"/runs/{run_id}/history", headers=headers)
                    response.raise_for_status()
                    replies = [
                        row
                        for row in response.json()
                        if row["event_type"] == "assistant_message_created"
                    ]
                    if replies:
                        reply_received = True
                        payload = replies[-1]["payload"]
                        citations = payload.get("citations", [])
                        print(
                            json.dumps(
                                {
                                    "embedding_dimensions": len(vector),
                                    "candidate_count": len(candidates),
                                    "exact_candidate": exact_candidate,
                                    "citation_count": len(citations),
                                    "exact_citation": any(
                                        c.get("document_id") == str(doc_id)
                                        and c.get("chunk_id") == str(chunk_id)
                                        and c.get("document_version") == 1
                                        for c in citations
                                    ),
                                    "private_code_present": verification_code
                                    in payload.get("content", ""),
                                }
                            )
                        )
                        if (
                            not exact_candidate
                            or verification_code not in payload.get("content", "")
                            or not any(
                                c.get("document_id") == str(doc_id)
                                and c.get("chunk_id") == str(chunk_id)
                                and c.get("document_version") == 1
                                for c in citations
                            )
                        ):
                            raise ValueError("grounded diagnostic failed")
                        break
                    await asyncio.sleep(1)
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
    finally:
        await embedding.aclose()
        async with async_session_factory() as session:
            operations = (
                (
                    await session.scalars(select(Operation.id).where(Operation.run_id == run_id))
                ).all()
                if run_id
                else []
            )
            if operations or (run_id is not None and not reply_received):
                print(json.dumps({"cleanup_deferred": True, "operation_count": len(operations)}))
            elif seeded:
                if run_id:
                    await session.execute(delete(Run).where(Run.id == run_id))
                await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
                await session.commit()
                print(json.dumps({"owned_diagnostic_rows_removed": True}))


if __name__ == "__main__":
    asyncio.run(main())
