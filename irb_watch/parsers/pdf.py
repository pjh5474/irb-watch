"""Extract text from text-based PDFs with pdfplumber (local, free, deterministic).

Scanned (image-only) PDFs have no text layer; they raise TextUnavailableError instead of
returning an empty string, because an empty hash would look like "the document became
empty". That is an expected outcome (track by file hash), not a failure. Real problems
(corrupt, password-protected, too many pages) raise ParseError. OCR is a later step.
"""

from dataclasses import dataclass
from io import BytesIO

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect

from irb_watch.core.hashing import normalize_text, sha256_hex
from irb_watch.parsers.errors import NO_TEXT_LAYER, ParseError, TextUnavailableError

MAX_PAGES = 500
MIN_TEXT_CHARS = 20  # below this the file is treated as having no text layer


@dataclass(frozen=True)
class ParsedDocument:
    text: str  # normalized (see core.hashing.normalize_text)
    text_hash: str
    pages: int
    empty_pages: int  # pages with no extractable text, e.g. scanned inserts


def parse_pdf(content: bytes, max_pages: int = MAX_PAGES) -> ParsedDocument:
    if not content.startswith(b"%PDF-"):
        raise ParseError("not a PDF file (missing %PDF- header)")

    page_texts: list[str] = []
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            if len(pdf.pages) > max_pages:
                raise ParseError(f"PDF has {len(pdf.pages)} pages, limit is {max_pages}")
            for page in pdf.pages:
                page_texts.append(page.extract_text() or "")
    except ParseError:
        raise
    except PDFPasswordIncorrect as exc:
        raise ParseError("PDF is password-protected") from exc
    except Exception as exc:
        raise ParseError(f"could not read PDF ({type(exc).__name__})") from exc

    if not page_texts:
        raise ParseError("PDF has no pages")

    normalized = [normalize_text(t) for t in page_texts]
    text = normalize_text(" ".join(normalized))
    if len(text) < MIN_TEXT_CHARS:
        raise TextUnavailableError(NO_TEXT_LAYER, "PDF has no extractable text (scanned image?)")

    return ParsedDocument(
        text=text,
        text_hash=sha256_hex(text.encode("utf-8")),
        pages=len(page_texts),
        empty_pages=sum(1 for t in normalized if not t),
    )
