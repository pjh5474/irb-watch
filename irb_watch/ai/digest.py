"""Build the text that is sent to Jev and OpenAI.

Only what is needed leaves the server: for a change, the title change, the changed words
with a little context, and file names; for a new post or an on-demand summary, the start of
the text. Everything is capped; when something is cut, `truncated` is set and the prompt says so.
"""

from dataclasses import dataclass

from irb_watch.core.diff import ChangeDetail, TextDiff, excerpt
from irb_watch.core.state import VersionContent

MAX_CHANGE_CHARS = 12_000
MAX_NEW_POST_CHARS = 12_000
MAX_DOCUMENT_CHARS = 60_000
_CONTEXT_WORDS = 12
_MAX_FILE_START = 1_500


@dataclass(frozen=True)
class Digest:
    text: str
    truncated: bool


def _cap(lines: list[str], limit: int, truncated: bool = False) -> Digest:
    text = "\n".join(lines)
    if len(text) > limit:
        return Digest(text[:limit] + "\n[CUT: the input was shortened here]", True)
    return Digest(text, truncated)


def _render(diff: TextDiff) -> list[str]:
    lines = []
    if diff.truncated:
        lines.append("(the texts were too large to compare word by word; only their starts follow)")
    for change in excerpt(diff, _CONTEXT_WORDS).changes:
        if change.op == "equal":
            if change.old:
                lines.append(f"  unchanged: {change.old}")
        elif change.op == "replace":
            lines.append(f"- REPLACED: [{change.old}] WITH [{change.new}]")
        elif change.op == "insert":
            lines.append(f"- ADDED: [{change.new}]")
        else:
            lines.append(f"- REMOVED: [{change.old}]")
    return lines


def change_digest(detail: ChangeDetail, content: VersionContent) -> Digest:
    """The changes of an updated post, compact."""
    lines = [f"TITLE: {content.title}"]
    if detail.title_change:
        old, new = detail.title_change
        lines.append(f"TITLE CHANGED FROM [{old}] TO [{new}]")
    if detail.body_diff is not None:
        lines.append("PAGE TEXT CHANGES:")
        lines += _render(detail.body_diff)
    for change in detail.meaningful_file_changes:
        lines.append(f"FILE {change.kind.upper()}: {change.filename}")
        if change.text_diff is not None:
            lines += _render(change.text_diff)
        elif change.new is not None and change.new.text:
            lines.append(f"  start of file text: {change.new.text[:_MAX_FILE_START]}")
        else:
            file = change.new or change.old
            lines.append(f"  (no text available: {file.note or file.hash_only_reason})")
    return _cap(lines, MAX_CHANGE_CHARS)


def _document_lines(content: VersionContent, per_file_chars: int | None) -> list[str]:
    lines = [f"TITLE: {content.title}", "PAGE TEXT:", content.body_text]
    for file in content.files:
        lines.append(f"FILE: {file.filename}")
        if file.text:
            lines.append(file.text if per_file_chars is None else file.text[:per_file_chars])
        else:
            lines.append(f"(no text available: {file.note or file.hash_only_reason})")
    return lines


def new_post_digest(content: VersionContent) -> Digest:
    """A post seen for the first time: its title, the start of its text, and its files."""
    return _cap(_document_lines(content, _MAX_FILE_START), MAX_NEW_POST_CHARS)


def document_digest(content: VersionContent) -> Digest:
    """The whole current document, for a summary requested by a user."""
    return _cap(_document_lines(content, None), MAX_DOCUMENT_CHARS)
