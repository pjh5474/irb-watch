import base64

import pytest

from irb_watch.docgen.docx_build import build_docx
from irb_watch.parsers.docx import parse_docx
from irb_watch.services.docgen import (
    MAX_DOCUMENTS,
    MAX_EXTRA_GUIDES,
    DocChoice,
    DocgenService,
    UploadedFile,
    docx_filename,
    read_guide_file,
)
from irb_watch.services.errors import (
    AuditUnavailableError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeWriter

PUBLIC, STUDY, OFF = 1, 2, 3


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("member", "m@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    users.add("gone", "g@example.com", deleted_at="2026-11-01T00:00:00+00:00")
    audit = FakeAudit()
    store = FakeDocgenStore(audit)
    store.add_source(PUBLIC, "Public board")
    store.add_source(STUDY, "Study board", requires_login=True, members={"member"})
    store.add_source(OFF, "AI-off study", ai_enabled=False, requires_login=True, members={"member"})
    store.add_document(10, PUBLIC, "Consent form")
    store.add_document(20, STUDY, "Protocol")
    store.add_document(30, OFF, "Secret protocol")
    store.add_guide(1, "SNUH guide", "Sections: summary, changes", sources=[PUBLIC])
    store.add_guide(2, "Study guide", "Study sections", sources=[STUDY])
    store.add_guide(3, "Unlinked guide", "Other")
    writer = FakeWriter()
    service = DocgenService(users, store, writer, audit, daily_limit=3, clock=lambda: NOW)
    return service, store, writer, audit


def make(service, user="user-1", docs=((10, None),), **kw):
    return service.create_draft(
        user,
        [DocChoice(d, v) for d, v in docs],
        kw.get("guide_ids"),
        kw.get("extra", []),
        kw.get("instruction", ""),
        kw.get("override", []),
    )


# -- creating a draft ---------------------------------------------------------------


def test_a_draft_uses_the_sources_guides_and_the_newest_version(env):
    service, store, writer, audit = env
    detail = make(service, instruction="Visits changed")
    assert detail.draft.title == "Amendment request"
    assert [g.id for g in detail.guides] == [1]
    assert detail.draft.document_refs[0].version_no == 2
    assert [v.origin for v in detail.versions] == ["ai"]
    instructions, text = writer.calls[0]
    assert "<guide>" in text and "Sections: summary, changes" in text
    assert "CHANGES FROM VERSION 1" in text and "Visits changed" in text
    entry = audit.entries[-1]
    assert entry["event_type"] == "docgen.generate"
    assert entry["payload"]["guide_ids"] == [1]
    assert entry["payload"]["ai_override_source_ids"] == []


def test_the_user_may_leave_out_a_default_guide(env):
    service, _, writer, _ = env
    detail = make(service, guide_ids=[])
    assert detail.guides == []
    assert "(No guide was given.)" in writer.calls[0][1]


def test_a_user_cannot_add_an_unlinked_library_guide_but_an_admin_can(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        make(service, guide_ids=[1, 3])
    detail = make(service, user="admin-1", guide_ids=[1, 3])
    assert [g.id for g in detail.guides] == [1, 3]


def test_extra_guide_files_are_read_and_sent(env):
    service, store, writer, _ = env
    detail = make(service, extra=[UploadedFile("my guide.md", b64("# Use table of changes"))])
    assert "Use table of changes" in writer.calls[0][1]
    assert detail.draft.extra_guides[0].filename == "my guide.md"


def test_a_study_document_is_not_found_for_a_non_member(env):
    service, _, writer, audit = env
    with pytest.raises(NotFoundError):
        make(service, docs=[(20, None)])
    assert writer.calls == [] and audit.entries == []


def test_a_member_can_use_a_study_document(env):
    service, *_ = env
    detail = make(service, user="member", docs=[(20, 1)])
    assert detail.draft.document_refs[0].version_no == 1
    assert [g.id for g in detail.guides] == [2]


def test_an_ai_off_source_needs_the_ticked_box(env):
    service, _, writer, audit = env
    with pytest.raises(ConflictError, match="AI-off study"):
        make(service, user="member", docs=[(30, None)])
    assert writer.calls == []
    detail = make(service, user="member", docs=[(30, None)], override=[OFF])
    assert detail.draft.ai_override_source_ids == (OFF,)
    assert audit.entries[-1]["payload"]["ai_override_source_ids"] == [OFF]


def test_ticking_an_unrelated_source_is_ignored(env):
    service, *_ = env
    detail = make(service, override=[OFF])
    assert detail.draft.ai_override_source_ids == ()


def test_unknown_document_or_version(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        make(service, docs=[(999, None)])
    with pytest.raises(NotFoundError):
        make(service, docs=[(10, 7)])


def test_size_limits(env):
    service, *_ = env
    with pytest.raises(ValidationError):
        make(service, docs=[])
    with pytest.raises(ValidationError):
        make(service, docs=[(i, None) for i in range(MAX_DOCUMENTS + 1)])
    files = [UploadedFile(f"g{i}.txt", b64("x")) for i in range(MAX_EXTRA_GUIDES + 1)]
    with pytest.raises(ValidationError):
        make(service, extra=files)
    with pytest.raises(ValidationError):
        make(service, instruction="x" * 2_001)


def test_the_daily_limit_counts_drafts_and_revisions(env):
    service, store, writer, _ = env
    draft = make(service).draft
    service.revise("user-1", draft.id, "shorter")
    make(service)
    with pytest.raises(TooManyRequestsError):
        make(service)
    with pytest.raises(TooManyRequestsError):
        service.revise("user-1", draft.id, "again")
    assert len(writer.calls) == 3


def test_the_audit_entry_is_written_before_the_model_call(env):
    service, store, writer, audit = env
    audit.fail_required = True
    with pytest.raises(AuditUnavailableError):
        make(service)
    assert writer.calls == []


def test_a_model_failure_is_reported_and_nothing_is_saved(env):
    service, store, writer, _ = env
    writer.fail = RuntimeError("timeout")
    with pytest.raises(UpstreamError, match="RuntimeError"):
        make(service)
    assert store.drafts == {}


def test_without_a_model_drafts_answer_503(env):
    service, store, _, audit = env
    off = DocgenService(service._users, store, None, audit, clock=lambda: NOW)
    with pytest.raises(UnavailableError):
        make(off)
    assert off.status("user-1")["enabled"] is False


def test_a_deleted_account_is_refused(env):
    service, *_ = env
    with pytest.raises(ForbiddenError):
        make(service, user="gone")


# -- editing, revising, exporting ----------------------------------------------------


def test_hand_edits_make_new_versions_and_identical_saves_do_not(env):
    service, *_ = env
    draft = make(service).draft
    v2 = service.save_edit("user-1", draft.id, "# Mine")
    assert (v2.version_no, v2.origin) == (2, "user_edit")
    assert service.save_edit("user-1", draft.id, "# Mine").version_no == 2
    with pytest.raises(ValidationError):
        service.save_edit("user-1", draft.id, "   ")


def test_a_revision_sends_the_current_draft_and_the_request(env):
    service, store, writer, audit = env
    draft = make(service).draft
    service.save_edit("user-1", draft.id, "# My edited draft")
    v = service.revise("user-1", draft.id, "make section 2 shorter")
    assert (v.version_no, v.origin, v.instruction) == (3, "ai_revision", "make section 2 shorter")
    instructions, text = writer.calls[-1]
    assert "REVISION" in instructions
    assert "<current_draft>\n# My edited draft" in text
    assert audit.entries[-1]["event_type"] == "docgen.revise"


def test_a_revision_rechecks_access(env):
    service, store, *_ = env
    draft = make(service, user="member", docs=[(20, None)]).draft
    store.sources[STUDY][2].discard("member")
    with pytest.raises(NotFoundError):
        service.revise("member", draft.id, "shorter")


def test_ai_switched_off_after_the_draft_needs_the_box_again_on_revise(env):
    service, store, writer, audit = env
    draft = make(service).draft
    ref, login, members = store.sources[PUBLIC]
    store.sources[PUBLIC] = (ref.__class__(ref.id, ref.name, False), login, members)
    with pytest.raises(ConflictError, match="Public board"):
        service.revise("user-1", draft.id, "shorter")
    v = service.revise("user-1", draft.id, "shorter", [PUBLIC])
    assert v.origin == "ai_revision"
    assert audit.entries[-1]["payload"]["ai_override_source_ids"] == [PUBLIC]


def test_an_edit_built_on_an_old_version_is_refused(env):
    service, store, *_ = env
    draft = make(service).draft
    service.revise("user-1", draft.id, "shorter")  # version 2, e.g. from another tab
    with pytest.raises(ConflictError, match="newer version"):
        service.save_edit("user-1", draft.id, "# Mine", base_version_no=1)
    assert len(store.draft_versions[draft.id]) == 2
    assert service.save_edit("user-1", draft.id, "# Mine", base_version_no=2).version_no == 3


def test_two_saves_at_once_give_a_reason_not_a_crash(env):
    service, store, *_ = env
    draft = make(service).draft
    store.conflicts = 1
    with pytest.raises(ConflictError, match="reload"):
        service.save_edit("user-1", draft.id, "# Mine")


def test_a_paid_revision_is_kept_after_one_clash_and_explained_after_two(env):
    service, store, *_ = env
    draft = make(service).draft
    store.conflicts = 1
    assert service.revise("user-1", draft.id, "shorter").version_no == 2
    store.conflicts = 2
    with pytest.raises(ConflictError, match="reload"):
        service.revise("user-1", draft.id, "shorter")


def test_the_limit_is_checked_before_attached_files_are_read(env, monkeypatch):
    service, store, *_ = env
    store.prior_used = 3  # the daily limit in this fixture
    read = []
    monkeypatch.setattr(
        "irb_watch.services.docgen.read_guide_file", lambda upload: read.append(upload)
    )
    with pytest.raises(TooManyRequestsError):
        make(service, extra=[UploadedFile("g.pdf", b64("x"))])
    assert read == []


def test_drafts_are_private(env):
    service, *_ = env
    draft = make(service).draft
    for call in (
        lambda: service.get_draft("member", draft.id),
        lambda: service.save_edit("member", draft.id, "x"),
        lambda: service.revise("member", draft.id, "x"),
        lambda: service.export_docx("member", draft.id, None),
        lambda: service.delete_draft("member", draft.id),
    ):
        with pytest.raises(NotFoundError):
            call()
    assert service.list_drafts("member") == []


def test_export_builds_a_docx_of_the_chosen_version(env):
    service, _, _, audit = env
    draft = make(service).draft
    service.save_edit("user-1", draft.id, "# Second version\n\n| item | before |\n|---|---|\n| visits | 3 |")
    content, name = service.export_docx("user-1", draft.id, 1)
    assert name == "Amendment request v1.docx"
    assert "Amendment" in parse_docx(content).text
    content, _ = service.export_docx("user-1", draft.id, None)
    assert "Second version" in parse_docx(content).text
    assert audit.entries[-1]["event_type"] == "docgen.export"
    with pytest.raises(NotFoundError):
        service.export_docx("user-1", draft.id, 9)


def test_delete_and_purge(env):
    service, store, _, audit = env
    draft = make(service).draft
    service.delete_draft("user-1", draft.id)
    assert store.drafts == {}
    store.purge_result = 2
    assert service.purge_old() == 2
    assert audit.entries[-1]["event_type"] == "docgen.purged"


# -- the admin's library ------------------------------------------------------------


def test_admin_uploads_links_and_archives_a_guide(env):
    service, store, _, audit = env
    guide = service.create_guide("admin-1", "New guide", UploadedFile("g.txt", b64("Write X")))
    assert guide.body_text == "Write X"
    service.link_guide("admin-1", PUBLIC, guide.id)
    assert guide.id in [g.id for g in service.source_guides("admin-1", PUBLIC)]
    service.unlink_guide("admin-1", PUBLIC, guide.id)
    service.link_guide("admin-1", STUDY, guide.id)
    service.archive_guide("admin-1", guide.id)
    assert guide.id not in [g.id for g in service.source_guides("admin-1", STUDY)]
    with pytest.raises(ConflictError):
        service.link_guide("admin-1", PUBLIC, guide.id)
    kinds = [e["event_type"] for e in audit.entries]
    assert kinds == [
        "docgen.guide_created",
        "docgen.guide_linked",
        "docgen.guide_unlinked",
        "docgen.guide_linked",
        "docgen.guide_archived",
    ]


def test_only_admins_manage_guides(env):
    service, *_ = env
    with pytest.raises(ForbiddenError):
        service.create_guide("user-1", "x", UploadedFile("g.txt", b64("x")))
    with pytest.raises(ForbiddenError):
        service.link_guide("user-1", PUBLIC, 1)
    with pytest.raises(ForbiddenError):
        service.list_guides("user-1", False)


def test_unknown_source_or_guide(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        service.link_guide("admin-1", 99, 1)
    with pytest.raises(NotFoundError):
        service.link_guide("admin-1", PUBLIC, 99)
    with pytest.raises(NotFoundError):
        service.unlink_guide("admin-1", STUDY, 1)


def test_users_see_default_guides_only_for_sources_they_read(env):
    service, *_ = env
    assert [g.id for g in service.default_guides("user-1", [PUBLIC, STUDY])] == [1]
    assert [g.id for g in service.default_guides("member", [PUBLIC, STUDY])] == [1, 2]


# -- guide files ----------------------------------------------------------------------


def test_guide_files_are_read_or_refused_with_a_reason():
    name, text, sha = read_guide_file(UploadedFile("C:\\x\\guide.txt", b64("  hello ")))
    assert (name, text, len(sha)) == ("guide.txt", "hello", 64)
    docx = base64.b64encode(build_docx("# Amendment guide\n\n- write the summary first")).decode()
    assert "Amendment guide" in read_guide_file(UploadedFile("g.docx", docx))[1]
    for upload, reason in [
        (UploadedFile("g.exe", b64("x")), "must be one of"),
        (UploadedFile("g.txt", "not base64!"), "not valid base64"),
        (UploadedFile("g.txt", ""), "empty"),
        (UploadedFile("g.txt", b64("   ")), "no text"),
        (UploadedFile("g.pdf", b64("not a pdf")), "not a valid pdf"),
        (UploadedFile("g.txt", base64.b64encode(b"\xff\xfe\x00").decode()), "not UTF-8"),
    ]:
        with pytest.raises(ValidationError, match=reason):
            read_guide_file(upload)


def test_docx_filename_is_safe():
    assert docx_filename('a/b:c*"d', 2) == "a b c d v2.docx"
    assert docx_filename("", 1) == "draft v1.docx"
