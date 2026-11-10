"""Which posts of a board a scan opens: the newest MAX_DOCS_PER_URL (owner, 2026-11-10: 50).

The whole list is still read (it is cheap: one request on R-BAY, a few pages on SNUH), but only
the newest posts are opened and their files downloaded. Posts outside the window are neither
seen nor missing: they are reported as `unreadable` to change detection, like secret posts, so
they are never counted towards removal. A post that disappears from the list itself is still
removed after 2 scans. Posts stored earlier that fall outside the window stay stored, frozen at
their last version (owner's choice).

"Newest" = posted date, then post number (both from the list), so a pinned old notice at the top
of a board does not take a place in the window.
"""

import re

from irb_watch.core.models import PostSummary


def _date_key(posted_on: str) -> str:
    """'2026.09.03' or '2026-09-03' -> '20260903'; '' when the site shows no date."""
    digits = re.sub(r"\D", "", posted_on or "")
    return digits if len(digits) == 8 else ""


def _number(post_id: str) -> int:
    return int(post_id) if post_id.isdigit() else -1


def newest(posts: list[PostSummary], limit: int) -> tuple[list[PostSummary], list[PostSummary]]:
    """(the newest `limit` posts, the rest)."""
    ordered = sorted(posts, key=lambda p: (_date_key(p.posted_on), _number(p.post_id)), reverse=True)
    return ordered[:limit], ordered[limit:]
