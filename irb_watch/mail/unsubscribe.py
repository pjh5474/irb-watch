"""Signed unsubscribe tokens for the daily digest.

A token is `<user id>.<signature>`: HMAC-SHA256 over `unsubscribe:<user id>` with the server's
UNSUBSCRIBE_KEY, URL-safe. It cannot be guessed or made for another user without the key, it
has no expiry (an unsubscribe link must keep working), and changing the key switches all
existing links off. The only thing a token can do is turn that one user's digest off.
"""

import base64
import hashlib
import hmac
import re

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _sign(user_id: str, key: str) -> str:
    digest = hmac.new(key.encode(), f"unsubscribe:{user_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_token(user_id: str, key: str) -> str:
    return f"{user_id}.{_sign(user_id, key)}"


def read_token(token: str, key: str) -> str | None:
    """The user id the token was made for, or None for anything that is not a valid token."""
    if not key or not isinstance(token, str) or len(token) > 200:
        return None
    user_id, _, signature = token.rpartition(".")
    if not _UUID.match(user_id):
        return None
    if not hmac.compare_digest(signature, _sign(user_id, key)):
        return None
    return user_id
