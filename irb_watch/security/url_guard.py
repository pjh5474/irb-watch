"""SSRF guard. Call validate_url() on the initial URL AND on every redirect hop."""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}

Resolver = Callable[[str], list[str]]


class UnsafeURLError(ValueError):
    """The URL must not be fetched."""


def _system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host: {host}") from exc
    return [info[4][0] for info in infos]


def _is_public(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    # Unwrap ::ffff:a.b.c.d so a mapped private v4 address is judged as v4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str, resolver: Resolver = _system_resolver) -> str:
    """Return the URL if safe, else raise UnsafeURLError.

    Every address the host resolves to must be public, so a hostname with one
    private record is rejected.

    Limitation: this checks at validation time. To fully stop DNS rebinding the
    fetcher should connect to the IP that was validated.
    """
    parts = urlsplit(url.strip())

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme or '(none)'}")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("missing host")

    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = resolver(host)

    if not addresses:
        raise UnsafeURLError(f"no addresses for host: {host}")
    for addr in addresses:
        if not _is_public(addr):
            raise UnsafeURLError(f"host resolves to non-public address: {addr}")
    return url
