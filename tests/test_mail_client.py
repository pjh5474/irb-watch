import json

import httpx
import pytest

from irb_watch.mail.client import Mail, MailError, ResendMailer

KEY = "re_SECRET_KEY_123"
MAIL = Mail(("a@example.com", "b@example.com"), "Subject", "text body", "<p>html body</p>")


def mailer(handler, sleeps=None):
    return ResendMailer(
        KEY,
        "IRB Watch <alerts@example.com>",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def test_sends_one_request_with_the_key_in_the_header_and_returns_the_id():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": "msg-1"})

    assert mailer(handler).send(MAIL) == "msg-1"
    [request] = seen
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["from"] == "IRB Watch <alerts@example.com>"
    assert body["to"] == ["a@example.com", "b@example.com"]
    assert (body["subject"], body["text"], body["html"]) == ("Subject", "text body", "<p>html body</p>")


def test_a_timeout_or_server_error_is_retried_once_then_succeeds():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200, json={"id": "msg-2"})

    assert mailer(handler, sleeps).send(MAIL) == "msg-2"
    assert len(calls) == 2 and sleeps == [2.0]


def test_two_server_errors_in_a_row_fail_with_a_safe_message():
    def handler(request):
        return httpx.Response(503, json={"message": "try later"})

    with pytest.raises(MailError, match="had a problem") as err:
        mailer(handler).send(MAIL)
    assert KEY not in str(err.value) and "a@example.com" not in str(err.value)


def test_a_refusal_is_not_retried_and_keeps_the_reason_without_addresses():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            403, json={"message": "You can only send testing emails to a@example.com. Verify a domain."}
        )

    with pytest.raises(MailError, match="refused") as err:
        mailer(handler).send(MAIL)
    assert len(calls) == 1
    text = str(err.value)
    assert "Verify a domain" in text and "<address>" in text
    assert "a@example.com" not in text and KEY not in text


def test_a_network_failure_never_leaks_the_key():
    def handler(request):
        raise httpx.ConnectError("boom with " + KEY)

    with pytest.raises(MailError) as err:
        mailer(handler).send(MAIL)
    assert KEY not in str(err.value)
    assert "ConnectError" in str(err.value)


def test_an_answer_without_an_id_is_an_error():
    with pytest.raises(MailError, match="message id"):
        mailer(lambda r: httpx.Response(200, json={})).send(MAIL)


def test_no_recipients_is_an_error():
    with pytest.raises(MailError, match="no recipients"):
        mailer(lambda r: httpx.Response(200, json={"id": "x"})).send(Mail((), "s", "t", "h"))


def test_more_than_fifty_recipients_are_split_into_several_calls():
    calls = []

    def handler(request):
        calls.append(len(json.loads(request.content)["to"]))
        return httpx.Response(200, json={"id": f"m{len(calls)}"})

    many = Mail(tuple(f"u{i}@example.com" for i in range(120)), "s", "t", "h")
    assert mailer(handler).send(many) == "m1"
    assert calls == [50, 50, 20]
