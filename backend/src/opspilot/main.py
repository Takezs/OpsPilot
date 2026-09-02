from fastapi import FastAPI

from opspilot.approvals.router import router as approvals_router
from opspilot.auth.router import router as auth_router
from opspilot.config import Settings
from opspilot.evaluation.router import router as evaluation_router
from opspilot.knowledge.router import catalog_router
from opspilot.knowledge.router import router as knowledge_router
from opspilot.observability.redaction import install_safe_logging
from opspilot.retrieval.router import router as retrieval_router
from opspilot.runs.router import router as runs_router

install_safe_logging()
app = FastAPI(title="OpsPilot", version="0.1.0")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(catalog_router, prefix="/api/v1")
app.include_router(approvals_router, prefix="/api/v1")
app.include_router(runs_router, prefix="/api/v1")
app.include_router(retrieval_router, prefix="/api/v1")
app.include_router(evaluation_router, prefix="/api/v1")
if Settings().evaluation_fault_matrix:
    from opspilot.evaluation.faults import router as evaluation_fault_router

    app.include_router(evaluation_fault_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
