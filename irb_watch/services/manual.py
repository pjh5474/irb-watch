"""Manual sources: an admin uploads a hospital's documents by hand (no e-IRB site to scan).

Owner decisions 2026-11-11 (docs/decisions.md "Manual sources"):
  * Admins only. A document is created with a title, an optional note (its "page text"), a
    posted date and files; a new version starts from the current files: keep some, add or
    replace others (a new file with a kept file's name replaces it), change the title, note or
    date.
  * Saved like a scan, through the same steps: the source's scan lock, change detection, AI
    summaries when the source's AI is on, news cards, and one all-or-nothing commit_scan. The
    first upload to a new source is NOT a silent baseline: every saved document is a change.
  * Files: the same per-file limit as scans (refused here with the reason, since the admin
    chose the file), zips unpacked the same way, at most MAX_FILES files per upload. A file
    whose bytes do not match its name is refused and nothing is saved; a readable file with a
    problem (password, scanned image) is kept and the document is flagged, as for scans.
  * Removal is by hand: "removed" and "restore" make removed/reappeared cards. History stays.
  * Nothing changed (same title, note, date and files) is refused with that reason, so a
    double click adds no version.
"""

import base64
import binascii
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import NamedTuple

from irb_watch.ai.enrich import Enricher
from irb_watch.core.detect import ChangeEvent, Observation, ScanInput, detect_changes
from irb_watch.core.hashing import sha256_hex
from irb_watch.core.state import (
    CURRENT,
    NEEDS_REVIEW,
    REMOVED,
    FileContent,
    StoredDocument,
    VersionContent,
)
from irb_watch.crawlers.attachments import read_attachment, size_label
from irb_watch.crawlers.errors import ScanTooLargeError
from irb_watch.parsers.errors import UNREADABLE, ParseError
from irb_watch.services.errors import (
    ConflictError,
    NotFoundError,
    ServiceError,
    UnavailableError,
    ValidationError,
)
from irb_watch.services.scan import (
    KEEP_PAUSE,
    OK,
    STALE_SCAN_MINUTES,
    FileStorage,
    LoadedStore,
    ScanCard,
    ScanRepo,
    event_card,
    failure_text,
    make_file_sink,
    needs_review_cards,
)
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

MAX_FILES = 20  # files in one upload
MAX_UPLOAD_BYTES = 60_000_000  # all files of one upload together (they travel as base64 JSON)
MAX_TITLE_CHARS = 300
MAX_NOTE_CHARS = 20_000
MAX_NAME_CHARS = 200


@dataclass(frozen=True)
class ManualFile:
    filename: str
    content_base64: str


@dataclass(frozen=True)
class ManualResult:
    document_id: int
    # new | updated | resaved (file bytes changed, text did not) | details (only the date)
    # | removed | reappeared
    change: str
    version_no: int
    warnings: tuple[str, ...] = ()


class _Snap(NamedTuple):
    """How a document looked before this save, to tell "nothing changed" and new versions."""

    fingerprint: tuple
    posted_on: str | None
    status: str
    review_reason: str | None
    version_no: int

    @classmethod
    def of(cls, doc: StoredDocument) -> "_Snap":
        return cls(
            doc.latest.content.fingerprint(),
            doc.posted_on,
            doc.status,
            doc.review_reason,
            doc.latest.version_no,
        )


def _clean_name(filename: str) -> str:
    """The file's own name (no folders). Names never contain "/": that mark is kept for the
    files inside a zip ("<zip name>/<path inside>")."""
    return (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()[:MAX_NAME_CHARS]


def _date(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValidationError("the posted date must look like 2026-11-11") from exc


def _belongs(name: str, top: str) -> bool:
    """A file record that comes with the top-level file `top` (itself, or a file in its zip)."""
    return name == top or name.startswith(f"{top}/")


class ManualService:
    def __init__(
        self,
        repo: ScanRepo,
        storage: FileStorage,
        audit: AuditSink,
        sources: Callable[[int], Source | None],
        enricher: Enricher | None = None,
        max_file_bytes: int = 20_000_000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._storage = storage
        self._audit = audit
        self._get_source = sources
        self._enricher = enricher
        self._max_file_bytes = max_file_bytes
        self._clock = clock

    # -- entry points (the API checks that the caller is an admin) ---------------------------

    def create_document(
        self,
        actor_id: str,
        source_id: int,
        title: str,
        note: str,
        posted_on: str | None,
        files: list[ManualFile],
    ) -> ManualResult:
        source = self._source(source_id)
        clean_title = self._title(title)
        uploads = self._decode(files)
        posted = _date(posted_on)

        def change(loaded: LoadedStore) -> tuple[str, Observation | None]:
            # A double click or a retried upload must not make a second identical document
            # (two cards, two paid summaries, and cards cannot be deleted).
            wanted = (clean_title, self._note(note), frozenset((n, sha256_hex(c)) for n, c in uploads))
            for doc in loaded.store.values():
                content = doc.latest.content
                tops = frozenset((f.filename, f.sha256) for f in content.files if "/" not in f.filename)
                if doc.status != REMOVED and (content.title, content.body_text, tops) == wanted:
                    raise ConflictError(
                        "a document with the same title, note and files already exists; "
                        "add a new version to it instead"
                    )
            external_id = f"m-{secrets.token_hex(6)}"
            while external_id in loaded.store:  # 48 random bits: practically never
                external_id = f"m-{secrets.token_hex(6)}"
            records, warnings = self._read(uploads)
            content = VersionContent(clean_title, self._note(note), tuple(records))
            return external_id, Observation(external_id, "", posted, content, tuple(warnings))

        return self._save(actor_id, source, None, change)

    def add_version(
        self,
        actor_id: str,
        source_id: int,
        document_id: int,
        title: str | None,
        note: str | None,
        posted_on: str | None,
        keep_files: list[str],
        files: list[ManualFile],
    ) -> ManualResult:
        """title / note None keep the current ones; posted_on None keeps the date ("" clears
        it). keep_files: names of the current top-level files to keep (a zip keeps its files)."""
        source = self._source(source_id)
        clean_title = self._title(title) if title is not None else None
        uploads = self._decode(files)
        posted = _date(posted_on) if posted_on is not None else None

        def change(loaded: LoadedStore) -> tuple[str, Observation | None]:
            external_id = self._external_id(loaded, document_id)
            doc = loaded.store[external_id]
            if doc.status == REMOVED:
                raise ConflictError("this document is marked removed; restore it first")
            current = doc.latest.content
            tops = [f.filename for f in current.files if "/" not in f.filename]
            unknown = [n for n in keep_files if n not in tops]
            if unknown:
                raise ValidationError(f'the current version has no file named "{unknown[0]}"')
            # A new file with a kept file's name replaces it.
            replaced = {name for name, _ in uploads}
            keep = [k for k in dict.fromkeys(keep_files) if k not in replaced]
            kept = [f for f in current.files if any(_belongs(f.filename, k) for k in keep)]
            records, warnings = self._read(uploads)
            # Problems of kept files stay known (their notes are on the records).
            warnings = [
                *(f"{f.filename}: {f.note}" for f in kept if f.hash_only_reason == UNREADABLE),
                *warnings,
            ]
            content = VersionContent(
                clean_title if clean_title is not None else current.title,
                self._note(note) if note is not None else current.body_text,
                tuple(kept + records),
            )
            when = doc.posted_on if posted_on is None else posted
            return external_id, Observation(external_id, "", when, content, tuple(warnings))

        return self._save(actor_id, source, document_id, change)

    def set_removed(self, actor_id: str, source_id: int, document_id: int, removed: bool) -> ManualResult:
        """Mark a document removed (a removed card; history and files stay) or restore it."""
        source = self._source(source_id)

        def change(loaded: LoadedStore) -> tuple[str, Observation | None]:
            external_id = self._external_id(loaded, document_id)
            doc = loaded.store[external_id]
            if removed == (doc.status == REMOVED):
                raise ConflictError(
                    "this document is already marked removed" if removed else "this document is not removed"
                )
            now = self._clock()
            if removed:
                doc.status, doc.removed_at = REMOVED, now
            else:
                doc.status = NEEDS_REVIEW if doc.review_reason else CURRENT
                doc.removed_at = None
            return external_id, None

        return self._save(actor_id, source, document_id, change, removal=removed)

    # -- the save, shared ----------------------------------------------------------------------

    def _save(
        self,
        actor_id: str,
        source: Source,
        document_id: int | None,
        change: Callable[[LoadedStore], tuple[str, Observation | None]],
        removal: bool | None = None,
    ) -> ManualResult:
        now = self._clock()
        if not self._repo.claim_scan(source.id, now, STALE_SCAN_MINUTES):
            raise ConflictError("another upload to this source is being saved; try again in a moment")
        try:
            loaded = self._repo.load_store(source.id)
            before = {ext: _Snap.of(d) for ext, d in loaded.store.items()}
            external_id, obs = change(loaded)
            events: list[ChangeEvent] = []
            needs_review: dict[str, str] = {}
            if obs is not None:
                result = detect_changes(
                    loaded.store,
                    ScanInput([obs], now, is_first_scan=False, complete=False),
                )
                events, needs_review = result.events, result.needs_review
            else:
                doc = loaded.store[external_id]
                kind = "removed" if removal else "reappeared"
                events = [ChangeEvent(kind, external_id, doc.latest.content.title, doc.latest.version_no)]

            doc = loaded.store[external_id]
            previous = before.get(external_id)
            if previous == _Snap.of(doc):
                raise ConflictError("nothing changed: the title, note, date and files are the same as now")

            use_ai = self._enricher is not None and source.ai_enabled
            enrichments = self._enricher.enrich(events, loaded.store) if use_ai else {}
            cards: list[ScanCard] = [
                event_card(e, actor_id, enrichments.get(e.external_id), plain=not source.ai_enabled)
                for e in events
            ]
            cards += needs_review_cards(
                needs_review,
                {ext: (s.status, s.review_reason) for ext, s in before.items()},
                {external_id: doc.latest.content.title},
                loaded,
                actor_id,
            )
            if not events and doc.latest.version_no > (previous.version_no if previous else 0):
                change_kind = "resaved"
            elif not events:
                change_kind = "details"
            else:
                change_kind = events[0].kind
            cards.append(
                ScanCard(
                    "admin",
                    "manual.saved",
                    actor_id,
                    title=f"Manual upload to {source.name}",
                    payload={
                        "external_id": external_id,
                        "change": change_kind,
                        "version_no": doc.latest.version_no,
                        "files": [f.filename for f in doc.latest.content.files],
                    },
                    external_id=external_id,
                )
            )
            # The source's status covers every document, like a scan: any document with a
            # file problem keeps it at needs_review.
            flagged = {
                ext: d.review_reason or "needs review"
                for ext, d in loaded.store.items()
                if d.status == NEEDS_REVIEW
            }
            status, error = ("needs_review", failure_text(flagged)) if flagged else (OK, None)
            try:
                ids = self._repo.commit_scan(source.id, loaded, {external_id}, now, cards, status, error)
            except ScanTooLargeError as exc:  # a reason for the admin, not a bare 500
                raise ValidationError(f"{exc}; upload fewer or smaller files at a time") from exc
        except BaseException:
            self._release(source)
            raise
        new_id = (ids or {}).get(external_id) or loaded.doc_ids.get(external_id) or document_id
        if new_id is None:
            # Saved (commit_scan succeeded) but the id did not come back: say so, never "0".
            raise UnavailableError("the document was saved, but its id could not be read; reload the list")
        return ManualResult(
            int(new_id),
            change_kind,
            doc.latest.version_no,
            tuple(obs.warnings if obs is not None else ()),
        )

    def _release(self, source: Source) -> None:
        """Nothing was saved: free the lock and leave the source's status as it is NOW (read
        again: another upload may have changed it since this request first read the source)."""
        try:
            current = self._get_source(source.id) or source
            self._repo.release_scan(
                source.id, current.last_scan_status, current.last_scan_error, None, KEEP_PAUSE
            )
        except Exception:  # the first error is the one to report
            log.exception("could not release the upload lock of source %s", source.id)

    # -- helpers -------------------------------------------------------------------------------

    def _source(self, source_id: int) -> Source:
        source = self._get_source(source_id)
        if source is None:
            raise NotFoundError("source not found")
        if not source.is_manual:
            raise ValidationError("documents can be uploaded only to a manual source")
        if not source.is_active:
            raise ConflictError("source is inactive")
        return source

    @staticmethod
    def _external_id(loaded: LoadedStore, document_id: int) -> str:
        for external_id, doc_id in loaded.doc_ids.items():
            if doc_id == document_id:
                return external_id
        raise NotFoundError("document not found in this source")

    @staticmethod
    def _title(title: str) -> str:
        clean = (title or "").strip()
        if not clean:
            raise ValidationError("the title must not be empty")
        if len(clean) > MAX_TITLE_CHARS:
            raise ValidationError(f"the title is too long (at most {MAX_TITLE_CHARS} characters)")
        return clean

    @staticmethod
    def _note(note: str) -> str:
        clean = (note or "").replace(chr(0), "").strip()
        if len(clean) > MAX_NOTE_CHARS:
            raise ValidationError(f"the note is too long (at most {MAX_NOTE_CHARS} characters)")
        return clean

    def _decode(self, files: list[ManualFile]) -> list[tuple[str, bytes]]:
        if len(files) > MAX_FILES:
            raise ValidationError(f"upload at most {MAX_FILES} files at once")
        out: list[tuple[str, bytes]] = []
        total = 0
        limit_mb = self._max_file_bytes / 1_000_000
        for f in files:
            name = _clean_name(f.filename)
            if not name:
                raise ValidationError("every file needs a name")
            if any(name == n for n, _ in out):
                raise ValidationError(f'two files are named "{name}"; rename one')
            try:
                content = base64.b64decode(f.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValidationError(f"{name}: the file content is not valid base64") from exc
            if not content:
                raise ValidationError(f"{name}: the file is empty")
            if len(content) > self._max_file_bytes:
                raise ValidationError(f"{name}: the file is larger than {limit_mb:.0f} MB")
            total += len(content)
            if total > MAX_UPLOAD_BYTES:
                raise ValidationError(
                    f"the files together are larger than {MAX_UPLOAD_BYTES / 1_000_000:.0f} MB; "
                    "upload them in two steps"
                )
            out.append((name, content))
        return out

    def _read(self, uploads: list[tuple[str, bytes]]) -> tuple[list[FileContent], list[str]]:
        """Read every file BEFORE storing it (read_attachment does so per file); bytes that do
        not match the name are refused with the reason and the upload saves nothing."""
        sink = make_file_sink(self._repo, self._storage)
        records: list[FileContent] = []
        warnings: list[str] = []
        for name, content in uploads:
            try:
                got, problems = read_attachment(
                    name, size_label(len(content)), content, sha256_hex(content), sink, self._max_file_bytes
                )
            except ParseError as exc:
                raise ValidationError(f"{name}: {exc}") from exc
            except ServiceError:
                raise
            except Exception as exc:
                # Reading or storing failed (storage down, a parser crash): a reason, not a 500.
                log.exception("manual upload: %s could not be read or stored", name)
                raise UnavailableError(
                    f"{name}: the file could not be read or stored ({type(exc).__name__}); nothing was saved"
                ) from exc
            records.extend(got)
            warnings.extend(problems)
        return records, warnings
