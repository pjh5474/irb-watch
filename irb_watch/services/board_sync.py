"""Add the boards an R-BAY hospital has but the server does not read yet (owner, 2026-11-20).

One call per hospital: log in (the hospital's stored login, read only), read the e-IRB menu, and
for every board the server knows (crawlers/rbay.BOARDS) that is in the menu and not yet a source of
this hospital and login, make that source. A known board missing from the menu is skipped and
listed, never an error (not every hospital has every board). Menu entries the server does not
know are only listed. `dry_run` makes nothing: it shows what would be made.

A failure (no stored login, a rejected login, the site down, a source that could not be made) is
reported with its reason for that hospital; it never looks like "nothing to add".
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from irb_watch.crawlers.rbay import BOARDS, agency_url
from irb_watch.services.errors import ValidationError
from irb_watch.services.hospitals import HospitalService
from irb_watch.services.source_login import login_site
from irb_watch.services.sources import RBAY_ADAPTER, Source, SourceService

log = logging.getLogger(__name__)

# (login user, hospital code, the site the login is stored under) -> {board code: menu label}
MenuReader = Callable[[str, str, str], dict[str, str]]


@dataclass
class GroupResult:
    """One R-BAY hospital code with one stored login."""

    agency: str
    login_user: str
    error: str | None = None  # why the menu could not be read; nothing was changed
    menu: dict[str, str] = field(default_factory=dict)  # every board of the e-IRB menu
    already: list[str] = field(default_factory=list)  # known boards that are sources already
    to_create: list[str] = field(default_factory=list)  # known boards in the menu, not yet sources
    not_in_menu: list[str] = field(default_factory=list)  # known boards this hospital does not have
    unknown: list[str] = field(default_factory=list)  # menu boards the server does not read
    created: list[Source] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # board code -> why it was not created


@dataclass
class SyncResult:
    hospital_id: int
    hospital_name: str
    dry_run: bool
    groups: list[GroupResult]


class BoardSyncService:
    def __init__(self, sources: SourceService, hospitals: HospitalService, read_menu: MenuReader) -> None:
        self._sources = sources
        self._hospitals = hospitals
        self._read_menu = read_menu

    def sync(self, actor_id: str | None, hospital_id: int, dry_run: bool = True) -> SyncResult:
        hospital = self._hospitals.get(hospital_id)  # NotFoundError for an unknown hospital
        rbay = [
            s for s in self._sources.list()
            if s.adapter == RBAY_ADAPTER and s.hospital_id == hospital_id and s.config.get("login_user")
        ]
        if not rbay:
            raise ValidationError("this hospital has no R-BAY board with a login to read the menu with")
        groups: dict[tuple[str, str], list[Source]] = {}
        for s in rbay:
            groups.setdefault((s.config["agency"], s.config["login_user"]), []).append(s)
        out = [
            self._sync_group(actor_id, hospital.name, hospital_id, agency, user, members, dry_run)
            for (agency, user), members in groups.items()
        ]
        return SyncResult(hospital_id, hospital.name, dry_run, out)

    def _sync_group(
        self, actor_id: str | None, hospital_name: str, hospital_id: int, agency: str, user: str,
        members: list[Source], dry_run: bool,
    ) -> GroupResult:
        result = GroupResult(agency, user)
        try:
            menu = self._read_menu(user, agency, login_site(members[0]) or "")
        except Exception as exc:  # noqa: BLE001 - reported with its reason, never "nothing to add"
            result.error = f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"
            return result
        result.menu = dict(menu)
        # Registered anywhere for this hospital code and login, even under another hospital row.
        have = {
            s.config.get("board") for s in self._sources.list()
            if s.adapter == RBAY_ADAPTER and s.config.get("agency") == agency and s.config.get("login_user") == user
        }
        for code in BOARDS:
            if code not in menu:
                result.not_in_menu.append(code)
            elif code in have:
                result.already.append(code)
            else:
                result.to_create.append(code)
        result.unknown = [c for c in menu if c not in BOARDS]
        if dry_run:
            return result
        for code in result.to_create:
            try:
                result.created.append(
                    self._sources.create(
                        actor_id, f"{hospital_name} : {BOARDS[code]}", f"{agency_url(agency)}?board={code}",
                        user, None, hospital_id, None,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the other boards are still made
                result.failed[code] = " ".join(str(exc).split())[:300]
                log.warning("board %s of hospital %s was not created: %s", code, hospital_id, type(exc).__name__)
        return result
