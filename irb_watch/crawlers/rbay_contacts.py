"""The contact cards (전화, 팩스, 이메일) on a hospital's R-BAY start page.

The start page `/agency/main/<code>` shows up to three cards for the hospital's IRB office. Every
hospital fills them in its own way (a line of text, several lines, a name next to a number) and
any card may be empty. The text is kept as the hospital wrote it, whitespace tidied, because the
form differs per hospital and guessing at it (splitting numbers from names) would lose things.

Seen markup (owner, 2026-11-17, one hospital):

    <li class="ct_guide_telbox"><div class="ct_int_title2"><em>전화</em></div>
        <div class="ctn_gdb_text ck-content"><p>031-999-1158&nbsp; IRB&nbsp;</p></div></li>
    <li class="ct_guide_faxbox"> ... <div class="ctn_gdb_text ck-content"></div></li>
    <li class="ct_guide_emailbox"> ...

What counts as what:
  * none of the three cards on the page -> `card_found` False (the caller says "R-BAY shows no
    contact card"; a page that is no longer an R-BAY start page is the caller's check, see
    `looks_like_start_page`);
  * a card with no text -> that field is "" (the hospital left it empty).
The text is untrusted page text: it is stored as plain text and never run as markup.
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from irb_watch.core.hashing import collapse_whitespace
from irb_watch.crawlers.errors import LayoutChangedError

MAX_FIELD_CHARS = 500  # per card; a longer text is cut (a notice pasted into a card, not a contact)

# Card class on the page -> the field it fills. The page's own labels are not matched, so a
# hospital that renames the card ("전화번호") still works.
CARDS = {"ct_guide_telbox": "phone", "ct_guide_faxbox": "fax", "ct_guide_emailbox": "email"}


@dataclass(frozen=True)
class Contacts:
    card_found: bool
    phone: str = ""
    fax: str = ""
    email: str = ""
    # The start page links the manager page with an element whose id is workMngr, only for a
    # hospital that has one (owner, 2026-11-17): a tile <li id=workMngr> on some pages, an
    # <a id=workMngr class=ct_int_worklink> in the intro block on others. The page is read only then.
    has_staff_page: bool = False


def _unbullet(line: str) -> str:
    """A hand-typed list marker ("- ", "· ", "• ") in front of a line is dropped: the screen
    draws its own list."""
    return line[2:].lstrip() if line[:2] in ("- ", "· ", "• ", "* ") else line


def _card_text(card) -> str:
    body = card.select_one(".ctn_gdb_text")
    if body is None:
        return ""
    # One line per block or line break, so "02-123 / 02-456" written on two lines stays two. ALL
    # the text of the card counts, also text that sits outside a <p> (bare text, <div>, <li>):
    # reading only the <p> elements once dropped a phone number written next to a <p>.
    for br in body.find_all("br"):
        br.replace_with("\n")
    for block in body.find_all(["p", "div", "li", "tr"]):
        block.insert_before("\n")
        block.insert_after("\n")
    lines = [_unbullet(collapse_whitespace(line)) for line in body.get_text("").split("\n")]
    return "\n".join(line for line in lines if line)[:MAX_FIELD_CHARS]


LOGOUT_TEXT = "로그아웃"  # the same mark the live check uses on /main to tell a signed-in page


def has_logout_link(html: str) -> bool:
    return LOGOUT_TEXT in html


def looks_like_start_page(html: str) -> bool:
    """The page is an R-BAY hospital start page and not an error, login or maintenance page.
    The mark is the page's own contact-card list or its e-IRB entry button text; a page with
    neither is treated as a layout change by the caller instead of "no contact card"."""
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one("ul.ctn_difx_box") or soup.select_one("[class*=ct_guide_]") or "입장하기" in html)


_TAB_VIEW = re.compile(r"^cts_tab_view\d+$")
MAX_STAFF_GROUPS = 10
MAX_STAFF_ENTRIES = 100  # over every tab of one hospital
MAX_STAFF_FIELD_CHARS = 200


@dataclass(frozen=True)
class StaffEntry:
    """One card of the manager page: what the office is for, who answers, and how to reach them."""

    title: str
    name: str
    email: str
    tel: str


@dataclass(frozen=True)
class StaffGroup:
    """One tab of the manager page (e.g. IRB, 임상시험센터)."""

    tab: str
    entries: tuple[StaffEntry, ...]


def _cell(card, css: str) -> str:
    el = card.select_one(f"p.{css}")
    return collapse_whitespace(el.get_text(" "))[:MAX_STAFF_FIELD_CHARS] if el is not None else ""


def parse_staff(html: str) -> list[StaffGroup]:
    """The people cards of the hospital's manager page (`/agency/workmanager/0/<code>`), grouped
    by tab. An empty list when the page holds no cards (a hospital may fill none in). A page
    that has the tab or card structure but no card that parses raises LayoutChangedError, so a
    changed layout never looks like "no managers". Text stays as written (untrusted plain text)."""
    soup = BeautifulSoup(html, "html.parser")
    # Exactly cts_tab_view1, cts_tab_view2, ...: a wrapper whose class only contains the name must
    # not shift the views against the tab buttons.
    views = soup.find_all("div", class_=_TAB_VIEW)
    # Pages without tabs hold their cards straight in the body.
    tabs = [collapse_whitespace(b.get_text(" ")) for b in soup.select("ul.cts_tabbox button")]
    groups: list[StaffGroup] = []
    total = 0
    for i, view in enumerate(views[:MAX_STAFF_GROUPS]):
        cards = view.select("div.ct_tel_guidebox")
        entries = tuple(
            StaffEntry(_cell(c, "cttg_title"), _cell(c, "cttg_name"), _cell(c, "cttg_email"), _cell(c, "cttg_tel"))
            for c in cards
        )
        entries = tuple(e for e in entries if e.title or e.name or e.email or e.tel)[: MAX_STAFF_ENTRIES - total]
        total += len(entries)
        if entries:
            groups.append(StaffGroup(tabs[i] if i < len(tabs) else "", entries))
    if not groups:
        loose = tuple(
            e for e in (
                StaffEntry(_cell(c, "cttg_title"), _cell(c, "cttg_name"), _cell(c, "cttg_email"), _cell(c, "cttg_tel"))
                for c in soup.select("div.ct_tel_guidebox")
            )
            if e.title or e.name or e.email or e.tel
        )[:MAX_STAFF_ENTRIES]
        if loose:  # cards with no tab wrapper
            groups.append(StaffGroup("", loose))
    # An empty tab list (`<ul class="cts_tabbox"></ul>`, no tab, no view, no card) is a hospital
    # that filled in no manager: an empty result, not a changed layout (owner, 2026-11-18).
    tabbox = soup.select_one("ul.cts_tabbox")
    tabbox_filled = tabbox is not None and bool(tabbox.get_text(strip=True))
    if not groups and (tabbox_filled or soup.select_one("[class*=ct_tel_guide]")):
        raise LayoutChangedError("manager page: the tab or card structure is there but no card was read")
    return groups


def parse_contacts(html: str) -> Contacts:
    """The three cards of a start page. LayoutChangedError when the page is not a start page."""
    if not looks_like_start_page(html):
        raise LayoutChangedError("hospital start page: no contact list and no entry button found")
    soup = BeautifulSoup(html, "html.parser")
    values: dict[str, str] = {}
    found = False
    for css, field in CARDS.items():
        card = soup.select_one(f"li.{css}")
        if card is not None:
            found = True
            values[field] = _card_text(card)
    return Contacts(found, has_staff_page=soup.select_one("#workMngr") is not None, **values)
