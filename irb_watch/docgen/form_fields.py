"""Find the fields of a blank DOCX form. No I/O; the file arrives as bytes.

Three kinds of places are found:
  * markers: {{name}} typed by an admin (Word often splits it into several pieces of text, so
    a paragraph's pieces are joined before searching). The same name is one field, filled
    everywhere it appears (body, tables, headers, footers).
  * tick-box groups: two or more ☐/□ in one paragraph, or consecutive paragraphs that each have
    one box. The options are the texts after the boxes.
  * blanks: an empty table cell right of a label cell, or under a header cell; an empty
    paragraph after a line ending in ':'; a run of four or more underscores.

The walk over the file is deterministic, so the same file gives the same places ("where") at
fill time (form_fill.py walks it again and matches by "where").

The file is untrusted: its size, unzipped size and type are checked before it is parsed;
python-docx parses XML with resolve_entities=False (checked 2026-11-11, python-docx 1.2.0).
"""

import io
import re
import zipfile
from dataclasses import dataclass, field

from docx import Document
from docx.oxml.ns import qn

from irb_watch.docgen.types import FormField

MAX_TEMPLATE_BYTES = 5_000_000
MAX_UNZIPPED_BYTES = 30_000_000
MAX_FIELDS = 150
MAX_LABEL_CHARS = 120
LABEL_MAX = 80  # a longer cell text is content, not a label

EMPTY_BOX = chr(0x2610)  # ☐
EMPTY_SQUARE = chr(0x25A1)  # □
TICKED = {EMPTY_BOX: chr(0x2611), EMPTY_SQUARE: chr(0x25A0)}  # ☑, ■
BOXES = frozenset(TICKED)

MARKER = re.compile(r"\{\{\s*([^{}]{1,80}?)\s*\}\}")
UNDERSCORES = re.compile(r"_{4,}")
_OPTION_TRIM = " \t,/|·;:" + chr(0xB7)

P, TBL, TR, TC, T = qn("w:p"), qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:t")
SDT, SDT_CONTENT = qn("w:sdt"), qn("w:sdtContent")
_MEDIA = (qn("w:drawing"), qn("w:pict"), qn("w:object"))
_PART_NAME = re.compile(r"^/word/((?:header|footer)\d*)\.xml$")


class FormError(ValueError):
    """The file cannot be used as a form template; the message says why."""


# -- the file ---------------------------------------------------------------------------------


def check_docx(filename: str, content: bytes) -> None:
    """Refuse, with the reason, anything that is not a plain .docx within the limits."""
    ext = filename.lower().rpartition(".")[2]
    if ext in ("doc", "docm", "hwp", "hwpx", "dotx", "dotm", "rtf", "odt"):
        raise FormError(f"{filename}: form templates must be .docx; open it in Word or Hangul and save it as .docx")
    if ext != "docx":
        raise FormError(f"{filename}: form templates must be .docx files")
    if not content:
        raise FormError(f"{filename}: the file is empty")
    if len(content) > MAX_TEMPLATE_BYTES:
        raise FormError(f"{filename}: the file is too large (at most {MAX_TEMPLATE_BYTES // 1_000_000} MB)")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise FormError(f"{filename}: this is not a Word .docx file")
            if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
                raise FormError(f"{filename}: the file unpacks to more than {MAX_UNZIPPED_BYTES // 1_000_000} MB")
            types = z.read("[Content_Types].xml")
    except zipfile.BadZipFile as exc:
        # A password-protected .docx is not a zip file either.
        raise FormError(
            f"{filename}: this is not a valid .docx file (a password-protected file cannot be used)"
        ) from exc
    if b"macroEnabled" in types:
        raise FormError(f"{filename}: files with macros cannot be used; save it as a plain .docx")


def open_docx(filename: str, content: bytes):
    check_docx(filename, content)
    try:
        return Document(io.BytesIO(content))
    except Exception as exc:  # any parser failure: the file is unusable
        raise FormError(f"{filename}: the file could not be opened as a Word form ({type(exc).__name__})") from exc


def parts(doc) -> list[tuple[str, object]]:
    """(prefix, root element) of the body and of every header and footer, in a fixed order."""
    out: list[tuple[str, object]] = [("body", doc.element.body)]
    extra = []
    for part in doc.part.package.iter_parts():
        m = _PART_NAME.match(str(part.partname))
        if m:
            extra.append((m.group(1), part.element))
    return out + sorted(extra, key=lambda x: x[0])


# -- text helpers (shared with form_fill) --------------------------------------------------


def own_texts(p) -> list:
    """The w:t elements of this paragraph, not of paragraphs nested in it (text boxes)."""
    out = []
    for t in p.iter(T):
        anc = t.getparent()
        while anc is not None and anc.tag != P:
            anc = anc.getparent()
        if anc is p:
            out.append(t)
    return out


def para_text(p) -> str:
    return "".join(t.text or "" for t in own_texts(p))


def has_media(el) -> bool:
    return any(True for tag in _MEDIA for _ in el.iter(tag))


def blocks(root) -> list:
    """The paragraphs and tables of a container in order, including those inside block-level
    content controls (w:sdt), which Word uses for fill-in areas."""
    out = []
    for c in root:
        if c.tag in (P, TBL):
            out.append(c)
        elif c.tag == SDT:
            content = c.find(SDT_CONTENT)
            if content is not None:
                out.extend(blocks(content))
    return out


def _cell_paras(tc) -> list:
    return [c for c in blocks(tc) if c.tag == P]


def cell_text(tc) -> str:
    return "\n".join(para_text(p) for p in _cell_paras(tc)).strip()


def _label_like(text: str) -> bool:
    return (
        0 < len(text) <= LABEL_MAX
        and not any(b in text for b in BOXES)
        and not MARKER.search(text)
        and not UNDERSCORES.search(text)
    )


def _clean_label(text: str) -> str:
    # ':' and '(' around a blank ("기타(____)", "성명: ____") are not part of the label.
    text = " ".join(text.split()).strip(" :(" + chr(0xFF1A) + chr(0xFF08))
    return text[:MAX_LABEL_CHARS]


# -- places -------------------------------------------------------------------------------


@dataclass
class Slot:
    """A place to fill. kind: cell (empty cell), para (empty paragraph), line (underscores),
    choice (tick boxes). where: stable path. paras: the paragraphs involved (for a cell, its
    first paragraph; for choice, the group's paragraphs). occurrence: which underscore run."""

    kind: str
    where: str
    label: str
    paras: list
    options: list[str] = field(default_factory=list)
    occurrence: int = 0
    cell: object = None


@dataclass
class _Cell:
    tc: object
    path: str
    grid: int
    span: int
    vmerge: str | None  # None, "restart" or "continue"
    text: str


def _vmerge(tc) -> str | None:
    pr = tc.find(qn("w:tcPr"))
    if pr is None:
        return None
    vm = pr.find(qn("w:vMerge"))
    if vm is None:
        return None
    return "restart" if vm.get(qn("w:val")) == "restart" else "continue"


def _span(tc) -> int:
    pr = tc.find(qn("w:tcPr"))
    gs = pr.find(qn("w:gridSpan")) if pr is not None else None
    try:
        return max(1, int(gs.get(qn("w:val")))) if gs is not None else 1
    except (TypeError, ValueError):
        return 1


def _options(p_list: list) -> list[str]:
    """Text after each box, up to the next box or the end of its paragraph."""
    out: list[str] = []
    for p in p_list:
        text = para_text(p)
        idx = [i for i, ch in enumerate(text) if ch in BOXES]
        for n, i in enumerate(idx):
            end = idx[n + 1] if n + 1 < len(idx) else len(text)
            out.append(" ".join(text[i + 1 : end].split()).strip(_OPTION_TRIM)[:MAX_LABEL_CHARS])
    return [o or f"option {n + 1}" for n, o in enumerate(out)]


def _boxes(p) -> int:
    return sum(1 for ch in para_text(p) if ch in BOXES)


class _Finder:
    def __init__(self) -> None:
        self.slots: list[Slot] = []

    # A container is the body, a header/footer, or a table cell: a list of paragraphs and tables.
    def container(self, root, prefix: str, outer_label: str = "") -> None:
        items = blocks(root)
        prev_text = ""  # the text of the paragraph before (for ':' lines and box groups)
        run: list = []  # consecutive one-box paragraphs
        run_start = ""

        def close_run() -> None:
            if len(run) >= 2:
                label = run_label or outer_label or "choice"
                self._choice(run_start, label, list(run))
            run.clear()

        run_label = ""
        for i, el in enumerate(items):
            path = f"{prefix}/{i}"
            if el.tag == TBL:
                close_run()
                self.table(el, path)
                prev_text = ""
                continue
            text = para_text(el)
            boxes = _boxes(el)
            has_marker = MARKER.search(text) is not None
            if boxes == 1 and not has_marker:
                if not run:
                    run_start, run_label = path, _clean_label(prev_text) if _label_like(prev_text.strip()) else ""
                run.append(el)
                self._lines(el, path, text, outer_label, prev_text)  # "☐ 기타: ____"
                continue
            close_run()
            if boxes >= 2 and not has_marker:
                before = _clean_label(text[: min(text.index(b) for b in BOXES if b in text)])
                label = before or (_clean_label(prev_text) if _label_like(prev_text.strip()) else "") or outer_label
                self._choice(path, label or "choice", [el])
                self._lines(el, path, text, outer_label, prev_text)  # "☐ 예 ☐ 기타(____)"
            elif UNDERSCORES.search(text) and not has_marker:
                self._lines(el, path, text, outer_label, prev_text)
            elif not text.strip() and not has_media(el):
                stripped = prev_text.strip()
                if stripped.endswith((":", chr(0xFF1A))) and _label_like(stripped):
                    self.slots.append(Slot("para", path, _clean_label(stripped), [el]))
                    prev_text = ""  # only the first empty line after a label
                    continue
            prev_text = text
        close_run()

    def _lines(self, el, path: str, text: str, outer_label: str, prev_text: str) -> None:
        """Each run of underscores in the paragraph is a blank. Its label is the words between
        the previous blank or tick box and it ("서명 ____ 날짜 ____", "☐ 기타(____)")."""
        start = 0
        for n, m in enumerate(UNDERSCORES.finditer(text)):
            box = max((i for i in range(start, m.start()) if text[i] in BOXES), default=-1)
            before = _clean_label(text[max(start, box + 1) : m.start()])
            start = m.end()
            label = before or outer_label or (_clean_label(prev_text) if _label_like(prev_text.strip()) else "")
            self.slots.append(Slot("line", f"{path}#u{n}", label or "blank", [el], occurrence=n))

    def _choice(self, where: str, label: str, paras: list) -> None:
        self.slots.append(Slot("choice", where, label, paras, options=_options(paras)))

    def table(self, tbl, path: str) -> None:
        rows: list[list[_Cell]] = []
        for r, tr in enumerate(c for c in tbl if c.tag == TR):
            cells, grid = [], 0
            for c, tc in enumerate(x for x in tr if x.tag == TC):
                span = _span(tc)
                cells.append(_Cell(tc, f"{path}/r{r}/c{c}", grid, span, _vmerge(tc), cell_text(tc)))
                grid += span
            rows.append(cells)

        for r, cells in enumerate(rows):
            for c, cell in enumerate(cells):
                left = cells[c - 1].text if c > 0 else ""
                label = _clean_label(left) if c > 0 and _label_like(left) else ""
                if any(x.tag == TBL for x in blocks(cell.tc)):
                    self.container(cell.tc, cell.path, label)
                    continue
                if cell.text or has_media(cell.tc) or cell.vmerge == "continue":
                    # Not a blank; still look inside for tick boxes and underscores.
                    self.container(cell.tc, cell.path, label or self._above(rows, r, cell)[0])
                    continue
                # In the first column the nearest text above is usually another row's item, so
                # the column's top header is used there.
                above, header_row = self._above(rows, r, cell, top=c == 0)
                if label and above and c == 1 and len(cells) >= 3:
                    # A grid (item | before | after): the row's item and the column's header.
                    label = f"{label} / {above}"
                elif not label:
                    if not above:
                        continue
                    first = cells[0].text if c > 0 else ""
                    if first and _label_like(first):
                        label = f"{_clean_label(first)} / {above}"
                    else:
                        label = f"{above} {r - header_row}"
                paras = _cell_paras(cell.tc)
                if paras:
                    self.slots.append(Slot("cell", cell.path, label, paras[:1], cell=cell.tc))

    @staticmethod
    def _above(rows: list[list[_Cell]], r: int, cell: _Cell, top: bool = False) -> tuple[str, int]:
        """The nearest non-empty label cell above, in the same grid column (top: the highest)."""
        order = range(r) if top else range(r - 1, -1, -1)
        for up in order:
            same = next((x for x in rows[up] if x.grid == cell.grid), None)
            if same is None or same.vmerge == "continue":
                continue
            if same.text:
                return (_clean_label(same.text), up) if _label_like(same.text) else ("", up)
        return "", r


def find_slots(doc) -> list[Slot]:
    finder = _Finder()
    for prefix, root in parts(doc):
        finder.container(root, prefix)
    return finder.slots


def find_markers(doc) -> list[str]:
    """Marker names in order of first appearance."""
    names: list[str] = []
    for _, root in parts(doc):
        for p in root.iter(P):
            for m in MARKER.finditer(para_text(p)):
                name = " ".join(m.group(1).split())
                if name and name not in names:
                    names.append(name)
    return names


def _unique(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for label in labels:
        n = seen.get(label, 0) + 1
        seen[label] = n
        out.append(label if n == 1 else f"{label} ({n})")
    return out


def detect_fields(filename: str, content: bytes) -> tuple[FormField, ...]:
    """Every field of the form, markers first. FormError (with the reason) when the file is not
    usable, has no field, or has more than MAX_FIELDS."""
    doc = open_docx(filename, content)
    fields: list[FormField] = [
        FormField(f"m:{name}", name[:MAX_LABEL_CHARS], "text", "marker", f"marker:{name}")
        for name in find_markers(doc)
    ]
    slots = find_slots(doc)
    labels = _unique([f.label for f in fields] + [s.label for s in slots])[len(fields) :]
    for s, label in zip(slots, labels, strict=True):
        if s.kind == "choice":
            fields.append(FormField(f"c:{s.where}", label, "choice", "auto", s.where, tuple(s.options)))
        else:
            fields.append(FormField(f"a:{s.where}", label, "text", "auto", s.where))
    if not fields:
        raise FormError(
            f"{filename}: no field was found; type {{{{name}}}} markers where answers go, or leave "
            "the answer cells empty next to their labels"
        )
    if len(fields) > MAX_FIELDS:
        raise FormError(f"{filename}: the form has {len(fields)} fields (at most {MAX_FIELDS})")
    return tuple(fields)
