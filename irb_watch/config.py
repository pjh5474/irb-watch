import os
from functools import lru_cache
from typing import Literal

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
    # The project's publishable (public) key. Only used by POST /auth/token, the login behind the
    # Authorize button of the API docs page. Empty = that endpoint answers 503.
    supabase_publishable_key: str = ""

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
    # Docs generator: AI drafts plus AI revisions one user may make per rolling 24 hours
    # (0 switches it off; hand edits and DOCX downloads are not counted).
    docgen_requests_per_day: int = 10

    # Download links for original files: how long a signed link works, and how many links
    # one user may request per day (admins are not limited).
    download_link_ttl_seconds: int = 60
    download_links_per_day: int = 200

    # Where the hospital logins used for scheduled scans of members-only boards are kept:
    #   keyring  = Windows Credential Manager (this PC only; entered with save_credentials.py)
    #   database = encrypted rows in site_credentials (for a server); needs CREDENTIALS_KEY,
    #              a Fernet key you create yourself (see irb_watch/security/secretbox.py).
    #              Several keys separated by commas rotate keys: the first encrypts.
    credential_store: Literal["keyring", "database"] = "keyring"
    credentials_key: str = ""

    # The daily scan: off unless SCHEDULER_ENABLED=true (tests and local runs stay quiet).
    scheduler_enabled: bool = False
    scan_cron: str = "0 6 * * *"
    scan_timezone: str = "Asia/Seoul"
    max_file_bytes: int = 20_000_000
    max_docs_per_url: int = 200
    # A scan is saved in one database call; refuse (needs_review) above this JSON size.
    # The largest real board so far is about 2 MB of text.
    max_scan_payload_bytes: int = 20_000_000
    max_llm_calls_per_run: int = 100
    per_domain_delay_seconds: float = 2.0
    # Temporary site errors (HTTP 502/503/504, timeouts): waits before each retry, and how
    # many retries one scan may use in total before it is abandoned as "site unavailable".
    fetch_retry_waits_seconds: str = "5,15"
    fetch_retry_budget: int = 10
    request_timeout_seconds: float = 30.0

    # Email (Resend): alarms when a source turns amber or red, recovery notices and the daily
    # scan report for admins. Mail is on only when all three are set; without them every mail
    # call is a logged no-op and scans are not affected. The key is typed by the owner on the
    # server and never printed or logged.
    resend_api_key: str = ""
    mail_from: str = ""  # for example: IRB Watch <alerts@your-domain>
    app_url: str = ""  # the frontend address used for links in mails, no trailing slash
    # A source with no good scan for this long counts as out of date (amber).
    alert_stale_hours: int = 36
    # The daily digest for users also needs a secret that signs the unsubscribe links (any long
    # random string, for example Python: secrets.token_hex(32); keep it ONLY in env vars) and the
    # public hostname of this API (the same API_DOMAIN the deploy uses) for the one-click link.
    unsubscribe_key: str = ""
    api_domain: str = ""
    # The weekly stats mail for admins: Mondays 07:00 in SCAN_TIMEZONE (crontab syntax). Write the
    # weekday as a NAME (mon, tue, ...): the scheduler counts weekday numbers from Monday = 0,
    # so "1" would mean Tuesday.
    weekly_stats_cron: str = "0 7 * * mon"

    @property
    def mail_enabled(self) -> bool:
        return bool(self.resend_api_key.strip() and self.mail_from.strip() and self.app_url.strip())

    @property
    def digest_enabled(self) -> bool:
        return bool(self.mail_enabled and self.unsubscribe_key.strip() and self.api_domain.strip())

    @property
    def fetch_retry_waits(self) -> tuple[float, ...]:
        return tuple(float(x) for x in self.fetch_retry_waits_seconds.split(",") if x.strip())

    @property
    def frontend_origin_list(self) -> list[str]:
        return [o.strip() for o in self.frontend_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=env_file_name())
