"""Sending mail through Resend (https://resend.com), over plain HTTPS.

Plain server code: no AI model reads or writes a mail, and no model is given a mail tool.
An error never contains the API key or an address; the text of Resend's own refusal is kept
(it says things like "domain is not verified") with addresses blanked out.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

RESEND_URL = "https://api.resend.com/emails"
MAX_RECIPIENTS_PER_CALL = 50  # Resend's limit for one request
_ADDRESS = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+")


class MailError(Exception):
    """A mail could not be sent. The message is safe to log and to show an admin."""


@dataclass(frozen=True)
class Mail:
    to: tuple[str, ...]
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    def send(self, mail: Mail) -> str:
        """Send it; return the provider's message id. Raises MailError."""
        ...


def _safe_reason(response: httpx.Response) -> str:
    """What Resend said, short and with addresses removed."""
    try:
        body = response.json()
        message = str(body.get("message") or body.get("error") or "")
    except (ValueError, AttributeError):  # not JSON, or not an object
        message = ""
    message = _ADDRESS.sub("<address>", message)[:200]
    return f"HTTP {response.status_code}" + (f": {message}" if message else "")


class ResendMailer:
    def __init__(
        self,
        api_key: str,
        sender: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = api_key
        self._sender = sender
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep

    def send(self, mail: Mail) -> str:
        if not mail.to:
            raise MailError("no recipients")
        ids = [self._send_one(mail, mail.to[i : i + MAX_RECIPIENTS_PER_CALL])
               for i in range(0, len(mail.to), MAX_RECIPIENTS_PER_CALL)]
        return ids[0]

    def _send_one(self, mail: Mail, recipients: tuple[str, ...]) -> str:
        payload = {
            "from": self._sender,
            "to": list(recipients),
            "subject": mail.subject,
            "text": mail.text,
            "html": mail.html,
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        problem = "mail service did not answer"
        for attempt in (1, 2):  # one retry, for a timeout or a 5xx
            try:
                with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                    response = client.post(RESEND_URL, json=payload, headers=headers)
            except httpx.TransportError as exc:
                problem = f"could not reach the mail service ({type(exc).__name__})"
            else:
                if response.status_code < 300:
                    try:
                        return str(response.json()["id"])
                    except Exception as exc:
                        raise MailError("the mail service answered without a message id") from exc
                if response.status_code < 500:
                    raise MailError(f"the mail service refused the message ({_safe_reason(response)})")
                problem = f"the mail service had a problem ({_safe_reason(response)})"
            if attempt == 1:
                self._sleep(2.0)
        raise MailError(problem)
