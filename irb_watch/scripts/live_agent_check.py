"""A live check of the assistant on the DEPLOYED server, run by the owner in their own terminal.

    uv run python -m irb_watch.scripts.live_agent_check
    uv run python -m irb_watch.scripts.live_agent_check --api https://irb-api.warwarsn.online

It asks for an email and a password (typed hidden, never stored or printed), signs in through
POST /auth/token, and then checks the assistant end to end: that answers really stream (they
arrive piece by piece, not all at the end), that the question and answer are saved, that a bad
token is refused, and, for an admin, that "scan all" comes back as a proposal and is NOT run.

It writes one conversation (and uses 2 or 3 of the account's daily assistant messages) and
deletes the conversation at the end. The audit-log entries stay (the log is append-only).
Nothing secret is printed: no password, no token.
"""

import argparse
import getpass
import json
import sys
import time

import httpx

DEFAULT_API = "https://irb-api.warwarsn.online"
ORIGIN = "https://irb-watch-frontend.vercel.app"


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failed += 1
        return ok

    def note(self, text: str) -> None:
        print(f"  [info] {text}")


def parse_sse(buffer: str) -> tuple[list[tuple[str, dict]], str]:
    events: list[tuple[str, dict]] = []
    blocks = buffer.replace("\r\n", "\n").split("\n\n")
    rest = blocks.pop()
    for block in blocks:
        name, data = "", ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if name and data:
            events.append((name, json.loads(data)))
    return events, rest


def stream_message(client: httpx.Client, api: str, token: str, conv: str, content: str):
    """Sends one message. Returns (status, events with arrival times, error detail)."""
    started = time.monotonic()
    out: list[tuple[float, str, dict]] = []
    with client.stream(
        "POST",
        f"{api}/agent/conversations/{conv}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": content},
        timeout=httpx.Timeout(120.0, connect=15.0),
    ) as response:
        if response.status_code != 200:
            response.read()
            try:
                detail = response.json().get("detail", "")
            except ValueError:
                detail = response.text[:200]
            return response.status_code, out, detail
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            events, buffer = parse_sse(buffer)
            now = time.monotonic() - started
            out.extend((now, name, data) for name, data in events)
    return 200, out, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=DEFAULT_API)
    api = parser.parse_args().api.rstrip("/")

    print(f"Server: {api}")
    email = input("Email: ").strip()
    password = getpass.getpass("Password (hidden): ")
    report = Report()
    client = httpx.Client()
    conv = None
    token = ""

    try:
        print("\n1. Server and login")
        health = client.get(f"{api}/health", timeout=15)
        report.check(health.status_code == 200, "GET /health", str(health.status_code))
        pre = client.options(
            f"{api}/agent/conversations/00000000-0000-0000-0000-000000000001/messages",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
            timeout=15,
        )
        report.check(
            pre.headers.get("access-control-allow-origin") == ORIGIN,
            "the web app's address is allowed to call the assistant (CORS)",
        )
        login = client.post(
            f"{api}/auth/token", data={"username": email, "password": password}, timeout=30
        )
        if login.status_code != 200:
            report.check(False, "POST /auth/token", f"{login.status_code} {login.json().get('detail', '')}")
            return 1
        token = login.json()["access_token"]
        report.check(True, "POST /auth/token gave a token", f"expires in {login.json().get('expires_in')}s")
        auth = {"Authorization": f"Bearer {token}"}

        print("\n2. Refusals")
        bad = client.get(f"{api}/agent/status", headers={"Authorization": "Bearer not-a-token"}, timeout=15)
        report.check(bad.status_code == 401, "a bad token is refused", str(bad.status_code))
        none = client.get(f"{api}/agent/status", timeout=15)
        report.check(none.status_code == 401, "no token is refused", str(none.status_code))

        print("\n3. Status")
        status = client.get(f"{api}/agent/status", headers=auth, timeout=15)
        report.check(status.status_code == 200, "GET /agent/status", str(status.status_code))
        info = status.json()
        report.note(f"enabled={info.get('enabled')} used={info.get('used')} limit={info.get('limit')} reason={info.get('reason')}")
        if not info.get("enabled"):
            report.check(False, "the assistant is enabled for this account")
            return 1

        print("\n4. A question, streamed")
        created = client.post(f"{api}/agent/conversations", headers=auth, timeout=15)
        report.check(created.status_code == 201, "POST /agent/conversations", str(created.status_code))
        conv = created.json()["id"]

        status_code, events, detail = stream_message(
            client, api, token, conv, "Which sources can you see? Answer in one short line."
        )
        report.check(status_code == 200, "the message was accepted", f"{status_code} {detail}")
        kinds = [k for _, k, _ in events]
        report.check(kinds[-1:] == ["done"], "the answer finished with a done event", str(kinds[-3:]))
        answer = "".join(d["delta"] for _, k, d in events if k == "text")
        report.check(bool(answer.strip()), "the answer has text")
        report.note(f"answer: {answer[:200]!r}")
        report.note(f"tools used: {[d['name'] for _, k, d in events if k == 'tool']}")

        print("\n5. Streaming through Caddy (a longer answer; does it arrive piece by piece?)")
        status_code, events, detail = stream_message(
            client,
            api,
            token,
            conv,
            "Write a numbered list of 8 short things a research coordinator should check when a "
            "consent form changes. Do not use any tools.",
        )
        report.check(status_code == 200, "the message was accepted", f"{status_code} {detail}")
        texts = [t for t, k, _ in events if k == "text"]
        if len(texts) >= 5:
            spread = texts[-1] - texts[0]
            bursts = len({round(t, 1) for t in texts})
            report.check(
                spread > 0.8 and bursts >= 4,
                "text arrives piece by piece (not all at the end)",
                f"{len(texts)} pieces over {spread:.1f}s in {bursts} separate moments",
            )
        else:
            report.check(False, "enough text pieces to judge streaming", f"only {len(texts)}")

        print("\n6. Saved history")
        history = client.get(f"{api}/agent/conversations/{conv}/messages", headers=auth, timeout=15)
        report.check(history.status_code == 200, "GET messages", str(history.status_code))
        roles = [m["role"] for m in history.json()]
        report.check(roles == ["user", "assistant", "user", "assistant"], "both questions and both answers were saved", str(roles))
        listing = client.get(f"{api}/agent/conversations", headers=auth, timeout=15).json()
        mine = next((c for c in listing if c["id"] == conv), None)
        report.check(bool(mine and mine["title"]), "the conversation has a title", repr(mine and mine["title"]))

        print("\n7. Admin only")
        limits = client.get(f"{api}/admin/agent/settings", headers=auth, timeout=15)
        if limits.status_code == 200:
            report.note(f"this account is an admin; limits now: {limits.json()}")
            status_code, events, detail = stream_message(
                client, api, token, conv, "Please scan all sources now."
            )
            actions = [d for _, k, d in events if k == "action"]
            report.check(
                [a.get("kind") for a in actions] == ["scan_all"],
                "'scan all' comes back as a proposal",
                str([a.get("kind") for a in actions]),
            )
            report.note("the proposal was NOT confirmed, so nothing was scanned")
        else:
            report.note(f"not an admin ({limits.status_code}): admin checks skipped")
            report.check(limits.status_code == 403, "a non-admin is refused the settings", str(limits.status_code))
    finally:
        if conv and token:
            gone = client.delete(
                f"{api}/agent/conversations/{conv}", headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            print(f"\nCleanup: conversation deleted -> {gone.status_code} (expected 204)")
        client.close()

    print(f"\nResult: {'ALL CHECKS PASSED' if report.failed == 0 else str(report.failed) + ' CHECK(S) FAILED'}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
