"""Encrypt small secrets (hospital logins) for storage in the database.

Fernet: AES-128-CBC with an HMAC, so a changed or wrong-key ciphertext is detected, never
silently decrypted to garbage. CREDENTIALS_KEY holds one key, or several separated by commas
for rotation: the FIRST key encrypts, every key can decrypt. Make a key with
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretBoxError(Exception):
    """The key is missing or invalid, or the data cannot be decrypted with it."""


class SecretBox:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise SecretBoxError("CREDENTIALS_KEY is not set")
        try:
            self._fernet = MultiFernet([Fernet(k.encode("ascii")) for k in parts])
        except (ValueError, UnicodeEncodeError):
            raise SecretBoxError(
                "CREDENTIALS_KEY is not a valid Fernet key (32 url-safe base64 bytes)"
            ) from None

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise SecretBoxError(
                "stored credentials cannot be decrypted with CREDENTIALS_KEY "
                "(wrong key, or the data was changed)"
            ) from None
