"""Plain descriptions of a change, built without any AI.

Used for title-only and cosmetic changes, and as the fallback when the AI is unavailable.
The language follows the document: Korean if its title contains Hangul, else English.
"""

import re

from irb_watch.core.diff import ChangeDetail

_HANGUL = re.compile("[가-힣ㄱ-ㆎ]")

_TEXT = {
    "ko": {
        "title": "제목이 '{old}'에서 '{new}'(으)로 변경되었습니다.",
        "body": "본문 내용이 수정되었습니다.",
        "added": "첨부파일이 추가되었습니다: {names}.",
        "removed": "첨부파일이 삭제되었습니다: {names}.",
        "replaced": "첨부파일이 교체되었습니다: {names}.",
        "fallback": "문서가 수정되었습니다.",
    },
    "en": {
        "title": "Title changed from '{old}' to '{new}'.",
        "body": "The text of the page was edited.",
        "added": "Attachments added: {names}.",
        "removed": "Attachments removed: {names}.",
        "replaced": "Attachments replaced: {names}.",
        "fallback": "The document was edited.",
    },
}


def language_of(*texts: str) -> str:
    return "ko" if any(_HANGUL.search(t) for t in texts) else "en"


def template_summary(detail: ChangeDetail, title: str) -> str:
    words = _TEXT[language_of(title, *(detail.title_change or ()))]
    parts = []
    if detail.title_change:
        old, new = detail.title_change
        parts.append(words["title"].format(old=old, new=new))
    if detail.body_diff is not None:
        parts.append(words["body"])
    for kind in ("added", "removed", "replaced"):
        names = ", ".join(c.filename for c in detail.meaningful_file_changes if c.kind == kind)
        if names:
            parts.append(words[kind].format(names=names))
    return " ".join(parts) or words["fallback"]
