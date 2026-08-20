from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "development-only-change-me"
    access_token_ttl_seconds: int = 900
    storage_root: str = "data/documents"
    bge_base_url: str = "http://localhost:8080/v1"
    bge_api_key: str = "local"
    bge_embedding_model: str = "BAAI/bge-m3"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
