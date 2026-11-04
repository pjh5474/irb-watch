from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""

    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""

    scan_cron: str = "0 6 * * *"
    max_file_bytes: int = 20_000_000
    max_docs_per_url: int = 200
    max_llm_calls_per_run: int = 100
    per_domain_delay_seconds: float = 2.0
    request_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
