import json
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "development-only-change-me-unsafe"
    access_token_ttl_seconds: int = 900
    storage_root: str = "data/documents"
    bge_base_url: str = "http://localhost:8080/v1"
    bge_api_key: str = "local"
    bge_embedding_model: str = "BAAI/bge-m3"
    bge_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_proxy_url: str | None = None
    generation_context_token_budget: int = 4000
    agent_max_model_calls: int = 8
    agent_max_tool_calls: int = 6
    agent_max_input_tokens: int = 16_000
    agent_max_duration_seconds: float = 300.0
    retrieval_reranker_timeout_seconds: float = 5.0
    # Task 9: default time-to-decide for a pending approval request.
    approval_ttl_seconds: int = 86400
    order_service_url: str = "http://127.0.0.1:8101"
    payment_service_url: str = "http://127.0.0.1:8102"
    email_service_url: str = "http://127.0.0.1:8103"
    payment_timeout_seconds: float = 1.0
    operation_lease_seconds: int = 30
    evaluation_api_base_url: str = "http://127.0.0.1:8000/api/v1"
    evaluation_api_token: str = ""
    runtime_secrets_file: str = ""
    evaluation_configuration_file: str = ""
    evaluation_configuration_sha: str = ""
    evaluation_identity_file: str = ""
    evaluation_user_credentials_file: str = ""
    evaluation_reviewer_credentials_file: str = ""
    evaluation_dataset_root: str = "../evaluation/datasets"
    evaluation_lease_seconds: int = 300
    evaluation_fault_matrix: bool = Field(
        default=False, validation_alias="OPSPILOT_EVAL_FAULT_MATRIX"
    )

    @model_validator(mode="after")
    def reject_weak_production_secret(self) -> Self:
        if self.runtime_secrets_file:
            try:
                with Path(self.runtime_secrets_file).open("rb") as stream:
                    raw = stream.read(16385)
                if len(raw) > 16384:
                    raise ValueError("oversized secrets file")
                values = json.loads(raw)
                expected = {"database_url", "jwt_secret", "deepseek_api_key", "bge_api_key"}
                if not isinstance(values, dict) or set(values) != expected:
                    raise ValueError("invalid secrets file")
                if not all(isinstance(value, str) and value for value in values.values()):
                    raise ValueError("invalid secrets file")
                for key, value in values.items():
                    setattr(self, key, value)
            except (OSError, ValueError, TypeError):
                raise ValueError("runtime credential file is invalid") from None
        if self.environment == "production" and (
            self.jwt_secret == "development-only-change-me-unsafe" or len(self.jwt_secret) < 32
        ):
            raise ValueError("JWT secret must be at least 32 characters and explicitly configured")
        return self
