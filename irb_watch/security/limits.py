"""Size limits for downloads."""

from collections.abc import Iterable


class TooLargeError(ValueError):
    """Response exceeds the configured size cap."""


def check_content_length(header_value: str | None, max_bytes: int) -> None:
    """Reject early when the server declares a size over the cap.

    A missing or bad header is not trusted either way; stream_with_cap still applies.
    """
    if header_value is None:
        return
    try:
        declared = int(header_value)
    except ValueError:
        return
    if declared > max_bytes:
        raise TooLargeError(f"declared size {declared} exceeds {max_bytes}")


def stream_with_cap(chunks: Iterable[bytes], max_bytes: int) -> bytes:
    """Join chunks, aborting as soon as the cap is passed."""
    buf = bytearray()
    for chunk in chunks:
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise TooLargeError(f"body exceeds {max_bytes} bytes")
    return bytes(buf)
