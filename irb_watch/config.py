import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def env_file_name() -> str:
    """APP_ENV picks the settings file: APP_ENV=prod loads .env.prod (default: dev).

    Real environment variables always override values from the file.
    """
    return f".env.{os.environ.get('APP_ENV', 'dev')}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = "dev"

    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Comma-separated browser origins allowed to call this API (CORS).
    frontend_origins: str = "http://localhost:3000"
    min_password_length: int = 8

    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    openai_timeout_seconds: float = 60.0

    # Jev (TypeSafe) classifies changes; leave the key empty to switch it off.
    typesafe_api_key: str = ""
    typesafe_model: str = "jev-latest"
    typesafe_timeout_seconds: float = 30.0

    # A change is "cosmetic" when Jev's probability that it is substantive is below this
    # and Jev is confident it is low priority.
    ai_skip_below: float = 0.15
    # Jev answers with less confidence than this count as "unsure" (priority medium).
    ai_min_confidence: float = 0.6
    # New summaries one user may request per day (cached ones are free).
    summary_requests_per_day: int = 20

    # Proof of access to a members-only board: how long it lasts, and how many login
    # checks one user may start per hour (protects hospital accounts from lockout).
    access_ttl_days: int = 30
    access_attempts_per_hour: int = 5

    scan_cron: str = "0 6 * * *"
    max_file_bytes: int = 20_000_000
    max_docs_per_url: int = 200
    # A scan is saved in one database call; refuse (needs_review) above this JSON size.
    # The largest real board so far is about 2 MB of text.
    max_scan_payload_bytes: int = 20_000_000
    max_llm_calls_per_run: int = 100
    per_domain_delay_seconds: float = 2.0
    request_timeout_seconds: float = 30.0

    @property
    def frontend_origin_list(self) -> list[str]:
        return [o.strip() for o in self.frontend_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=env_file_name())
