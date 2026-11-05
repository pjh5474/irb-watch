"""Word-level text diff and the description of what changed between two versions.

Works on any two versions of a document, so "what changed" can be shown between version
numbers or between two dates (see StoredDocument.version_as_of).
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

from irb_watch.core.state import FileContent, VersionContent

MAX_TOKENS = 20_000  # per side; beyond this a word-level diff gets too slow


@dataclass(frozen=True)
class TextChange:
    op: str  # 'equal' | 'insert' | 'delete' | 'replace'
    old: str
    new: str


@dataclass(frozen=True)
class TextDiff:
    changes: tuple[TextChange, ...]
    truncated: bool = False  # True when the texts were too large to diff word by word

    @property
    def changed(self) -> bool:
        return any(c.op != "equal" for c in self.changes)


def diff_text(old: str, new: str) -> TextDiff:
    """Diff two normalized texts word by word."""
    if old == new:
        return TextDiff((TextChange("equal", old, new),) if old else ())
    a, b = old.split(), new.split()
    if len(a) > MAX_TOKENS or len(b) > MAX_TOKENS:
        return TextDiff((TextChange("replace", old[:200], new[:200]),), truncated=True)
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return TextDiff(
        tuple(
            TextChange(tag, " ".join(a[i1:i2]), " ".join(b[j1:j2]))
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        )
    )


def excerpt(diff: TextDiff, context_words: int = 8) -> TextDiff:
    """Compact view for cards and AI input: unchanged stretches keep only a little context."""
    out: list[TextChange] = []
    last = len(diff.changes) - 1
    for i, change in enumerate(diff.changes):
        if change.op != "equal":
            out.append(change)
            continue
        words = change.old.split()
        has_prev, has_next = i > 0, i < last
        if has_prev and has_next:
            if len(words) <= 2 * context_words:
                kept = " ".join(words)
            else:
                kept = " ".join(words[:context_words]) + " ... " + " ".join(words[-context_words:])
        elif has_prev:
            kept = " ".join(words[:context_words]) + (" ..." if len(words) > context_words else "")
        elif has_next:
            kept = ("... " if len(words) > context_words else "") + " ".join(words[-context_words:])
        else:
            kept = " ".join(words)
        out.append(TextChange("equal", kept, kept))
    return TextDiff(tuple(out), diff.truncated)


# ---- describing a change between two versions ------------------------------------


@dataclass(frozen=True)
class FileChange:
    filename: str
    kind: str  # 'added' | 'removed' | 'replaced' | 'resaved'
    old: FileContent | None
    new: FileContent | None
    text_diff: TextDiff | None  # only when both sides have text


@dataclass(frozen=True)
class ChangeDetail:
    title_change: tuple[str, str] | None
    body_diff: TextDiff | None  # None when the page text is unchanged
    file_changes: tuple[FileChange, ...]

    @property
    def meaningful_file_changes(self) -> tuple[FileChange, ...]:
        """Everything except 'resaved' (new file bytes, identical extracted text)."""
        return tuple(f for f in self.file_changes if f.kind != "resaved")

    @property
    def only_resaved(self) -> bool:
        """Files differ only in bytes (e.g. re-exported PDF): not worth an alert."""
        return (
            self.title_change is None
            and self.body_diff is None
            and bool(self.file_changes)
            and not self.meaningful_file_changes
        )

    @property
    def title_only(self) -> bool:
        return (
            self.title_change is not None
            and self.body_diff is None
            and not self.meaningful_file_changes
        )

    @property
    def suggested_priority(self) -> str | None:
        """Rule-based floor; the AI step may raise it. None means 'let the AI decide'."""
        return "low" if self.title_only else None


def describe_change(old: VersionContent, new: VersionContent) -> ChangeDetail:
    title_change = (old.title, new.title) if old.title != new.title else None
    body_diff = None
    if old.body_text != new.body_text:
        body_diff = diff_text(old.body_text, new.body_text)

    old_files = {f.filename: f for f in old.files}
    new_files = {f.filename: f for f in new.files}
    changes: list[FileChange] = []
    for name in sorted(old_files.keys() | new_files.keys()):
        before, after = old_files.get(name), new_files.get(name)
        if before is None:
            changes.append(FileChange(name, "added", None, after, None))
        elif after is None:
            changes.append(FileChange(name, "removed", before, None, None))
        elif before.sha256 != after.sha256:
            both_text = before.text is not None and after.text is not None
            if both_text and before.text == after.text:
                changes.append(FileChange(name, "resaved", before, after, None))
            else:
                text_diff = diff_text(before.text, after.text) if both_text else None
                changes.append(FileChange(name, "replaced", before, after, text_diff))
    return ChangeDetail(title_change, body_diff, tuple(changes))
