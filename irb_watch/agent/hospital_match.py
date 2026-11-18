"""Find hospitals by a name the user typed, forgiving of short forms and other spellings.

Users write "이대목동병원" or "국립의료원"; the catalog holds "이화의대부속목동병원" and
"국립중앙의료원". A plain substring test finds neither. Here both sides are tidied the same way
(spaces, punctuation, a few generic words and known short forms) and compared by shared pairs of
characters; the best matches come first. Pure logic: no database, no network.
"""

import re

# Short form -> the form the catalog uses. Applied to the query and to every name.
_ALIASES = (
    ("이화여자대학교의과대학", "이화의대"),
    ("이화여대", "이화의대"),
    ("이대", "이화의대"),
    ("서울대학교", "서울대"),
    ("연세대학교", "연세대"),
    ("가톨릭대학교", "가톨릭대"),
    ("가톨릭대학", "가톨릭대"),
    ("고려대학교", "고려대"),
    ("성균관대학교", "성균관대"),
    ("국립의료원", "국립중앙의료원"),
    ("엔엠씨", "국립중앙의료원"),
    ("nmc", "국립중앙의료원"),
    ("snuh", "서울대"),
)
_GENERIC = ("대학교", "부속", "병원", "의료원", "의과대학")
MIN_SCORE = 0.6  # one shared pair out of two (a shared city or university word) is not enough


def _tidy(text: str) -> str:
    text = re.sub(r"[\W_]+", "", text.lower())
    for short, full in _ALIASES:
        text = text.replace(short, full)
    return text


def _core(text: str) -> str:
    """Tidied text without the words every hospital has ('병원', '부속', ...)."""
    for word in _GENERIC:
        text = text.replace(word, "")
    return text


def _pairs(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def score(query: str, name: str) -> float:
    """0..1: how much of the query's characters appear, in pairs, in the name."""
    q, n = _tidy(query), _tidy(name)
    if not q or not n:
        return 0.0
    qc, nc = _core(q), _core(n)
    if not qc:  # only generic words ('병원', '의료원'): says nothing about which hospital
        return 0.0
    if q in n or n in q:
        return 1.0
    if qc and nc and (qc in nc or nc in qc):
        return 0.95
    qp, np_ = _pairs(qc or q), _pairs(nc or n)
    if not qp:  # one character left: only the substring test above counts
        return 0.0
    return len(qp & np_) / len(qp)


def match(query: str, hospitals: list[tuple[int, str]], limit: int) -> list[tuple[int, str]]:
    """The hospitals that fit the query, best first (ties by name), at most `limit`."""
    scored = [(score(query, name), name, hid) for hid, name in hospitals]
    good = sorted((s for s in scored if s[0] >= MIN_SCORE), key=lambda s: (-s[0], s[1]))
    return [(hid, name) for _, name, hid in good[:limit]]
