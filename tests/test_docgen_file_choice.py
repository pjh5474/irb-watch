"""Docs generator: choosing files of a post, the version to compare with, and changing the
inputs on a revision (owner decisions 2026-11-11)."""

import base64

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_docgen_service, get_user_store, get_verifier
from irb_watch.core.state import FileContent, VersionContent
from irb_watch.main import app
from irb_watch.services.docgen import DocChoice, DocgenService, InputChange, UploadedFile
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeWriter
from tests.test_admin_users_api import FakeVerifier

PUBLIC, OFF = 1, 3


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    audit = FakeAudit()
    store = FakeDocgenStore(audit)
    store.add_source(PUBLIC, "Public board")
    store.add_source(OFF, "AI-off board", ai_enabled=False)
    store.add_document(10, PUBLIC, "Amendment notice", versions=3, files=("protocol.pdf", "consent.hwp"))
    store.add_document(11, PUBLIC, "Other notice")
    store.add_document(30, OFF, "Secret")
    store.add_guide(1, "SNUH guide", "Guide text", hospitals=[PUBLIC])
    store.add_guide(2, "Unlinked guide", "Other")
    writer = FakeWriter()
    service = DocgenService(users, store, writer, audit, daily_limit=10, clock=lambda: NOW)
    return service, store, writer, audit


def make(service, choices, user="user-1", extra=()):
    return service.create_draft(user, list(choices), None, list(extra), "", [])


def sent(writer, n=-1) -> str:
    return writer.calls[n][1]


# -- choosing files ----------------------------------------------------------------------


def test_every_file_and_the_page_text_are_sent_by_default(env):
    service, _, writer, _ = env
    detail = make(service, [DocChoice(10)])
    text = sent(writer)
    assert "page text v3" in text
    assert "protocol.pdf visits: 5" in text and "consent.hwp visits: 5" in text
    assert "left out" not in text
    ref = detail.draft.document_refs[0]
    assert ref.files is None and ref.page_text and ref.compare


def test_only_the_chosen_files_are_sent_and_the_rest_are_named(env):
    service, _, writer, audit = env
    detail = make(service, [DocChoice(10, files=("consent.hwp",))])
    text = sent(writer)
    assert "consent.hwp visits: 5" in text
    assert "protocol.pdf visits" not in text
    assert "left out: protocol.pdf" in text
    # The list of changes covers only the chosen file as well.
    changes = text.split("CHANGES FROM VERSION 2:")[1]
    assert "consent.hwp" in changes and "protocol.pdf" not in changes
    assert detail.draft.document_refs[0].files == ("consent.hwp",)
    assert audit.entries[-1]["payload"]["documents"][0]["files"] == ["consent.hwp"]


def test_the_page_text_can_be_left_out(env):
    service, _, writer, _ = env
    make(service, [DocChoice(10, files=("protocol.pdf",), page_text=False)])
    text = sent(writer)
    assert "page text v" not in text
    assert "left out the post's page text" in text


def test_nothing_chosen_or_an_unknown_file_is_refused_with_a_reason(env):
    service, _, writer, audit = env
    with pytest.raises(ValidationError, match="choose its page text or at least one file"):
        make(service, [DocChoice(10, files=(), page_text=False)])
    with pytest.raises(ValidationError, match='no file named "missing.pdf"'):
        make(service, [DocChoice(10, files=("missing.pdf",))])
    assert writer.calls == [] and audit.entries == []


def test_the_page_text_alone_is_allowed(env):
    service, _, writer, _ = env
    make(service, [DocChoice(10, files=())])
    text = sent(writer)
    assert "page text v3" in text and "visits" not in text.split("TEXT:")[1].split("CHANGES")[0]


# -- the version to compare with -----------------------------------------------------------


def test_compare_with_an_older_version(env):
    service, _, writer, _ = env
    detail = make(service, [DocChoice(10, compare_with=1)])
    text = sent(writer)
    assert "CHANGES FROM VERSION 1:" in text and "CHANGES FROM VERSION 2:" not in text
    assert "REPLACED: [3] WITH [5]" in text  # version 1 had 'visits: 3', version 3 'visits: 5'
    assert detail.draft.document_refs[0].compare_with == 1


def test_no_comparison(env):
    service, _, writer, _ = env
    detail = make(service, [DocChoice(10, compare=False)])
    assert "asked for no comparison" in sent(writer)
    ref = detail.draft.document_refs[0]
    assert ref.compare is False and ref.compare_with is None


def test_compare_with_must_be_an_earlier_existing_version(env):
    service, store, _, _ = env
    with pytest.raises(ValidationError, match="earlier version"):
        make(service, [DocChoice(10, version_no=2, compare_with=2)])
    with pytest.raises(ValidationError, match="earlier version"):
        make(service, [DocChoice(10, version_no=2, compare_with=3)])
    del store.versions[(10, 1)]
    with pytest.raises(NotFoundError, match="no version 1"):
        make(service, [DocChoice(10, compare_with=1)])


# -- inputs per version and changing them on a revision -------------------------------------


def test_each_ai_version_records_its_inputs_and_hand_edits_do_not(env):
    service, _, _, _ = env
    detail = make(service, [DocChoice(10, files=("consent.hwp",))])
    did = detail.draft.id
    inputs = detail.versions[0].inputs
    assert inputs["documents"][0]["files"] == ["consent.hwp"]
    assert inputs["guide_ids"] == [1]
    service.save_edit("user-1", did, "# mine")
    service.revise("user-1", did, "shorter")
    versions = service.get_draft("user-1", did).versions
    assert versions[1].inputs is None
    assert versions[2].inputs == inputs  # unchanged inputs are recorded again


def test_a_revision_can_change_documents_files_and_guides(env):
    service, store, writer, audit = env
    detail = make(service, [DocChoice(10)], extra=[UploadedFile("mine.txt", b64("My guide"))])
    did = detail.draft.id
    change = InputChange(
        documents=[DocChoice(10, files=("protocol.pdf",)), DocChoice(11)],
        guide_ids=[],
        keep_extra_guides=[],
        extra_files=[UploadedFile("new.txt", b64("New guide"))],
    )
    v = service.revise("user-1", did, "use the protocol only", change=change)
    text = sent(writer)
    assert "consent.hwp visits" not in text and "protocol.pdf visits: 5" in text
    assert "Other notice" in text
    assert "Guide text" not in text and "My guide" not in text and "New guide" in text
    assert v.inputs["extra_guides"] == ["new.txt"]
    assert [d["document_id"] for d in v.inputs["documents"]] == [10, 11]
    assert [r.document_id for r in store.drafts[did].document_refs] == [10, 11]
    assert store.drafts[did].extra_guides[0].filename == "new.txt"
    entry = audit.entries[-1]
    assert entry["event_type"] == "docgen.revise" and entry["payload"]["inputs_changed"] is True
    # The next revision without a change uses the new selection.
    service.revise("user-1", did, "again")
    assert "New guide" in sent(writer) and "consent.hwp visits" not in sent(writer)


def test_kept_attached_guides_are_reused_and_unknown_names_refused(env):
    service, _, writer, _ = env
    detail = make(service, [DocChoice(10)], extra=[UploadedFile("mine.txt", b64("My guide"))])
    did = detail.draft.id
    keep = InputChange([DocChoice(10)], keep_extra_guides=["mine.txt"])
    service.revise("user-1", did, "x", change=keep)
    assert "My guide" in sent(writer)
    with pytest.raises(NotFoundError, match='no attached guide file "other.txt"'):
        service.revise("user-1", did, "x", change=InputChange([DocChoice(10)], keep_extra_guides=["other.txt"]))
    too_many = InputChange(
        [DocChoice(10)],
        keep_extra_guides=["mine.txt"],
        extra_files=[UploadedFile(f"{n}.txt", b64("g")) for n in "abc"],
    )
    with pytest.raises(ValidationError, match="at most 3"):
        service.revise("user-1", did, "x", change=too_many)


def test_a_user_cannot_add_an_unlinked_guide_on_revise_but_may_keep_one_already_used(env):
    service, store, _, _ = env
    admin_draft = service.create_draft("admin-1", [DocChoice(10)], [1, 2], [], "", [])
    service.revise("admin-1", admin_draft.draft.id, "x", change=InputChange([DocChoice(10)], [1, 2]))
    detail = make(service, [DocChoice(10)])
    with pytest.raises(NotFoundError, match="guide 2"):
        service.revise("user-1", detail.draft.id, "x", change=InputChange([DocChoice(10)], [1, 2]))
    # A guide the draft already uses stays usable after it was unlinked from the source.
    store.unlink_guide(PUBLIC, 1)
    service.revise("user-1", detail.draft.id, "x", change=InputChange([DocChoice(10)], [1]))


def test_adding_an_ai_off_document_on_revise_needs_the_box(env):
    service, _, _, _ = env
    detail = make(service, [DocChoice(10)])
    change = InputChange([DocChoice(10), DocChoice(30)])
    with pytest.raises(ConflictError, match="AI-off board"):
        service.revise("user-1", detail.draft.id, "x", change=change)
    v = service.revise("user-1", detail.draft.id, "x", [OFF], change=change)
    assert v.inputs["ai_override_source_ids"] == [OFF]


def test_an_empty_document_list_on_revise_is_refused(env):
    service, _, _, _ = env
    detail = make(service, [DocChoice(10)])
    with pytest.raises(ValidationError, match="at least one document"):
        service.revise("user-1", detail.draft.id, "x", change=InputChange([]))


def test_drafts_saved_before_file_choice_still_work(env):
    """Old rows hold only document_id, version_no and source_id."""
    from irb_watch.docgen.types import ref_from_json

    ref = ref_from_json({"document_id": 10, "version_no": 2, "source_id": 1})
    assert ref.files is None and ref.page_text and ref.compare and ref.compare_with is None


# -- the API -------------------------------------------------------------------------------


@pytest.fixture
def client(env):
    service, store, writer, audit = env
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: service._users
    app.dependency_overrides[get_docgen_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_the_api_takes_file_choices_and_new_inputs_on_revise(client, env):
    _, _, writer, _ = env
    h = auth("user-1")
    res = client.post(
        "/docgen/drafts",
        headers=h,
        json={"documents": [{"document_id": 10, "files": ["consent.hwp"], "compare_with": 1}]},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    doc = body["draft"]["documents"][0]
    assert doc["files"] == ["consent.hwp"] and doc["compare_with"] == 1 and doc["page_text"] is True
    assert body["versions"][0]["inputs"]["documents"][0]["files"] == ["consent.hwp"]
    draft_id = body["draft"]["id"]

    res = client.post(
        f"/docgen/drafts/{draft_id}/revise",
        headers=h,
        json={
            "instruction": "add the protocol",
            "inputs": {"documents": [{"document_id": 10, "compare": False}], "guide_ids": None},
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["inputs"]["documents"][0]["files"] is None
    assert "asked for no comparison" in writer.calls[-1][1]

    res = client.post(
        f"/docgen/drafts/{draft_id}/revise",
        headers=h,
        json={"instruction": "x", "inputs": {"documents": [{"document_id": 10, "files": ["nope.pdf"]}]}},
    )
    assert res.status_code == 422 and "nope.pdf" in res.json()["detail"]


# -- fixes after the code review (2026-11-11) ----------------------------------------------


def test_a_replaced_file_shows_as_removed_and_added_when_files_are_chosen(env):
    service, store, writer, _ = env
    v1 = VersionContent("Notice", "page", (FileContent("consent_v1.pdf", "1 KB", "a" * 64, "old text"),))
    v2 = VersionContent("Notice", "page", (FileContent("consent_v2.pdf", "1 KB", "b" * 64, "new text"),))
    store.versions[(40, 1)] = (PUBLIC, "Notice", v1)
    store.versions[(40, 2)] = (PUBLIC, "Notice", v2)
    make(service, [DocChoice(40, files=("consent_v2.pdf",))])
    changes = sent(writer).split("CHANGES FROM VERSION 1:")[1]
    assert "FILE REMOVED: consent_v1.pdf" in changes
    assert "FILE ADDED: consent_v2.pdf" in changes


def test_newest_and_the_newest_number_count_as_one_document(env):
    service, _, writer, _ = env
    detail = make(service, [DocChoice(10, files=("consent.hwp",)), DocChoice(10, version_no=3)])
    assert len(detail.draft.document_refs) == 1
    assert detail.draft.document_refs[0].files == ("consent.hwp",)  # the first choice wins
    assert sent(writer).count("<document>") == 1


def test_the_version_before_is_read_only_when_it_is_compared_with(env):
    service, store, _, _ = env
    make(service, [DocChoice(10, compare=False)])
    assert store.reads == 1
    store.reads = 0
    make(service, [DocChoice(10, compare_with=1)])
    assert store.reads == 2  # version 3 and version 1, not version 2
    store.reads = 0
    make(service, [DocChoice(10)])
    assert store.reads == 2


def test_the_draft_row_is_rewritten_only_when_its_selection_changed(env):
    service, store, _, _ = env
    detail = make(service, [DocChoice(10)])
    did = detail.draft.id
    service.revise("user-1", did, "shorter")
    assert store.selection_writes == 0
    service.revise("user-1", did, "x", change=InputChange([DocChoice(10, files=("consent.hwp",))]))
    assert store.selection_writes == 1


class _Query:
    def __init__(self, client, table):
        self.client, self.table, self.op = client, table, "select"

    def __getattr__(self, name):  # select, eq, order, limit: chainable
        return lambda *a, **k: self

    def insert(self, row):
        self.op, self.row = "insert", row
        return self

    def update(self, row):
        self.op = "update"
        return self

    def execute(self):
        if self.table == "drafts" and self.op == "update" and self.client.fail_update:
            raise RuntimeError("network")
        if self.op == "insert":
            return type("R", (), {"data": [{**self.row, "draft_id": self.row["draft_id"]}]})()
        return type("R", (), {"data": [{"version_no": 1}]})()


class _Client:
    fail_update = True

    def table(self, name):
        return _Query(self, name)


def test_a_failed_draft_row_update_does_not_fail_a_saved_version(caplog):
    from irb_watch.db.docgen import SupabaseDocgenStore
    from irb_watch.docgen.types import Draft, Inputs

    store = SupabaseDocgenStore(_Client())
    draft = Draft("d", "u", "t", "", (), (), (), (), "", "")
    inputs = Inputs((), (), (), ())
    v = store.add_version(draft, "# text", "ai_revision", "x", NOW, inputs, True)
    assert v.version_no == 2 and v.inputs == inputs.to_json()
    assert "draft row was not updated" in caplog.text
