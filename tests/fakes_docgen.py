"""In-memory stand-ins for the docs generator's store and AI writer."""

import itertools
from dataclasses import replace
from datetime import datetime

from irb_watch.core.state import FileContent, VersionContent
from irb_watch.docgen.types import (
    DocVersion,
    Draft,
    DraftVersion,
    Guide,
    GuideRevision,
    Inputs,
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
        self.reads = 0  # version contents read (to check no needless reads)
        self.revisions: list[GuideRevision] = []
        self.selection_writes = 0
        self._ids = itertools.count(1)

    # -- setup helpers ---------------------------------------------------------------
    def add_source(self, sid, name="Board", ai_enabled=True, requires_login=False, members=()):
        self.sources[sid] = (SourceRef(sid, name, ai_enabled), requires_login, set(members))

    def add_document(self, doc_id, source_id, title="Consent form", versions=2, files=("a.pdf",)):
        """Each file's text is '<name> visits: <n + 2>' and changes in every version."""
        for n in range(1, versions + 1):
            self.versions[(doc_id, n)] = (
                source_id,
                title,
                VersionContent(
                    title,
                    f"page text v{n}",
                    tuple(
                        FileContent(
                            name, "1 KB", str(n) * 64, f"{name} visits: {n + 2}", None, None, None
                        )
                        for name in files
                    ),
                ),
            )

    def add_guide(
        self, gid, title="SNUH amendment guide", text="Sections: 1. Summary", sources=(), is_global=False
    ):
        self.guides[gid] = Guide(
            gid, title, f"{title}.txt", text, "2026-11-01T00:00:00+00:00", is_global=is_global
        )
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

    def get_doc_version(self, document_id, version_no, previous=True):
        numbers = [n for (d, n) in self.versions if d == document_id]
        if not numbers:
            return None
        number = max(numbers) if version_no is None else version_no
        if (document_id, number) not in self.versions:
            return None
        source_id, title, content = self.versions[(document_id, number)]
        self.reads += 1 + (1 if previous and (document_id, number - 1) in self.versions else 0)
        prev = self.versions.get((document_id, number - 1)) if previous else None
        return DocVersion(document_id, source_id, title, number, content, prev[2] if prev else None)

    def get_version_content(self, document_id, version_no):
        self.reads += 1
        row = self.versions.get((document_id, version_no))
        return row[2] if row else None

    def _with_links(self, g: Guide) -> Guide:
        sids = tuple(sorted(s for s, gid in self.links if gid == g.id))
        return replace(g, source_ids=sids)

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
        if not source_ids:
            return []
        live = [g for g in sorted(self.guides) if self.guides[g].archived_at is None]
        global_ids = [g for g in live if self.guides[g].is_global]
        linked = sorted({gid for s, gid in self.links if s in source_ids} - set(global_ids))
        return [self._with_links(self.guides[g]) for g in global_ids + [g for g in linked if g in live]]

    def list_sources(self):
        return sorted((row[0] for row in self.sources.values()), key=lambda s: s.name)

    def update_guide(self, guide_id, title, is_global):
        g = self.guides[guide_id]
        self.guides[guide_id] = replace(
            g,
            title=g.title if title is None else title,
            is_global=g.is_global if is_global is None else is_global,
        )

    def replace_guide_text(self, guide, filename, body_text, sha256, by, now):
        g = self.guides[guide.id]
        self.revisions.append(
            GuideRevision(
                len(self.revisions) + 1,
                g.id,
                g.filename,
                g.body_text,
                g.text_updated_at or g.created_at,
                by,
                now.isoformat(),
            )
        )
        self.guides[guide.id] = replace(
            g, filename=filename, body_text=body_text, text_updated_at=now.isoformat()
        )

    def list_guide_revisions(self, guide_id):
        return [r for r in reversed(self.revisions) if r.guide_id == guide_id]

    def archive_guide(self, guide_id, now):
        g = self.guides[guide_id]
        self.guides[guide_id] = replace(g, archived_at=now.isoformat())
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
        inputs = Inputs(
            new.document_refs, new.guide_ids, new.extra_guides, new.ai_override_source_ids
        )
        self.draft_versions[did] = [
            DraftVersion(
                did, 1, first.markdown, first.origin, first.instruction, now.isoformat(), inputs.to_json()
            )
        ]
        return draft

    def get_draft(self, draft_id):
        return self.drafts.get(draft_id)

    def list_drafts(self, user_id, limit):
        return [d for d in self.drafts.values() if d.user_id == user_id][:limit]

    def list_versions(self, draft_id):
        return list(self.draft_versions.get(draft_id, []))

    def add_version(self, draft, markdown, origin, instruction, now, inputs=None, update_selection=False):
        if self.conflicts > 0:  # another save took the next number first
            self.conflicts -= 1
            raise VersionConflictError
        rows = self.draft_versions[draft.id]
        v = DraftVersion(
            draft.id,
            rows[-1].version_no + 1,
            markdown,
            origin,
            instruction,
            now.isoformat(),
            inputs.to_json() if inputs is not None else None,
        )
        rows.append(v)
        if inputs is not None and update_selection:
            self.selection_writes += 1
            self.drafts[draft.id] = replace(
                self.drafts[draft.id],
                document_refs=inputs.document_refs,
                guide_ids=inputs.guide_ids,
                extra_guides=inputs.extra_guides,
                ai_override_source_ids=inputs.ai_override_source_ids,
            )
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
