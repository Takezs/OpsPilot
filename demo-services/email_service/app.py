"""Demo Email Service: accepts a message and returns a message id."""

import uuid

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Email Service")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


class EmailMessage(BaseModel):
    to: str
    subject: str
    body: str


@app.post("/emails", status_code=202)
async def send_email(message: EmailMessage) -> dict:
    return {"message_id": str(uuid.uuid4()), "to": message.to, "status": "SENT"}
