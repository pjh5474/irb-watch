import pytest

from irb_watch.core.hashing import sha256_hex
from irb_watch.core.models import AttachmentRef, DownloadedFile, PostDetail, PostSummary
from irb_watch.crawlers.errors import FetchError, LayoutChangedError
from irb_watch.crawlers.snuh_reader import SnuhSourceReader, iso_date
from tests.test_pdf_parser import LONG, make_pdf


def summary(post_id, title="Guideline", date="2026.09.03", secret=False):
    return PostSummary("K", post_id, title, date, secret, f"http://site/view?pstSn={post_id}")


class FakeAdapter:
    def __init__(self, summaries, details, files, list_error=None):
        self.summaries = summaries
        self.details = details  # post_id -> PostDetail or Exception
        self.files = files  # filename -> bytes or Exception
        self.list_error = list_error
        self.closed = False

    def list_board(self, board_key, max_posts):
        if self.list_error:
            raise self.list_error
        return len(self.summaries), self.summaries

    def fetch_post(self, s):
        result = self.details[s.post_id]
        if isinstance(result, Exception):
            raise result
        return result

    def download(self, ref, max_bytes):
        content = self.files[ref.filename]
        if isinstance(content, Exception):
            raise content
        return DownloadedFile(ref.filename, content, sha256_hex(content))

    def close(self):
        self.closed = True


def detail(s, body="page text", names=()):
    refs = [AttachmentRef(n, "10 KB", f"http://site/f/{n}") for n in names]
    return PostDetail(s, f"{s.title} 임상연구윤리센터 {body}", "unused", refs)


class Sink:
    def __init__(self):
        self.calls = []

    def __call__(self, sha, filename, content):
        self.calls.append((sha, filename))
        return f"stored/{sha[:8]}"


def reader(adapter):
    return SnuhSourceReader(adapter, "K", max_posts=50, max_file_bytes=1_000_000)


def test_reads_a_post_with_a_text_pdf():
    s = summary("1")
    pdf = make_pdf([LONG])
    sink = Sink()
    reading = reader(FakeAdapter([s], {"1": detail(s, names=["a.pdf"])}, {"a.pdf": pdf})).read(sink)

    [obs] = reading.observations
    [file] = obs.content.files
    assert obs.external_id == "1" and obs.posted_on == "2026-09-03"
    assert file.text and LONG in file.text and file.hash_only_reason is None
    assert file.storage_path == f"stored/{sha256_hex(pdf)[:8]}"
    assert sink.calls == [(sha256_hex(pdf), "a.pdf")]


def test_title_is_removed_from_the_body_text():
    s = summary("1", title="Unique Title X")
    [obs] = reader(FakeAdapter([s], {"1": detail(s)}, {})).read(Sink()).observations
    assert "Unique Title X" not in obs.content.body_text
    assert obs.content.title == "Unique Title X" and "page text" in obs.content.body_text


def test_title_edit_does_not_change_the_body_text():
    old, new = summary("1", title="Old title"), summary("1", title="New title")
    a = reader(FakeAdapter([old], {"1": detail(old)}, {})).read(Sink()).observations[0]
    b = reader(FakeAdapter([new], {"1": detail(new)}, {})).read(Sink()).observations[0]
    assert a.content.body_text == b.content.body_text


def test_scanned_pdf_and_old_word_file_are_hash_only_without_a_warning():
    s = summary("1")
    files = {"scan.pdf": make_pdf(["", ""]), "old.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"}
    [obs] = reader(
        FakeAdapter([s], {"1": detail(s, names=["scan.pdf", "old.doc"])}, files)
    ).read(Sink()).observations
    reasons = {f.filename: (f.text, f.hash_only_reason) for f in obs.content.files}
    assert reasons == {
        "scan.pdf": (None, "no_text_layer"),
        "old.doc": (None, "unsupported_format"),
    }
    assert obs.warnings == ()  # expected and stable: nothing for people to act on
    assert all(f.note for f in obs.content.files)  # but the reason is recorded


def test_real_hwp_attachment_is_read():
    from pathlib import Path

    real = next(Path(__file__).parent.joinpath("fixtures").glob("snuh_form_*.hwp")).read_bytes()
    s = summary("1")
    [obs] = reader(
        FakeAdapter([s], {"1": detail(s, names=["form.hwp"])}, {"form.hwp": real})
    ).read(Sink()).observations
    assert obs.content.files[0].text and "IRB No." in obs.content.files[0].text
    assert obs.warnings == ()


def test_unreadable_attachment_keeps_the_post_tracked_and_adds_a_warning():
    s = summary("1")
    corrupt_hwp = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"junk" * 100
    files = {"good.pdf": make_pdf([LONG]), "bad.hwp": corrupt_hwp}
    reading = reader(
        FakeAdapter([s], {"1": detail(s, names=["good.pdf", "bad.hwp"])}, files)
    ).read(Sink())

    assert reading.failures == {}  # the post is NOT failed
    [obs] = reading.observations
    by_name = {f.filename: f for f in obs.content.files}
    assert by_name["good.pdf"].text and by_name["bad.hwp"].text is None
    assert by_name["bad.hwp"].hash_only_reason == "unreadable"
    assert by_name["bad.hwp"].sha256 == sha256_hex(corrupt_hwp)  # still tracked by hash
    assert len(obs.warnings) == 1 and obs.warnings[0].startswith("bad.hwp: HWP is corrupt")


def test_secret_posts_are_unreadable_not_failures():
    secret, normal = summary("1", secret=True), summary("2")
    reading = reader(FakeAdapter([secret, normal], {"2": detail(normal)}, {})).read(Sink())
    assert reading.unreadable_ids == {"1"} and reading.failures == {}
    assert [o.external_id for o in reading.observations] == ["2"]


def test_one_broken_post_does_not_stop_the_others():
    a, b, c = summary("1"), summary("2"), summary("3")
    adapter = FakeAdapter(
        [a, b, c],
        {
            "1": detail(a),
            "2": LayoutChangedError("detail page 2: article.board-view not found"),
            "3": detail(c, names=["bad.pdf"]),
        },
        {"bad.pdf": b"<html>login page</html>"},
    )
    reading = reader(adapter).read(Sink())
    assert [o.external_id for o in reading.observations] == ["1"]
    assert set(reading.failures) == {"2", "3"}
    assert "article.board-view" in reading.failures["2"]
    # An HTML page served under a .pdf name is a download problem, so the post fails.
    assert "not a valid pdf" in reading.failures["3"]


def test_download_error_fails_only_that_post():
    a = summary("1")
    adapter = FakeAdapter([a], {"1": detail(a, names=["a.pdf"])}, {"a.pdf": FetchError("HTTP 500")})
    reading = reader(adapter).read(Sink())
    assert reading.observations == [] and "HTTP 500" in reading.failures["1"]


def test_list_errors_propagate_so_the_whole_scan_fails():
    adapter = FakeAdapter([], {}, {}, list_error=LayoutChangedError("list markup changed"))
    with pytest.raises(LayoutChangedError):
        reader(adapter).read(Sink())


def test_duplicate_filenames_in_one_post_are_kept_apart():
    s = summary("1")
    pdf = make_pdf([LONG])
    [obs] = reader(
        FakeAdapter([s], {"1": detail(s, names=["a.pdf", "a.pdf"])}, {"a.pdf": pdf})
    ).read(Sink()).observations
    assert [f.filename for f in obs.content.files] == ["a.pdf", "a.pdf (2)"]


def test_close_closes_the_adapter():
    adapter = FakeAdapter([], {}, {})
    reader(adapter).close()
    assert adapter.closed


@pytest.mark.parametrize(
    "site, expected", [("2026.09.03", "2026-09-03"), (" 2024.10.20 ", "2024-10-20"), ("soon", None)]
)
def test_iso_date(site, expected):
    assert iso_date(site) == expected
