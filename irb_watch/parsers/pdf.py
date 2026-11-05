"""Extract text from text-based PDFs with pdfplumber (local, free, deterministic).

Scanned (image-only) PDFs have no text layer; they raise TextUnavailableError instead of
returning an empty string, because an empty hash would look like "the document became
empty". Genuine PDFs we cannot read (corrupt, password-protected, too many pages) raise
UnreadableFileError. OCR is a later step.
"""

from io import BytesIO

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect

from irb_watch.parsers.common import ParsedDocument, build_document, internal_error
from irb_watch.parsers.errors import ParseError, TextUnavailableError, UnreadableFileError

MAX_PAGES = 500

__all__ = ["MAX_PAGES", "ParsedDocument", "parse_pdf"]


def parse_pdf(content: bytes, max_pages: int = MAX_PAGES) -> ParsedDocument:
    if not content.startswith(b"%PDF-"):
        raise ParseError("not a PDF file (missing %PDF- header)")

    page_texts: list[str] = []
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            if len(pdf.pages) > max_pages:
                raise UnreadableFileError(f"PDF has {len(pdf.pages)} pages, limit is {max_pages}")
            for page in pdf.pages:
                page_texts.append(page.extract_text() or "")
    except (UnreadableFileError, TextUnavailableError):
        raise
    except PDFPasswordIncorrect as exc:
        raise UnreadableFileError("PDF is password-protected") from exc
    except Exception as exc:
        # pdfminer raises many different types for damaged files; all of them mean "cannot read".
        raise UnreadableFileError(f"PDF could not be read ({type(exc).__name__})") from exc

    if not page_texts:
        raise UnreadableFileError("PDF has no pages")
    try:
        return build_document(page_texts, "PDF")
    except (UnreadableFileError, TextUnavailableError):
        raise
    except Exception as exc:
        raise internal_error("PDF", exc) from exc
