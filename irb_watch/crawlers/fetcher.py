"""Polite, SSRF-safe HTTP fetcher.

Redirects are followed by hand so validate_url() runs on every hop. robots.txt is
honoured, requests to one host are spaced out, and bodies are size-capped.
"""

import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

import httpx

from irb_watch.crawlers.errors import (
    FetchError,
    RobotsDisallowedError,
    SiteUnavailableError,
    TransientFetchError,
)
from irb_watch.security.limits import TooLargeError, check_content_length, stream_with_cap
from irb_watch.security.url_guard import UnsafeURLError, validate_url

USER_AGENT = "IRBWatchBot/0.1"

# Temporary "try again" answers. A plain 500 is not retried: it often means a real bug on
# that one page, and repeating it only adds load.
RETRYABLE_STATUS = frozenset({502, 503, 504})
DEFAULT_RETRY_WAITS = (5.0, 15.0)  # seconds before the 2nd and 3rd try


class _Transient(Exception):
    """A connection-level failure worth retrying (internal to this module)."""


@dataclass(frozen=True)
class Body:
    """A POST body: form fields or JSON (one of them)."""

    data: dict[str, str] | None = None
    json: dict | None = None

    def kwargs(self) -> dict:
        if self.json is not None:
            return {"json": self.json}
        return {"data": self.data or {}}


@dataclass(frozen=True)
class FetchedResponse:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes

    @property
    def text(self) -> str:
        # iter_bytes() already decompressed the body, so drop the encoding header
        # or httpx would try to decompress it a second time.
        headers = httpx.Headers(self.headers)
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(self.status, headers=headers, content=self.content).text


class SafeFetcher:
    def __init__(
        self,
        *,
        max_bytes: int = 20_000_000,
        timeout: float = 30.0,
        delay: float = 2.0,
        max_redirects: int = 5,
        check_robots: bool = True,
        retry_waits: tuple[float, ...] = DEFAULT_RETRY_WAITS,
        retry_budget: int = 10,
        cookies: httpx.Cookies | None = None,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        verify: ssl.SSLContext | bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._delay = delay
        self._max_redirects = max_redirects
        self._check_robots = check_robots
        self._resolver = resolver
        self._sleep = sleep
        self._monotonic = monotonic
        self._retry_waits = retry_waits
        self._retry_budget = retry_budget
        self._retries_used = 0
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}
        # `verify` is an SSL context only for a site with a broken certificate chain (see
        # crawlers/tls.py); verification is never switched off.
        if verify is False:
            raise ValueError("TLS verification cannot be switched off")
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            cookies=cookies,
            transport=transport,
            verify=verify,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    def close(self) -> None:
        self._client.close()

    def get(
        self, url: str, params: dict[str, str] | None = None, max_bytes: int | None = None
    ) -> FetchedResponse:
        """GET a URL. Any non-2xx status raises FetchError."""
        response = self._request(url, params, max_bytes or self._max_bytes, self._check_robots)
        if not 200 <= response.status < 300:
            raise FetchError(f"HTTP {response.status} for {response.url}")
        return response

    def post(
        self,
        url: str,
        data: dict[str, str] | None = None,
        json: dict | None = None,
        max_bytes: int | None = None,
        follow_redirects: bool = True,
    ) -> FetchedResponse:
        """POST a form or JSON body. ONLY for requests that read: some sites (r-bay) load list
        pages, posts and files by POST. Retried like a GET for that reason. A redirect is
        followed as a GET (what a browser does after a form post). Non-2xx raises FetchError,
        except a redirect when follow_redirects is False (returned as is)."""
        body = Body(data=data, json=json)
        response = self._request(
            url, None, max_bytes or self._max_bytes, self._check_robots, body, follow_redirects
        )
        if follow_redirects and not 200 <= response.status < 300:
            raise FetchError(f"HTTP {response.status} for {response.url}")
        return response

    def _validate(self, url: str) -> None:
        try:
            if self._resolver is None:
                validate_url(url)
            else:
                validate_url(url, self._resolver)
        except UnsafeURLError as exc:
            raise FetchError(f"blocked URL {url}: {exc}") from exc

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            remaining = self._delay - (self._monotonic() - last)
            if remaining > 0:
                self._sleep(remaining)

    def _request(
        self,
        url: str,
        params: dict[str, str] | None,
        max_bytes: int,
        check_robots: bool,
        body: "Body | None" = None,
        follow_redirects: bool = True,
    ) -> FetchedResponse:
        """One request with retries for temporary site trouble (GET, and the read-only POSTs
        of post(), are safe to repeat).

        Retried: HTTP 502/503/504, connection errors and timeouts. Waits come from
        retry_waits (default 5 s then 15 s = 3 tries). All retries in this fetcher's life
        (one scan) share retry_budget; when it is used up the scan is abandoned with
        SiteUnavailableError instead of hammering a site that is down.
        """
        tries = len(self._retry_waits) + 1
        for attempt in range(1, tries + 1):
            problem: str
            try:
                response = self._request_once(
                    url, params, max_bytes, check_robots, body, follow_redirects
                )
            except _Transient as exc:
                problem = str(exc)
            else:
                if response.status not in RETRYABLE_STATUS:
                    return response
                problem = f"HTTP {response.status} for {response.url}"

            if attempt == tries:
                raise TransientFetchError(f"{problem} (gave up after {tries} tries)")
            if self._retries_used >= self._retry_budget:
                raise SiteUnavailableError(
                    f"site unavailable: {problem}; stopped after {self._retries_used} "
                    "retries in this scan"
                )
            self._retries_used += 1
            self._sleep(self._retry_waits[attempt - 1])
        raise AssertionError("unreachable")  # pragma: no cover

    def _request_once(
        self,
        url: str,
        params: dict[str, str] | None,
        max_bytes: int,
        check_robots: bool,
        body: "Body | None" = None,
        follow_redirects: bool = True,
    ) -> FetchedResponse:
        if params:
            url = str(httpx.URL(url).copy_merge_params(params))

        for _ in range(self._max_redirects + 1):
            self._validate(url)
            if check_robots:
                self._ensure_allowed(url)
            host = httpx.URL(url).host
            self._throttle(host)
            method = "POST" if body is not None else "GET"
            extra = body.kwargs() if body is not None else {}
            try:
                with self._client.stream(method, url, **extra) as r:
                    self._last_request[host] = self._monotonic()
                    if r.is_redirect:
                        location = r.headers.get("location")
                        if not follow_redirects:
                            return FetchedResponse(url, r.status_code, r.headers, b"")
                        if not location:
                            raise FetchError(f"redirect without Location from {url}")
                        url = str(httpx.URL(url).join(location))
                        body = None  # after a form post, the browser follows with a GET
                        continue
                    check_content_length(r.headers.get("content-length"), max_bytes)
                    content = stream_with_cap(r.iter_bytes(), max_bytes)
                    return FetchedResponse(url, r.status_code, r.headers, content)
            except TooLargeError as exc:
                raise FetchError(f"too large: {url}: {exc}") from exc
            except httpx.TransportError as exc:  # connect errors, timeouts, dropped connections
                raise _Transient(f"request failed: {url}: {exc}") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"request failed: {url}: {exc}") from exc
        raise FetchError("too many redirects")

    def _ensure_allowed(self, url: str) -> None:
        parsed = httpx.URL(url)
        origin = f"{parsed.scheme}://{parsed.netloc.decode()}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            response = self._request(f"{origin}/robots.txt", None, 1_000_000, check_robots=False)
            if response.status >= 500:
                raise FetchError(f"robots.txt unavailable (HTTP {response.status}) at {origin}")
            if response.status >= 400:
                parser.parse([])  # no robots.txt: everything allowed
            else:
                parser.parse(response.text.splitlines())
            self._robots[origin] = parser
        if not parser.can_fetch(USER_AGENT, url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
