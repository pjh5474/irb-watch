from functools import lru_cache

from supabase import Client, create_client

from irb_watch.config import get_settings


@lru_cache
def get_supabase_client() -> Client:
    """Service-role client. Bypasses RLS, so it must only be used server-side."""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
