from fastapi import FastAPI

from opspilot.approvals.router import router as approvals_router
from opspilot.auth.router import router as auth_router
from opspilot.knowledge.router import router as knowledge_router

app = FastAPI(title="OpsPilot", version="0.1.0")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(approvals_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
