"""Write a template draft's answers into a copy of its DOCX form. No I/O.

The form's own layout (tables, fonts, logos, headers) is kept: only text is put in the places
form_fields.py found, and picked tick boxes are swapped for ticked ones.
  * A field with no answer at all is written as "[확인 필요: label]"; an answer the user left
    empty on purpose stays empty.
  * A marker field the admin removed from the list is replaced by nothing, so no "{{...}}"
    is left in the file.
"""

import copy
import io

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from irb_watch.docgen.form_fields import (
    MARKER,
    TICKED,
    UNDERSCORES,
    find_slots,
    open_docx,
    own_texts,
    para_text,
    parts,
)
from irb_watch.docgen.types import FieldValue, FormField, picked_list

_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_MARK_ONLY = frozenset(qn(f"w:{t}") for t in ("ins", "del", "moveFrom", "moveTo", "rPrChange"))


def placeholder(label: str) -> str:
    return f"[확인 필요: {label}]"


def _preserve(t) -> None:
    t.set(_SPACE, "preserve")


def _put(p, start: int, end: int, value: str) -> None:
    """Replace characters start..end of the paragraph's text with value (lines become line
    breaks). The replaced text may be spread over several runs; the first one keeps its look."""
    ts = own_texts(p)
    spans, pos = [], 0
    for t in ts:
        n = len(t.text or "")
        spans.append((t, pos, pos + n))
        pos += n
    first = next(i for i, (_, a, b) in enumerate(spans) if a <= start < b)
    last = next(i for i, (_, a, b) in enumerate(spans) if a < end <= b)
    t0, a0, _ = spans[first]
    tl, al, _ = spans[last]
    head = (t0.text or "")[: start - a0]
    tail = (tl.text or "")[end - al :]
    for i in range(first + 1, last + 1):
        spans[i][0].text = ""
    lines = value.split("\n")
    t0.text = head + lines[0]
    _preserve(t0)
    prev = t0
    for line in lines[1:]:
        br = OxmlElement("w:br")
        prev.addnext(br)
        nt = OxmlElement("w:t")
        nt.text = line
        _preserve(nt)
        br.addnext(nt)
        prev = nt
    if first == last:
        prev.text = (prev.text or "") + tail
    else:
        tl.text = tail
        _preserve(tl)


def _append(p, value: str) -> None:
    """Add value at the end of a paragraph, in the paragraph's own font when it has one."""
    run = OxmlElement("w:r")
    ppr = p.find(qn("w:pPr"))
    mark = ppr.find(qn("w:rPr")) if ppr is not None else None
    if mark is not None:
        rpr = copy.deepcopy(mark)
        # A paragraph mark may carry tracked-change marks, which are not allowed in a run.
        for child in list(rpr):
            if child.tag in _MARK_ONLY:
                rpr.remove(child)
        run.append(rpr)
    for n, line in enumerate(value.split("\n")):
        if n:
            run.append(OxmlElement("w:br"))
        t = OxmlElement("w:t")
        t.text = line
        _preserve(t)
        run.append(t)
    p.append(run)


def _tick(paras: list, options: tuple[str, ...], picked: list[str]) -> None:
    wanted = {i for i, o in enumerate(options) if o in picked}
    n = 0
    for p in paras:
        for t in own_texts(p):
            text = t.text or ""
            if not any(ch in TICKED for ch in text):
                continue
            chars = list(text)
            for i, ch in enumerate(chars):
                if ch in TICKED:
                    if n in wanted:
                        chars[i] = TICKED[ch]
                    n += 1
            t.text = "".join(chars)


def _text(f: FormField, values: dict[str, FieldValue]) -> str:
    if f.key not in values:
        return placeholder(f.label)
    v = values[f.key]
    return v if isinstance(v, str) else ", ".join(picked_list(v))


def fill(filename: str, content: bytes, fields: tuple[FormField, ...], values: dict[str, FieldValue]) -> bytes:
    doc = open_docx(filename, content)
    # The places are found on the untouched file, so they match the ones found at upload.
    slots = {s.where: s for s in find_slots(doc)}
    by_where = {f.where: f for f in fields if f.source == "auto"}

    # 1. Tick boxes (one character for one, so no position moves).
    for where, f in by_where.items():
        s = slots.get(where)
        if s is not None and s.kind == "choice" and f.kind == "choice":
            _tick(s.paras, f.options, picked_list(values.get(f.key)))

    # 2. Every text replacement inside existing text (markers and underscore lines) is found
    # on the file's own text FIRST, then written last-first per paragraph. Written answers are
    # never searched again, so an answer containing "{{x}}" or "____" stays as written.
    edits: dict[object, list[tuple[int, int, str]]] = {}
    for s in slots.values():
        if s.kind == "line" and s.where in by_where:
            matches = list(UNDERSCORES.finditer(para_text(s.paras[0])))
            if s.occurrence < len(matches):
                m = matches[s.occurrence]
                edits.setdefault(s.paras[0], []).append((m.start(), m.end(), _text(by_where[s.where], values)))
    markers = {f.key[2:]: f for f in fields if f.source == "marker"}
    for _, root in parts(doc):
        for p in root.iter(qn("w:p")):
            for m in MARKER.finditer(para_text(p)):
                f = markers.get(" ".join(m.group(1).split()))
                # A marker the admin removed becomes nothing.
                edits.setdefault(p, []).append((m.start(), m.end(), _text(f, values) if f is not None else ""))
    for p, spans in edits.items():
        for start, end, value in sorted(spans, reverse=True):
            _put(p, start, end, value)

    # 3. Empty cells and empty paragraphs: new text at the end, after all searching is done.
    for s in slots.values():
        if s.kind in ("cell", "para") and s.where in by_where:
            _append(s.paras[0], _text(by_where[s.where], values))

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def render_markdown(title: str, fields: tuple[FormField, ...], values: dict[str, FieldValue]) -> str:
    """A readable copy of a template draft's answers (lists, previews, search)."""
    out = [f"# {title}".rstrip()]
    for f in fields:
        out.append(f"## {f.label}")
        v = values.get(f.key)
        if f.kind == "choice":
            picked = picked_list(v)
            out.append(" ".join(("[x] " if o in picked else "[ ] ") + o for o in f.options))
        else:
            out.append(v if isinstance(v, str) and v else (placeholder(f.label) if v is None else ""))
    return "\n\n".join(x for x in out if x is not None).strip()
