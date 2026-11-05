"""Stream the text out of a word-processor XML part without trusting the XML.

defusedxml refuses DTDs, entity tricks and external references, which is what protects
against "billion laughs" style files. Parsing is streaming, so memory stays small.
"""

from io import BytesIO

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as XmlParseError
from defusedxml.ElementTree import iterparse

from irb_watch.parsers.errors import UnreadableFileError


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_text(
    xml: bytes,
    *,
    text_tags: frozenset[str],
    paragraph_tags: frozenset[str],
    tab_tags: frozenset[str] = frozenset(),
    break_tags: frozenset[str] = frozenset(),
    skip_tags: frozenset[str] = frozenset(),
    what: str,
) -> str:
    """Concatenate text in document order.

    text_tags      elements whose text is content (w:t, hp:t)
    paragraph_tags a newline is added after each of these ends
    skip_tags      whole subtrees ignored (deleted text, duplicate fallbacks)
    """
    out: list[str] = []
    skipping = 0
    try:
        for event, el in iterparse(BytesIO(xml), events=("start", "end"), forbid_dtd=True):
            name = _local(el.tag)
            if event == "start":
                if name in skip_tags:
                    skipping += 1
                continue
            if name in skip_tags:
                skipping -= 1
            elif not skipping:
                if name in text_tags:
                    out.append("".join(el.itertext()))
                elif name in tab_tags:
                    out.append("\t")
                elif name in break_tags or name in paragraph_tags:
                    out.append("\n")
            el.clear()
    except (XmlParseError, DefusedXmlException) as exc:
        raise UnreadableFileError(f"{what} contains malformed or unsafe XML") from exc
    return "".join(out)
