"""In-memory stand-ins for the docs generator's store and AI writer."""

import itertools
from datetime import datetime

from irb_watch.core.state import FileContent, VersionContent
from irb_watch.docgen.types import (
    DocVersion,
    Draft,
    DraftVersion,
    Guide,
    SourceRef,
    VersionConflictError,
    Written,
)

NOW = datetime(2026, 11, 10, 9, 0)


class FakeDocgenStore:
    """sources: id -> (SourceRef, requires_login, members)."""

    def __init__(self, audit=None) -> None:
        self.audit = audit
        self.sources: dict[int, tuple[SourceRef, bool, set[str]]] = {}
        self.versions: dict[tuple[int, int], tuple[int, str, VersionContent]] = {}
        self.guides: dict[int, Guide] = {}
        self.links: set[tuple[int, int]] = set()  # (source_id, guide_id)
        self.drafts: dict[str, Draft] = {}
        self.draft_versions: dict[str, list[DraftVersion]] = {}
        self.prior_used = 0
        self.purged_before: datetime | None = None
        self.purge_result = 0
        self.conflicts = 0
        self._ids = itertools.count(1)

    # -- setup helpers ---------------------------------------------------------------
    def add_source(self, sid, name="Board", ai_enabled=True, requires_login=False, members=()):
        self.sources[sid] = (SourceRef(sid, name, ai_enabled), requires_login, set(members))

    def add_document(self, doc_id, source_id, title="Consent form", versions=2):
        for n in range(1, versions + 1):
            self.versions[(doc_id, n)] = (
                source_id,
                title,
                VersionContent(
                    title,
                    f"page text v{n}",
                    (FileContent("a.pdf", "1 KB", "h" * 64, f"visits: {n + 2}", None, None, None),),
                ),
            )

    def add_guide(self, gid, title="SNUH amendment guide", text="Sections: 1. Summary", sources=()):
        self.guides[gid] = Guide(gid, title, f"{title}.txt", text, "2026-11-01T00:00:00+00:00")
        for s in sources:
            self.links.add((s, gid))

    # -- protocol --------------------------------------------------------------------
    def readable_sources(self, user_id, is_admin):
        return {
            sid: ref
            for sid, (ref, login, members) in self.sources.items()
            if is_admin or not login or user_id in members
        }

    def get_source(self, source_id):
        row = self.sources.get(source_id)
        return row[0] if row else None

    def get_doc_version(self, document_id, version_no):
        numbers = [n for (d, n) in self.versions if d == document_id]
        if not numbers:
            return None
        number = max(numbers) if version_no is None else version_no
        if (document_id, number) not in self.versions:
            return None
        source_id, title, content = self.versions[(document_id, number)]
        prev = self.versions.get((document_id, number - 1))
        return DocVersion(document_id, source_id, title, number, content, prev[2] if prev else None)

    def _with_links(self, g: Guide) -> Guide:
        sids = tuple(sorted(s for s, gid in self.links if gid == g.id))
        return Guide(g.id, g.title, g.filename, g.body_text, g.created_at, g.archived_at, sids)

    def create_guide(self, title, filename, body_text, sha256, created_by, now):
        gid = max(self.guides, default=0) + 1
        self.guides[gid] = Guide(gid, title, filename, body_text, now.isoformat())
        return self._with_links(self.guides[gid])

    def list_guides(self, include_archived):
        return [
            self._with_links(g)
            for g in self.guides.values()
            if include_archived or g.archived_at is None
        ]

    def get_guides(self, guide_ids):
        return [self._with_links(self.guides[g]) for g in guide_ids if g in self.guides]

    def guides_for_sources(self, source_ids):
        ids = sorted({gid for s, gid in self.links if s in source_ids})
        return [
            self._with_links(self.guides[g]) for g in ids if self.guides[g].archived_at is None
        ]

    def archive_guide(self, guide_id, now):
        g = self.guides[guide_id]
        self.guides[guide_id] = Guide(g.id, g.title, g.filename, g.body_text, g.created_at, now.isoformat())
        self.links = {(s, gid) for s, gid in self.links if gid != guide_id}

    def link_guide(self, source_id, guide_id, now):
        self.links.add((source_id, guide_id))

    def unlink_guide(self, source_id, guide_id):
        self.links.discard((source_id, guide_id))

    def create_draft(self, new, first, now):
        did = f"00000000-0000-0000-0000-{next(self._ids):012d}"
        draft = Draft(
            did,
            new.user_id,
            new.title,
            new.instruction,
            new.document_refs,
            new.guide_ids,
            new.extra_guides,
            new.ai_override_source_ids,
            now.isoformat(),
            now.isoformat(),
        )
        self.drafts[did] = draft
        self.draft_versions[did] = [
            DraftVersion(did, 1, first.markdown, first.origin, first.instruction, now.isoformat())
        ]
        return draft

    def get_draft(self, draft_id):
        return self.drafts.get(draft_id)

    def list_drafts(self, user_id, limit):
        return [d for d in self.drafts.values() if d.user_id == user_id][:limit]

    def list_versions(self, draft_id):
        return list(self.draft_versions.get(draft_id, []))

    def add_version(self, draft, markdown, origin, instruction, now):
        if self.conflicts > 0:  # another save took the next number first
            self.conflicts -= 1
            raise VersionConflictError
        rows = self.draft_versions[draft.id]
        v = DraftVersion(draft.id, rows[-1].version_no + 1, markdown, origin, instruction, now.isoformat())
        rows.append(v)
        return v

    def delete_draft(self, draft_id):
        self.drafts.pop(draft_id, None)
        self.draft_versions.pop(draft_id, None)

    def count_requests_since(self, user_id, since):
        logged = 0
        if self.audit is not None:
            logged = sum(
                1
                for e in self.audit.entries
                if e["event_type"] in ("docgen.generate", "docgen.revise") and e["actor_id"] == user_id
            )
        return self.prior_used + logged

    def purge_before(self, cutoff):
        self.purged_before = cutoff
        return self.purge_result


class FakeWriter:
    def __init__(self, title="Amendment request", markdown="# Amendment\n\n## 1. Summary\n- change") -> None:
        self.calls: list[tuple[str, str]] = []
        self.title = title
        self.markdown = markdown
        self.fail: Exception | None = None

    def write(self, instructions, input_text):
        self.calls.append((instructions, input_text))
        if self.fail is not None:
            raise self.fail
        return Written(self.title, self.markdown)
