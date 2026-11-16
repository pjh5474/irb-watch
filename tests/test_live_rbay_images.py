"""The --images part of scripts/live_rbay_check.py, offline (fake page and fetchers)."""

import hashlib
from types import SimpleNamespace

import pytest

from irb_watch.scripts import live_rbay_check as check

PAGE = """<div class="write_table"><table>
<tr><th>제목</th><td>공지</td></tr>
<tr><th>내용</th><td><div class="editor_read">
  <img src="/images/editorUpload/20260615/084145598410.png?v=1">
  <img src="https://tracker.example/pixel.gif">
  <img src="data:image/png;base64,AAAA">
</div></td></tr>
<tr><th>내용</th><td><img src="/images/editorUpload/20260101/other.png"></td></tr>
</table></div>"""
PNG = b"\x89PNG\r\n\x1a\nimage"


class FakeAdapter:
    def post_page(self, post):
        return SimpleNamespace(text=PAGE, url="https://www.r-bay.co.kr/mng/brd/view")


class FakeSession:
    def jar(self):
        return {}

    def headers(self):
        return {"User-Agent": "browser", "Referer": "https://www.r-bay.co.kr/mng/gate"}


def run(monkeypatch, capsys, answers):
    """answers: one per fetch in order (bytes, or an exception to raise)."""
    made, fetched = [], []

    class FakeFetcher:
        def __init__(self, **kw):
            made.append(kw)

        def get(self, url):
            fetched.append((len(made), url))
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return SimpleNamespace(url=url, status=200, headers={"content-type": "image/png"}, content=answer)

        def close(self):
            pass

    monkeypatch.setattr(check, "SafeFetcher", FakeFetcher)
    monkeypatch.setattr(check, "rbay_ssl_context", lambda: None)
    ok = check.check_images(FakeAdapter(), SimpleNamespace(post_id="77"), FakeSession())
    return ok, capsys.readouterr().out, made, fetched


def test_reads_the_first_body_row_fetches_only_rbay_and_prints_patterns(monkeypatch, capsys):
    ok, out, made, fetched = run(monkeypatch, capsys, [PNG, PNG, PNG])
    assert ok is True
    assert "images: 3" in out  # the first 내용 row only, as the scan reads it
    assert "/images/editorUpload/<date>/<name>.png (has a query)" in out
    assert "084145598410" not in out and "20260615" not in out  # no full addresses
    assert "images on other hosts (not fetched): tracker.example" in out
    assert all("tracker.example" not in url for _, url in fetched)
    assert "5. same bytes on both session fetches: True" in out
    # Two session fetches on one fetcher (the delay applies), then one without cookies but with the headers.
    assert [n for n, _ in fetched] == [1, 1, 2]
    assert "cookies" in made[0] and "cookies" not in made[1]
    assert made[1]["headers"]["Referer"].startswith("https://www.r-bay.co.kr")


def test_a_failed_session_fetch_is_not_compared_and_fails(monkeypatch, capsys):
    ok, out, *_ = run(monkeypatch, capsys, [RuntimeError("timeout"), PNG, PNG])
    assert ok is False
    assert "not compared" in out and "same bytes on both session fetches: True" not in out


def test_changing_bytes_are_reported(monkeypatch, capsys):
    ok, out, *_ = run(monkeypatch, capsys, [PNG, PNG + b"x", PNG])
    assert ok is True
    assert "same bytes on both session fetches: False" in out
    assert hashlib.sha256(PNG).hexdigest()[:12] in out


def test_an_html_answer_is_not_an_image(monkeypatch, capsys):
    ok, out, *_ = run(monkeypatch, capsys, [b"<html>login</html>", b"<html>login</html>", PNG])
    assert ok is False
    assert "bytes look like not an image" in out


@pytest.mark.parametrize("url,shape", [
    ("https://www.r-bay.co.kr/images/editorUpload/20260615/a.png", "https://www.r-bay.co.kr/images/editorUpload/<date>/<name>.png"),
    ("https://www.r-bay.co.kr/file/view?key=secret", "https://www.r-bay.co.kr/file/<name> (has a query)"),
])
def test_shape_hides_names_dates_and_queries(url, shape):
    assert check._shape(url) == shape
