from datetime import UTC, datetime

from irb_watch.core.diff import describe_change, diff_text, excerpt
from irb_watch.core.state import FileContent, StoredDocument, StoredVersion, VersionContent


def f(name, sha, text="some file text", size="10 KB", reason=None):
    return FileContent(name, size, sha, text, reason)


def content(title="T", body="body text", files=()):
    return VersionContent(title, body, tuple(files))


# ---- diff_text ---------------------------------------------------------------


def test_identical_texts_have_no_changes():
    assert not diff_text("a b c", "a b c").changed


def test_replace_insert_delete_are_reported_word_by_word():
    d = diff_text("the cat sat on the mat", "the dog sat on the red mat")
    ops = [(c.op, c.old, c.new) for c in d.changes if c.op != "equal"]
    assert ops == [("replace", "cat", "dog"), ("insert", "", "red")]


def test_delete():
    d = diff_text("one two three", "one three")
    assert [(c.op, c.old) for c in d.changes if c.op != "equal"] == [("delete", "two")]


def test_empty_to_text():
    assert diff_text("", "hello world").changed


def test_huge_texts_are_truncated_not_hung():
    big = " ".join(["w"] * 25_000)
    d = diff_text(big, big + " extra")
    assert d.truncated and d.changed


def test_excerpt_keeps_only_context_around_changes():
    old = " ".join(f"a{i}" for i in range(40)) + " X " + " ".join(f"b{i}" for i in range(40))
    new = old.replace(" X ", " Y ")
    small = excerpt(diff_text(old, new), context_words=3)
    texts = [c.old for c in small.changes if c.op == "equal"]
    assert texts[0].startswith("...") and texts[0].endswith("a39")
    assert texts[1].startswith("b0") and texts[1].endswith("...")
    assert any(c.op == "replace" and (c.old, c.new) == ("X", "Y") for c in small.changes)


# ---- describe_change ---------------------------------------------------------------


def test_title_change_only():
    d = describe_change(content(title="Old"), content(title="New"))
    assert d.title_change == ("Old", "New") and d.body_diff is None and d.title_only
    assert d.suggested_priority == "low"


def test_body_change():
    d = describe_change(content(body="rev 1"), content(body="rev 2"))
    assert d.body_diff.changed and not d.title_only and d.suggested_priority is None


def test_file_added_removed_replaced():
    old = content(files=[f("keep.pdf", "s1"), f("gone.pdf", "s2"), f("edit.pdf", "s3", "v1 text")])
    new = content(files=[f("keep.pdf", "s1"), f("new.pdf", "s4"), f("edit.pdf", "s5", "v2 text")])
    kinds = {c.filename: c.kind for c in describe_change(old, new).file_changes}
    assert kinds == {"gone.pdf": "removed", "new.pdf": "added", "edit.pdf": "replaced"}


def test_replaced_file_with_text_carries_a_text_diff():
    d = describe_change(
        content(files=[f("a.pdf", "s1", "version one text")]),
        content(files=[f("a.pdf", "s2", "version two text")]),
    )
    change = d.file_changes[0]
    assert change.kind == "replaced" and change.text_diff.changed


def test_replaced_hash_only_file_has_no_text_diff():
    d = describe_change(
        content(files=[f("scan.pdf", "s1", None, reason="no_text_layer")]),
        content(files=[f("scan.pdf", "s2", None, reason="no_text_layer")]),
    )
    assert d.file_changes[0].kind == "replaced" and d.file_changes[0].text_diff is None
    assert not d.only_resaved


def test_same_text_different_bytes_is_resaved_not_meaningful():
    d = describe_change(
        content(files=[f("a.pdf", "s1", "same words")]),
        content(files=[f("a.pdf", "s2", "same words")]),
    )
    assert d.file_changes[0].kind == "resaved" and d.only_resaved
    assert d.meaningful_file_changes == ()


def test_resaved_plus_title_change_is_not_only_resaved():
    d = describe_change(
        content(title="A", files=[f("a.pdf", "s1", "same")]),
        content(title="B", files=[f("a.pdf", "s2", "same")]),
    )
    assert not d.only_resaved and d.title_only


# ---- version lookup by number and date -----------------------------------------------


def test_version_lookup_by_number_and_date():
    def at(day):
        return datetime(2026, 11, day, tzinfo=UTC)

    doc = StoredDocument("1", "http://x", None, at(1))
    for n, day in [(1, 1), (2, 10), (3, 20)]:
        doc.versions.append(StoredVersion(n, at(day), "updated", content(body=f"v{n}")))

    assert doc.get_version(2).content.body_text == "v2" and doc.get_version(9) is None
    assert doc.version_as_of(at(15)).version_no == 2
    assert doc.version_as_of(at(10)).version_no == 2
    assert doc.version_as_of(at(30)).version_no == 3
    assert doc.version_as_of(datetime(2026, 10, 1, tzinfo=UTC)) is None
