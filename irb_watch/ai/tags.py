"""Defuse closing tags inside data that is wrapped in tags for a model.

Untrusted text (page and file text, guides, the admins' added text) is wrapped like
<document>...</document>. If the text itself contains the closing tag, the rest of it would sit
outside the block and read like instructions. Any spelling the model might accept counts:
upper or lower case and spaces anywhere (</DOCUMENT>, < / document >).
"""

import re
from functools import lru_cache


@lru_cache
def _pattern(tag: str) -> re.Pattern[str]:
    return re.compile(r"<\s*/\s*" + re.escape(tag) + r"\s*>", re.IGNORECASE)


def defuse(text: str, tag: str) -> str:
    """Replace every closing `tag` in `text` with a harmless [/tag]."""
    return _pattern(tag).sub(f"[/{tag}]", text)


def wrap(text: str, tag: str) -> str:
    return f"<{tag}>\n{defuse(text, tag)}\n</{tag}>"
