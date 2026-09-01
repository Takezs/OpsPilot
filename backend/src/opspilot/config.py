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
    evaluation_dataset_root: str = "../evaluation/datasets"
    evaluation_lease_seconds: int = 300
    evaluation_fault_matrix: bool = Field(
        default=False, validation_alias="OPSPILOT_EVAL_FAULT_MATRIX"
    )

    @model_validator(mode="after")
    def reject_weak_production_secret(self) -> Self:
        if self.environment == "production" and (
            self.jwt_secret == "development-only-change-me-unsafe" or len(self.jwt_secret) < 32
        ):
            raise ValueError("JWT secret must be at least 32 characters and explicitly configured")
        return self
