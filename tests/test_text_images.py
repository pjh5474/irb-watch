"""Images in R-BAY posts (owner, 2026-11-16): parsing, fetching and storing, change rules,
cards without AI, and the page's image links (offline)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from irb_watch.ai.enrich import _images_only
from irb_watch.ai.templates import template_summary
from irb_watch.api.deps import get_download_service, get_verifier
from irb_watch.core.detect import ChangeEvent
from irb_watch.core.diff import describe_change
from irb_watch.core.models import AttachmentRef, DownloadedFile, PostSummary
from irb_watch.core.state import FileContent, VersionContent
from irb_watch.crawlers.attachments import observe_files
from irb_watch.crawlers.errors import FetchError, LoginExpiredError, TransientFetchError
from irb_watch.crawlers.rbay import MAX_TEXT_IMAGES, image_url, parse_detail
from irb_watch.main import app
from irb_watch.services.downloads import DownloadService, StoredFile
from irb_watch.services.errors import NotFoundError, TooManyRequestsError
from tests.fakes import FakeAudit, FakeStore
from tests.test_admin_users_api import FakeVerifier

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
SUMMARY = PostSummary("NOTI", "1846923", "공지", "2026-06-15", False, "https://www.r-bay.co.kr/x#NOTI-1846923")


def page(body: str, files: str = "") -> str:
    return f"""<div class="write_table"><table>
    <tr><th>제목</th><td>공지</td></tr>
    <tr><th>내용</th><td><div class="editor_read">{body}</div></td></tr>
    <tr><th>파일</th><td>{files}</td></tr></table></div>"""


# -- parsing --------------------------------------------------------------------------------------


def test_an_image_only_post_is_no_longer_an_error():
    detail = parse_detail(page('<p><img src="/images/editorUpload/20260615/084145598410.png"></p>'), SUMMARY)
    assert detail.body_text == ""
    [ref] = detail.attachments
    assert ref.inline_image and ref.filename == "084145598410.png"
    assert ref.url == "https://www.r-bay.co.kr/images/editorUpload/20260615/084145598410.png"
    assert ref.stable_id == "/images/editorUpload/20260615/084145598410.png"


def test_a_post_with_nothing_at_all_is_still_an_error():
    with pytest.raises(Exception, match="empty post"):
        parse_detail(page("<p> </p>"), SUMMARY)
    with pytest.raises(Exception, match="empty post"):  # only images R-BAY does not host
        parse_detail(page('<img src="https://cdn.example/a.png"><img src="data:image/png;base64,AA">'), SUMMARY)


def test_only_rbay_images_once_each_and_at_most_the_limit():
    imgs = "".join(f'<img src="/images/editorUpload/20260615/{i}.png">' for i in range(MAX_TEXT_IMAGES + 3))
    body = 'text <img src="/images/editorUpload/20260615/0.png"> <img src="https://cdn.example/x.png">' + imgs
    detail = parse_detail(page(body), SUMMARY)
    images = [a for a in detail.attachments if a.inline_image]
    assert len(images) == MAX_TEXT_IMAGES and detail.skipped_images == 3
    assert images[0].filename == "0.png"  # the repeated address counted once


@pytest.mark.parametrize("src,ok", [
    ("/images/editorUpload/a.png", True),
    ("https://r-bay.co.kr/images/a.png", True),
    ("http://www.r-bay.co.kr/images/a.png", False),  # not https
    ("/v1/file/fileDownload", False),  # not the editor's image folder
    ("https://www.r-bay.co.kr.evil.example/images/a.png", False),
    ("data:image/png;base64,AAAA", False),
])
def test_image_addresses(src, ok):
    assert (image_url(src) is not None) is ok


# -- fetching and storing -------------------------------------------------------------------------


def ref(name="084145598410.png"):
    return AttachmentRef(name, "", f"https://www.r-bay.co.kr/images/editorUpload/20260615/{name}",
                         stable_id=f"/images/editorUpload/20260615/{name}", inline_image=True)


def store():
    stored = {}
    return stored, lambda sha, name, content: stored.setdefault(sha, f"docs/{sha}")


def test_an_image_is_stored_without_text_and_without_a_warning():
    stored, sink = store()
    files, warnings = observe_files([ref()], lambda r, m: DownloadedFile(r.filename, PNG, "sha-png"), 1000, sink)
    [f] = files
    assert (f.hash_only_reason, f.text, f.storage_path, f.note) == ("inline_image", None, "docs/sha-png", "image in the post's text")
    assert warnings == [] and "sha-png" in stored


def test_a_broken_image_gets_a_stable_placeholder_and_a_warning():
    _, sink = store()

    def missing(r, m):
        raise FetchError("HTTP 404 for the image")

    first, warnings = observe_files([ref()], missing, 1000, sink)
    second, _ = observe_files([ref()], missing, 1000, sink)
    assert first[0].sha256 == second[0].sha256  # the same broken image is "unchanged" next scan
    assert first[0].hash_only_reason == "unreadable" and first[0].storage_path is None
    assert "image in the post's text: could not be loaded" in warnings[0]
    html, warnings = observe_files([ref()], lambda r, m: DownloadedFile(r.filename, b"<html>", "x"), 1000, sink)
    assert "not an image" in warnings[0] and html[0].sha256 == first[0].sha256


@pytest.mark.parametrize("error", [TransientFetchError("timeout"), LoginExpiredError("session ended")])
def test_a_temporary_problem_fails_the_post_instead_of_looking_like_a_change(error):
    def flaky(r, m):
        raise error

    with pytest.raises(type(error)):
        observe_files([ref()], flaky, 1000, store()[1])


def test_an_attachment_with_the_same_name_keeps_its_own_record():
    _, sink = store()
    attachment = AttachmentRef("084145598410.png", "48KB", "key-1", stable_id="key-1")
    files, _ = observe_files(
        [attachment, ref()], lambda r, m: DownloadedFile(r.filename, PNG + r.url.encode(), r.url), 1000, sink
    )
    assert [f.filename for f in files] == ["084145598410.png", "084145598410.png (2)"]


# -- change rules ---------------------------------------------------------------------------------


def image(name="a.png", sha="s1"):
    return FileContent(name, "48KB", sha, None, "inline_image", f"docs/{sha}", "image in the post's text")


PDF = FileContent("규정.pdf", "1MB", "p1", "본문", None, "docs/p1")


def content(*files, body="공지 본문"):
    return VersionContent("공지", body, tuple(files))


def test_first_images_are_a_quiet_catch_up_and_later_changes_are_cards():
    before, after = content(PDF), content(PDF, image())
    assert describe_change(before, after).first_images_only(before)
    replaced = describe_change(after, content(PDF, image(sha="s2")))
    assert replaced.images_only and not replaced.first_images_only(after)  # a later change: a card
    another = describe_change(after, content(PDF, image(), image("b.png", "s3")))
    assert not another.first_images_only(after)  # the post had images already
    with_text = describe_change(before, content(PDF, image(), body="새 본문"))
    assert not with_text.first_images_only(before)  # text changed too: a card


def test_the_catch_up_through_detect(monkeypatch):
    from irb_watch.core.detect import Observation, ScanInput, detect_changes
    from irb_watch.core.state import StoredDocument, StoredVersion

    now = datetime(2026, 11, 17, tzinfo=UTC)
    doc = StoredDocument("1", "u", "2026-06-15", now)
    doc.versions.append(StoredVersion(1, now, "baseline", content(PDF)))
    docs = {"1": doc}
    result = detect_changes(docs, ScanInput(now=now, observations=[Observation("1", "u", "2026-06-15", content(PDF, image()))]))
    assert result.events == [] and docs["1"].latest.change_type == "resaved"
    result = detect_changes(docs, ScanInput(now=now, observations=[Observation("1", "u", "2026-06-15", content(PDF, image(sha="s9")))]))
    assert [e.kind for e in result.events] == ["updated"]


def test_cards_for_images_name_them_by_count_and_send_nothing_to_the_ai():
    old, new = content(PDF, image()), content(PDF, image(sha="s2"), image("b.png", "s3"))
    detail = describe_change(old, new)
    text = template_summary(detail, "공지")
    assert "본문 이미지가 추가되었습니다 (1개)." in text and "본문 이미지가 바뀌었습니다 (1개)." in text
    assert "a.png" not in text
    assert _images_only(ChangeEvent("updated", "1", "공지", 2, detail), new)
    assert _images_only(ChangeEvent("new", "1", "공지", 1, None), content(image(), body=""))
    assert not _images_only(ChangeEvent("new", "1", "공지", 1, None), content(image()))  # has text: AI as usual
    mixed = describe_change(old, content(image(), body="새 본문"))
    assert not _images_only(ChangeEvent("updated", "1", "공지", 2, mixed), new)


# -- the page's image links -----------------------------------------------------------------------

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


class Repo:
    def __init__(self):
        self.views = 0
        self.images = [
            StoredFile(1, 7, 100, 2, "a.png", "s1", "docs/s1"),
            StoredFile(2, 7, 100, 2, "lost.png", "s2", None),
        ]

    def version_images(self, version_id):
        return (7, 100, 2, self.images) if version_id == 55 else None

    def may_read(self, user_id, source_id, now):
        return user_id == "reader"

    def count_image_views_since(self, user_id, since):
        return self.views


class Signer:
    def sign_inline(self, path, expires_in):
        return f"https://storage.example/{path}?ttl={expires_in}"


def service(repo=None, limit=1000):
    users = FakeStore()
    users.add("reader", "r@example.com")
    users.add("stranger", "s@example.com")
    audit = FakeAudit()
    return DownloadService(users, repo or Repo(), Signer(), audit, clock=lambda: NOW, image_views_per_day=limit), audit


def test_one_call_gives_inline_links_for_every_stored_image_and_one_log_entry():
    svc, audit = service()
    links, expires = svc.image_links("reader", 55)
    assert [(x.file_id, x.url) for x in links] == [(1, "https://storage.example/docs/s1?ttl=600")]
    assert (expires - NOW).total_seconds() == 600
    [entry] = audit.entries
    assert entry["event_type"] == "file.image_links" and entry["payload"]["files"] == [1]


def test_a_reader_without_access_or_an_unknown_version_gets_not_found():
    svc, audit = service()
    for user, version in (("stranger", 55), ("reader", 99)):
        with pytest.raises(NotFoundError):
            svc.image_links(user, version)
    assert audit.entries == []


def test_its_own_daily_limit_and_no_log_when_there_is_nothing_to_show():
    repo = Repo()
    repo.views = 5
    svc, _ = service(repo, limit=5)
    with pytest.raises(TooManyRequestsError, match="image views"):
        svc.image_links("reader", 55)
    repo.images = [replace(repo.images[1])]  # nothing stored
    svc, audit = service(repo, limit=5)
    assert svc.image_links("reader", 55)[0] == [] and audit.entries == []


def test_the_route():
    svc, _ = service()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_download_service] = lambda: svc
    try:
        client = TestClient(app)
        answer = client.post("/files/versions/55/image-links", headers={"Authorization": "Bearer tok-reader"})
        assert answer.status_code == 200, answer.text
        assert answer.json()["images"][0]["file_id"] == 1
        assert client.post("/files/versions/99/image-links", headers={"Authorization": "Bearer tok-reader"}).status_code == 404
    finally:
        app.dependency_overrides.clear()
