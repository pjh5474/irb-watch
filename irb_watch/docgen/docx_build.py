"""Build a plain DOCX from the Markdown of an approved draft, with no extra library.

Supported: headings (# to ######, shown as three heading sizes), paragraphs, '-', '*', '+'
bullets and '1.' numbered lines (with indentation by leading spaces), simple '|' tables,
**bold**, *italic* / _italic_, `code` (as plain text), links (text only), horizontal rules
(an empty line). Anything else is written as plain text, so nothing is lost.
The layout is neutral on purpose; hospital layouts are a later step.
"""

import io
import re
import zipfile
from xml.sax.saxutils import escape

FONT = "Malgun Gothic"  # has Hangul; Word substitutes a similar font where it is missing

# Characters XML 1.0 does not allow (control characters other than tab, newline, return).
_BAD_XML = re.compile(
    "[" + "".join(chr(c) for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)) + chr(0xFFFE)
    + chr(0xFFFF) + "]"
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|`[^`]+`|\*[^*\s][^*]*?\*|_[^_\s][^_]*?_)")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _clean(text: str) -> str:
    return _BAD_XML.sub("", text)


def _run(text: str, bold: bool = False, italic: bool = False) -> str:
    if not text:
        return ""
    props = ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "")
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(_clean(text))}</w:t></w:r>'


def _runs(text: str, bold: bool = False) -> str:
    text = _LINK.sub(r"\1", text)
    out = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if (piece.startswith("**") and piece.endswith("**") and len(piece) > 4) or (
            piece.startswith("__") and piece.endswith("__") and len(piece) > 4
        ):
            out.append(_run(piece[2:-2], bold=True))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            out.append(_run(piece[1:-1], bold=bold))
        elif (piece.startswith("*") and piece.endswith("*") and len(piece) > 2) or (
            piece.startswith("_") and piece.endswith("_") and len(piece) > 2
        ):
            out.append(_run(piece[1:-1], bold=bold, italic=True))
        else:
            out.append(_run(piece, bold=bold))
    return "".join(out)


def _paragraph(text: str, style: str | None = None, indent_level: int | None = None) -> str:
    ppr = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if indent_level is not None:
        left = 360 * (indent_level + 1)
        ppr.append(f'<w:ind w:left="{left}" w:hanging="360"/>')
    props = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
    return f"<w:p>{props}{_runs(text)}</w:p>"


def _cells(line: str) -> list[str]:
    line = line.strip().removeprefix("|").removesuffix("|")
    return [c.strip() for c in line.split("|")]


def _table(lines: list[str]) -> str:
    rows = [_cells(line) for line in lines if not _TABLE_SEP.match(line)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    border = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    out = [
        "<w:tbl><w:tblPr>"
        '<w:tblW w:w="5000" w:type="pct"/>'
        f"<w:tblBorders>{border}</w:tblBorders>"
        "</w:tblPr><w:tblGrid>"
        + "".join("<w:gridCol/>" for _ in range(width))
        + "</w:tblGrid>"
    ]
    has_header = len(lines) > 1 and _TABLE_SEP.match(lines[1]) is not None
    for i, row in enumerate(rows):
        cells = row + [""] * (width - len(row))
        bold = has_header and i == 0
        out.append(
            "<w:tr>"
            + "".join(
                f"<w:tc><w:tcPr/><w:p>{_runs(c, bold=bold)}</w:p></w:tc>" for c in cells
            )
            + "</w:tr>"
        )
    out.append("</w:tbl>")
    # Word needs a paragraph between two tables and after a table at the end.
    out.append("<w:p/>")
    return "".join(out)


def _body(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0
    in_code = False

    def flush() -> None:
        if para:
            out.append(_paragraph(" ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(f"<w:p>{_run(line)}</w:p>")
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        if line.lstrip().startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            level = min(len(m.group(1)), 3)
            out.append(_paragraph(m.group(2), style=f"Heading{level}"))
            i += 1
            continue
        if _RULE.match(line):
            flush()
            out.append("<w:p/>")
            i += 1
            continue
        m = _BULLET.match(line)
        if m:
            flush()
            out.append(_paragraph("• " + m.group(2), indent_level=len(m.group(1)) // 2))
            i += 1
            continue
        m = _NUMBER.match(line)
        if m:
            flush()
            out.append(
                _paragraph(f"{m.group(2)}. {m.group(3)}", indent_level=len(m.group(1)) // 2)
            )
            i += 1
            continue
        para.append(line.lstrip("> ").rstrip())
        i += 1
    flush()
    return "".join(out) or "<w:p/>"


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)

_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _heading_style(level: int, size: int) -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="Heading{level}">'
        f'<w:name w:val="heading {level}"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
        '<w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
        f'<w:outlineLvl w:val="{level - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:styles {_W}>"
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>'
    '<w:sz w:val="21"/><w:szCs w:val="21"/><w:lang w:val="ko-KR" w:eastAsia="ko-KR"/>'
    "</w:rPr></w:rPrDefault>"
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr>'
    "</w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    "<w:qFormat/></w:style>"
    + _heading_style(1, 32)
    + _heading_style(2, 28)
    + _heading_style(3, 24)
    + "</w:styles>"
)


def build_docx(markdown: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_W}><w:body>{_body(markdown)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1260" w:bottom="1440" w:left="1260" '
        'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/document.xml", document)
    return buf.getvalue()
