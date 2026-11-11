"""Guide library admin tools: global guides, many sites at once, rename, replace with history
(owner decisions 2026-11-11)."""

import base64

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_docgen_service, get_user_store, get_verifier
from irb_watch.main import app
from irb_watch.services.docgen import DocChoice, DocgenService, InputChange, UploadedFile
from irb_watch.services.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeWriter
from tests.test_admin_users_api import FakeVerifier

A, B, STUDY = 1, 2, 3


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("member", "m@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    audit = FakeAudit()
    store = FakeDocgenStore(audit)
    store.add_source(A, "SNUH notices")
    store.add_source(B, "SNUH forms")
    store.add_source(STUDY, "Study", requires_login=True, members={"member"})
    store.add_document(10, A)
    store.add_document(20, B)
    store.add_document(30, STUDY)
    store.add_guide(1, "SNUH guide", "Hospital sections", sources=[A])
    store.add_guide(2, "Global guide", "Global sections", is_global=True)
    store.add_guide(3, "Unused guide", "Other")
    writer = FakeWriter()
    service = DocgenService(users, store, writer, audit, daily_limit=10, clock=lambda: NOW)
    return service, store, writer, audit


# -- global guides ------------------------------------------------------------------------


def test_a_global_guide_is_a_default_for_every_source_and_goes_first(env):
    service, _, writer, _ = env
    assert [g.id for g in service.default_guides("user-1", [A])] == [2, 1]
    assert [g.id for g in service.default_guides("user-1", [B])] == [2]
    # a study's member gets it too
    assert [g.id for g in service.default_guides("member", [STUDY])] == [2]
    service.create_draft("user-1", [DocChoice(10)], None, [], "", [])
    text = writer.calls[-1][1]
    assert text.index("Global sections") < text.index("Hospital sections")
    assert "scope: global" in text and "scope: hospital, source ids: 1" in text
    assert "for other documents it does not override a global guide" in writer.calls[-1][0]


def test_scope_lines_name_each_hospital_guides_sources(env):
    """A draft over two hospitals: each hospital guide says which sources it speaks for."""
    service, store, writer, _ = env
    store.add_guide(4, "Forms guide", "Forms sections", sources=[B])
    service.create_draft("admin-1", [DocChoice(10), DocChoice(20)], [2, 1, 4, 3], [], "", [])
    text = writer.calls[-1][1]
    assert "scope: hospital, source ids: 1\n" in text
    assert "scope: hospital, source ids: 2\n" in text
    assert "scope: this draft (chosen for it)" in text  # guide 3 is linked to no source
    assert "source id: 1" in text and "source id: 2" in text  # the documents name theirs


def test_a_user_may_untick_a_global_guide(env):
    service, _, writer, _ = env
    detail = service.create_draft("user-1", [DocChoice(10)], [1], [], "", [])
    assert [g.id for g in detail.guides] == [1]
    assert "Global sections" not in writer.calls[-1][1]


def test_making_a_guide_global_and_back_keeps_its_links(env):
    service, store, _, audit = env
    g = service.update_guide("admin-1", 1, None, True)
    assert g.is_global and g.source_ids == (A,)
    assert [x.id for x in service.default_guides("user-1", [B])] == [1, 2]
    assert audit.entries[-1]["event_type"] == "docgen.guide_updated"
    assert audit.entries[-1]["payload"] == {"guide_id": 1, "is_global": True}
    g = service.update_guide("admin-1", 1, None, False)
    assert not g.is_global and g.source_ids == (A,)
    assert [x.id for x in service.default_guides("user-1", [B])] == [2]


def test_an_archived_global_guide_is_no_longer_a_default(env):
    service, _, _, _ = env
    service.archive_guide("admin-1", 2)
    assert [g.id for g in service.default_guides("user-1", [A])] == [1]
    with pytest.raises(ConflictError, match="archived"):
        service.update_guide("admin-1", 2, None, False)


def test_a_global_guide_cannot_be_linked_to_sites(env):
    service, _, _, _ = env
    with pytest.raises(ConflictError, match="global"):
        service.set_guide_sources("admin-1", 2, [A])
    with pytest.raises(ConflictError, match="global"):
        service.link_guide("admin-1", B, 2)


def test_a_draft_keeps_a_global_guide_on_revise_after_it_became_site_specific(env):
    service, _, writer, _ = env
    detail = service.create_draft("user-1", [DocChoice(20)], None, [], "", [])
    assert [g.id for g in detail.guides] == [2]
    service.update_guide("admin-1", 2, None, False)
    # the side panel sends the draft's guide list back: still allowed (the draft already uses it)
    service.revise("user-1", detail.draft.id, "x", change=InputChange([DocChoice(20)], [2]))
    assert "Global sections" in writer.calls[-1][1]


# -- many sites at once ----------------------------------------------------------------


def test_set_sources_adds_and_removes_in_one_call_with_one_entry(env):
    service, store, _, audit = env
    g = service.set_guide_sources("admin-1", 1, [B, STUDY])
    assert g.source_ids == (B, STUDY)
    entry = audit.entries[-1]
    assert entry["event_type"] == "docgen.guide_sources_set"
    assert entry["payload"] == {"guide_id": 1, "added": [B, STUDY], "removed": [A]}
    count = len(audit.entries)
    service.set_guide_sources("admin-1", 1, [STUDY, B])  # same list: nothing to do
    assert len(audit.entries) == count


def test_set_sources_refuses_unknown_sources_and_archived_guides(env):
    service, _, _, _ = env
    with pytest.raises(NotFoundError, match="source 99"):
        service.set_guide_sources("admin-1", 1, [A, 99])
    service.archive_guide("admin-1", 3)
    with pytest.raises(ConflictError, match="archived"):
        service.set_guide_sources("admin-1", 3, [A])


# -- rename, replace, history ---------------------------------------------------------------


def test_rename(env):
    service, _, _, audit = env
    assert service.update_guide("admin-1", 1, "  SNUH amendment guide ", None).title == "SNUH amendment guide"
    assert audit.entries[-1]["payload"]["old_title"] == "SNUH guide"
    with pytest.raises(ValidationError, match="title"):
        service.update_guide("admin-1", 1, "  ", None)


def test_replacing_the_file_keeps_the_old_text_in_the_history(env):
    service, store, writer, audit = env
    g = service.replace_guide_file("admin-1", 1, UploadedFile("new.txt", b64("New sections")))
    assert g.body_text == "New sections" and g.filename == "new.txt" and g.text_updated_at
    assert g.source_ids == (A,)
    history = service.guide_history("admin-1", 1)
    assert [(r.filename, r.body_text, r.replaced_by) for r in history] == [
        ("SNUH guide.txt", "Hospital sections", "admin-1")
    ]
    assert history[0].valid_from == "2026-11-01T00:00:00+00:00"
    assert audit.entries[-1]["event_type"] == "docgen.guide_replaced"
    # drafts use the current text
    service.create_draft("user-1", [DocChoice(10)], None, [], "", [])
    assert "New sections" in writer.calls[-1][1] and "Hospital sections" not in writer.calls[-1][1]


def test_the_same_file_again_is_refused_and_adds_no_history(env):
    service, store, _, audit = env
    count = len(audit.entries)
    with pytest.raises(ConflictError, match="same text"):
        service.replace_guide_file("admin-1", 1, UploadedFile("again.txt", b64("Hospital sections")))
    assert store.revisions == [] and len(audit.entries) == count


def test_a_replacement_at_the_same_moment_is_refused_with_a_reason(env):
    from irb_watch.docgen.types import VersionConflictError

    service, store, _, _ = env

    def clash(*_a, **_k):
        raise VersionConflictError

    store.replace_guide_text = clash
    with pytest.raises(ConflictError, match="someone else at the same moment"):
        service.replace_guide_file("admin-1", 1, UploadedFile("new.txt", b64("New sections")))


def test_site_changes_are_logged_before_they_are_made(env):
    """Fail closed: no audit entry, no link change."""
    from irb_watch.services.errors import AuditUnavailableError

    service, store, _, audit = env
    audit.fail_required = True
    with pytest.raises(AuditUnavailableError):
        service.set_guide_sources("admin-1", 1, [B])
    assert store.guides_for_sources([B]) == [store._with_links(store.guides[2])]  # only the global one


class _Q:
    def __init__(self, client, table):
        self.client, self.table, self.op = client, table, "select"

    def __getattr__(self, name):  # select, eq, is_, limit: chainable
        def chain(*args, **_k):
            if name in ("eq", "is_"):
                self.client.filters.append((self.table, name, *args))
            return self

        return chain

    def update(self, row):
        self.op = "update"
        return self

    def insert(self, row):
        self.op = "insert"
        self.client.inserted.append((self.table, row))
        return self

    def execute(self):
        data = []
        if self.table == "guides" and self.op == "select":
            data = [
                {
                    "filename": "old.txt",
                    "body_text": "Old",
                    "sha256": "s0",
                    "created_at": "2026-11-01T00:00:00+00:00",
                    "text_updated_at": None,
                }
            ]
        elif self.table == "guides" and self.op == "update":
            data = [{"id": 1}] if self.client.update_matches else []
        return type("R", (), {"data": data})()


class _C:
    def __init__(self, update_matches):
        self.update_matches, self.inserted, self.filters = update_matches, [], []

    def table(self, name):
        return _Q(self, name)


def test_the_store_replaces_only_the_text_it_read_and_then_keeps_the_old_one():
    from irb_watch.db.docgen import SupabaseDocgenStore
    from irb_watch.docgen.types import Guide, VersionConflictError

    guide = Guide(1, "G", "old.txt", "Old", "2026-11-01T00:00:00+00:00")
    ok = _C(update_matches=True)
    SupabaseDocgenStore(ok).replace_guide_text(guide, "new.txt", "New", "s1", "admin-1", NOW)
    assert ("guides", "eq", "sha256", "s0") in ok.filters
    assert ("guides", "is_", "text_updated_at", "null") in ok.filters
    assert [t for t, _ in ok.inserted] == ["guide_revisions"]
    assert ok.inserted[0][1]["body_text"] == "Old" and ok.inserted[0][1]["valid_from"] == "2026-11-01T00:00:00+00:00"

    clash = _C(update_matches=False)  # someone replaced it in between
    with pytest.raises(VersionConflictError):
        SupabaseDocgenStore(clash).replace_guide_text(guide, "new.txt", "New", "s1", "admin-1", NOW)
    assert clash.inserted == []  # nothing written to the history


def test_a_bad_replacement_file_changes_nothing(env):
    service, store, _, _ = env
    with pytest.raises(ValidationError):
        service.replace_guide_file("admin-1", 1, UploadedFile("x.exe", b64("x")))
    assert store.guides[1].body_text == "Hospital sections" and store.revisions == []


def test_only_admins(env):
    service, _, _, _ = env
    for call in (
        lambda: service.update_guide("user-1", 1, "x", None),
        lambda: service.set_guide_sources("user-1", 1, [A]),
        lambda: service.replace_guide_file("user-1", 1, UploadedFile("a.txt", b64("a"))),
        lambda: service.guide_history("user-1", 1),
        lambda: service.guide("user-1", 1),
    ):
        with pytest.raises(ForbiddenError):
            call()


# -- the API -------------------------------------------------------------------------------


@pytest.fixture
def client(env):
    service = env[0]
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: service._users
    app.dependency_overrides[get_docgen_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_the_admin_api(client):
    h = auth("admin-1")
    assert client.get("/admin/guides/1", headers=h).json()["body_text"] == "Hospital sections"
    res = client.patch("/admin/guides/3", headers=h, json={"title": "Renamed", "is_global": True})
    assert res.status_code == 200 and res.json()["is_global"] is True and res.json()["title"] == "Renamed"
    res = client.put("/admin/guides/1/sources", headers=h, json={"source_ids": [A, B]})
    assert res.status_code == 200 and res.json()["source_ids"] == [A, B]
    assert client.put("/admin/guides/3/sources", headers=h, json={"source_ids": [A]}).status_code == 409
    res = client.put("/admin/guides/1/file", headers=h, json={"file": {"filename": "n.md", "content_base64": b64("# New")}})
    assert res.status_code == 200 and res.json()["body_text"] == "# New"
    history = client.get("/admin/guides/1/history", headers=h).json()
    assert history[0]["body_text"] == "Hospital sections" and history[0]["chars"] == 17
    # users see the global flag on their default guides
    guides = client.get(f"/docgen/guides?source_id={B}", headers=auth("user-1")).json()
    assert [(g["id"], g["is_global"]) for g in guides] == [(2, True), (3, True), (1, False)]


def test_new_routes_are_admin_only(client):
    h = auth("user-1")
    assert client.get("/admin/guides/1", headers=h).status_code == 403
    assert client.patch("/admin/guides/1", headers=h, json={"title": "x"}).status_code == 403
    assert client.put("/admin/guides/1/sources", headers=h, json={"source_ids": []}).status_code == 403
    assert client.get("/admin/guides/1/history", headers=h).status_code == 403
