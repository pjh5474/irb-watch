"""Admin-managed list of sources (URLs to scan)."""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from irb_watch.crawlers.rbay import BOARD_KINDS as RBAY_BOARD_KINDS
from irb_watch.crawlers.rbay import BOARDS as RBAY_BOARDS
from irb_watch.crawlers.rbay import HOST as RBAY_HOST
from irb_watch.crawlers.rbay import HOSTS as RBAY_HOSTS
from irb_watch.crawlers.rbay import agency_key as rbay_agency_key
from irb_watch.crawlers.rbay import agency_url as rbay_agency_url
from irb_watch.crawlers.sessions import valid_name
from irb_watch.security.url_guard import UnsafeURLError, validate_url
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.hospitals import MAX_NAME_CHARS, HospitalService, checked_kind
from irb_watch.services.users import AuditSink

SNUH_HOST = "hrpp.snuh.org"
SNUH_LIST_PATH = "/hrpp/bbs/list.do"
RBAY_ADAPTER = "rbay"
# A hospital without an e-IRB site: an admin uploads its documents by hand (services/manual.py).
# Never scanned; its url is a "manual:<random>" placeholder.
MANUAL_ADAPTER = "manual"
MANUAL_URL_PREFIX = "manual:"


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    adapter: str
    url: str
    config: dict
    needs_browser: bool
    is_active: bool
    created_at: str
    last_scan_at: str | None = None
    last_scan_status: str | None = None
    last_scan_error: str | None = None
    scan_started_at: str | None = None
    # Members-only board: readable only by admins and by users who proved access.
    requires_login: bool = False
    # Set after a rejected login. Scheduled scans skip the source until a person scans it.
    scans_paused_reason: str | None = None
    # False: no text of this source is sent to the AI services (scan cards get a plain
    # template line, summaries are refused). Members-only sources start with it off.
    ai_enabled: bool = True
    # The hospital this board belongs to (required in the database) and the board's kind
    # (services/hospitals.BOARD_KINDS), which only sets defaults.
    hospital_id: int | None = None
    board_kind: str = "other"

    @property
    def is_manual(self) -> bool:
        """Filled by hand by an admin (no e-IRB site): never scanned, never "stale"."""
        return self.adapter == MANUAL_ADAPTER


class DuplicateSourceError(Exception):
    """Raised by SourceStore.create when the same URL is already registered for the same login.

    The same board URL may be registered again with a different `login_user` (two studies
    with their own accounts); the database enforces this with a unique index.
    """


class SourceStore(Protocol):
    def create(
        self,
        name: str,
        adapter: str,
        url: str,
        config: dict,
        created_by: str | None,
        requires_login: bool = False,
        ai_enabled: bool = True,
        hospital_id: int | None = None,
        board_kind: str = "other",
    ) -> Source: ...
    def get(self, source_id: int) -> Source | None: ...
    def list(self) -> list[Source]: ...
    def update(self, source_id: int, values: dict) -> Source: ...


def detect_adapter(url: str) -> tuple[str, str, dict]:
    """Return (adapter, canonical_url, config) or raise ValidationError for unknown sites."""
    parts = urlsplit(url.strip())
    if (parts.hostname or "").lower() == SNUH_HOST and parts.path == SNUH_LIST_PATH:
        key = (parse_qs(parts.query).get("key") or [""])[0]
        if key.isdigit():
            canonical = f"https://{SNUH_HOST}{SNUH_LIST_PATH}?key={key}"
            return "snuh_hrpp", canonical, {"board_key": key}
        raise ValidationError("the SNUH board link needs a numeric ?key=... parameter")
    if (parts.hostname or "").lower() in RBAY_HOSTS:
        # One source per board: the hospital's R-BAY address plus ?board=<code>.
        agency = rbay_agency_key(f"https://{RBAY_HOST}{parts.path}")
        # Addresses copied from R-BAY often end in "#" (its links are href="#"), so
        # "...#?board=NOTI" puts the board after the fragment mark: read it from there too.
        fragment_query = parts.fragment.lstrip("?")
        board = (
            parse_qs(parts.query).get("board") or parse_qs(fragment_query).get("board") or [""]
        )[0].strip().upper()
        if agency is None:
            raise ValidationError(
                "an R-BAY source needs the hospital's address, https://www.r-bay.co.kr/agency/main/<code>"
            )
        if board not in RBAY_BOARDS:
            choices = ", ".join(f"{code} ({name})" for code, name in RBAY_BOARDS.items())
            raise ValidationError(f"add ?board=<code> to the R-BAY address; one of: {choices}")
        canonical = f"{rbay_agency_url(agency)}?board={board}"
        return RBAY_ADAPTER, canonical, {"agency": agency, "board": board}
    raise ValidationError(
        "no adapter for this site yet (supported: hrpp.snuh.org boards, R-BAY e-IRB boards)"
    )


def site_key(adapter: str, config: dict) -> str | None:
    """Boards with the same key are on the same hospital's site: R-BAY by agency code, every
    SNUH HRPP board together. None: no rule (each board picks or creates its hospital)."""
    if adapter == RBAY_ADAPTER and config.get("agency"):
        return f"rbay:{config['agency']}"
    if adapter == "snuh_hrpp":
        return "snuh_hrpp"
    return None


def default_kind(adapter: str, config: dict) -> str:
    if adapter == RBAY_ADAPTER:
        return RBAY_BOARD_KINDS.get(str(config.get("board", "")), "other")
    return "other"


def _checked_login_user(login_user: str) -> str:
    login_user = login_user.strip()
    if not valid_name(login_user):
        raise ValidationError("login_user may use only letters, digits, '.', '_' and '-'")
    return login_user


class SourceService:
    def __init__(
        self,
        store: SourceStore,
        audit: AuditSink,
        url_checker: Callable[[str], str] = validate_url,
        *,
        hospitals: HospitalService,
    ) -> None:
        self._store = store
        self._audit = audit
        self._check_url = url_checker
        self._hospitals = hospitals

    def _hospital_for(
        self, actor_id: str | None, name: str, adapter: str, config: dict, hospital_id: int | None
    ) -> tuple[int, bool]:
        """The chosen hospital; else the hospital of another board on the same site; else the
        hospital with the board's name (R-BAY: the part before " : "), created if missing.
        Returns (hospital id, made by this call). A source name may be longer than a hospital
        name: the made hospital gets its first MAX_NAME_CHARS characters."""
        if hospital_id is not None:
            return self._hospitals.get(hospital_id).id, False
        key = site_key(adapter, config)
        if key is not None:
            for s in self._store.list():
                if s.hospital_id is not None and site_key(s.adapter, s.config) == key:
                    return s.hospital_id, False
        hospital_name = name.split(" : ")[0] if adapter == RBAY_ADAPTER else name
        hospital_name = " ".join(hospital_name.split())[:MAX_NAME_CHARS].strip() or name[:MAX_NAME_CHARS]
        found = self._hospitals.find_by_name(hospital_name)
        if found:
            return found.id, False
        try:
            return self._hospitals.create(actor_id, hospital_name).id, True
        except ConflictError:
            # Another request made the same hospital at the same moment: use that one.
            found = self._hospitals.find_by_name(hospital_name)
            if found is None:
                raise
            return found.id, False

    def _drop_new_hospital(self, actor_id: str | None, hospital_id: int, made: bool) -> None:
        """The board was not created: a hospital made only for it must not stay behind."""
        if made:
            self._hospitals.remove_empty(actor_id, hospital_id)

    def create(
        self,
        actor_id: str | None,
        name: str,
        url: str,
        login_user: str | None = None,
        ai_enabled: bool | None = None,
        hospital_id: int | None = None,
        board_kind: str | None = None,
    ) -> Source:
        name = name.strip()
        if not name:
            raise ValidationError("name must not be empty")
        adapter, canonical, config = detect_adapter(url)
        kind = checked_kind(board_kind) if board_kind else default_kind(adapter, config)
        if login_user:
            config["login_user"] = _checked_login_user(login_user)
        elif adapter == RBAY_ADAPTER:
            raise ValidationError(
                "an R-BAY board can only be read with a login: give login_user "
                "(after creating it, save the password on the source's admin page; one login "
                "per hospital)"
            )
        try:
            self._check_url(canonical)
        except UnsafeURLError as exc:
            raise ValidationError(f"URL is not allowed: {exc}") from exc
        members_only = "login_user" in config
        # Study documents may be confidential: AI is off unless an admin asks for it.
        use_ai = (not members_only) if ai_enabled is None else ai_enabled
        hospital, made = self._hospital_for(actor_id, name, adapter, config, hospital_id)
        try:
            source = self._store.create(
                name,
                adapter,
                canonical,
                config,
                actor_id,
                requires_login=members_only,
                ai_enabled=use_ai,
                hospital_id=hospital,
                board_kind=kind,
            )
        except DuplicateSourceError as exc:
            self._drop_new_hospital(actor_id, hospital, made)
            raise ConflictError("this URL is already registered for this login") from exc
        except Exception:
            self._drop_new_hospital(actor_id, hospital, made)
            raise
        self._audit.record(
            "admin",
            "source.created",
            actor_id,
            title=f"Source {name} added",
            payload={
                "url": canonical, "adapter": adapter, "ai_enabled": use_ai,
                "hospital_id": hospital, "board_kind": kind,
            },
            source_id=source.id,
        )
        return source

    def create_manual(
        self,
        actor_id: str | None,
        name: str,
        members_only: bool,
        ai_enabled: bool | None = None,
        hospital_id: int | None = None,
        board_kind: str | None = None,
    ) -> Source:
        """A source filled by hand (no site to scan). members_only: readable only by admins and
        assigned members, like a study; AI then starts off unless the admin says otherwise.
        Without a hospital, a public source uses the one with its name (created if missing);
        a members-only source must name its hospital."""
        name = name.strip()
        if not name:
            raise ValidationError("name must not be empty")
        kind = checked_kind(board_kind) if board_kind else "other"
        if members_only and hospital_id is None:
            # Hospital names are shown to every user: a members-only source (possibly named
            # after a study or sponsor) never becomes a hospital name by itself (owner, 2026-11-12).
            raise ValidationError(
                "choose the hospital for a members-only source "
                "(hospital names are visible to every user, so none is made from its name)"
            )
        use_ai = (not members_only) if ai_enabled is None else ai_enabled
        hospital, made = self._hospital_for(actor_id, name, MANUAL_ADAPTER, {}, hospital_id)
        placeholder = f"{MANUAL_URL_PREFIX}{secrets.token_hex(8)}"
        try:
            source = self._store.create(
                name,
                MANUAL_ADAPTER,
                placeholder,
                {},
                actor_id,
                requires_login=members_only,
                ai_enabled=use_ai,
                hospital_id=hospital,
                board_kind=kind,
            )
        except DuplicateSourceError as exc:  # 16 random hex digits: practically never
            self._drop_new_hospital(actor_id, hospital, made)
            raise ConflictError("please try again") from exc
        except Exception:
            self._drop_new_hospital(actor_id, hospital, made)
            raise
        self._audit.record(
            "admin",
            "source.created",
            actor_id,
            title=f"Source {name} added",
            payload={
                "adapter": MANUAL_ADAPTER, "members_only": members_only, "ai_enabled": use_ai,
                "hospital_id": hospital, "board_kind": kind,
            },
            source_id=source.id,
        )
        return source

    def list(self) -> list[Source]:
        return self._store.list()

    def get(self, source_id: int) -> Source:
        source = self._store.get(source_id)
        if source is None:
            raise NotFoundError("source not found")
        return source

    def update(
        self,
        actor_id: str | None,
        source_id: int,
        name: str | None = None,
        is_active: bool | None = None,
        login_user: str | None = None,
        ai_enabled: bool | None = None,
        hospital_id: int | None = None,
        board_kind: str | None = None,
    ) -> Source:
        """hospital_id moves the board to another hospital. Its documents move with it; the
        pipelines, guides and forms of the old hospital stay there."""
        current = self.get(source_id)
        values: dict = {}
        if hospital_id is not None and hospital_id != current.hospital_id:
            values["hospital_id"] = self._hospitals.get(hospital_id).id
        if board_kind is not None and board_kind != current.board_kind:
            values["board_kind"] = checked_kind(board_kind)
        if login_user is not None and current.is_manual:
            raise ValidationError("a manual source has no site to log in to")
        if login_user is not None:  # "" removes the login
            if not login_user.strip() and current.adapter == RBAY_ADAPTER:
                raise ValidationError("an R-BAY board cannot be read without a login")
            config = {k: v for k, v in current.config.items() if k != "login_user"}
            if login_user:
                config["login_user"] = _checked_login_user(login_user)
                values["requires_login"] = True
                if not current.requires_login and ai_enabled is None:
                    values["ai_enabled"] = False  # a public source turned members-only: AI off
            # Removing the scanning login never clears requires_login: data already
            # scanned from a members-only board must not become public by accident.
            values["config"] = config
        if name is not None:
            if not name.strip():
                raise ValidationError("name must not be empty")
            values["name"] = name.strip()
        if is_active is not None:
            values["is_active"] = is_active
        if ai_enabled is not None:
            values["ai_enabled"] = ai_enabled
        if not values:
            return self.get(source_id)
        try:
            source = self._store.update(source_id, values)
        except DuplicateSourceError as exc:
            raise ConflictError("this URL is already registered for this login") from exc
        self._audit.record(
            "admin",
            "source.updated",
            actor_id,
            title=f"Source {source.name} updated",
            payload=values,
            source_id=source_id,
        )
        return source
