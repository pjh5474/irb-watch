"""Map the R-BAY e-IRB "신규계획" (new submission) form and find which controls show, hide, enable,
disable or require other fields. Writes nothing to R-BAY and nothing to the database.

    uv run python -m irb_watch.scripts.rbay_form_probe --keyring --user owner \
        --url "https://www.r-bay.co.kr/agency/main/<hospital code>" --phase map
    ... --phase probe        # the mutation test (run `map` first and look at the baseline)

Login: the stored login for the hospital (site rbay-<hospital code>), found like live_rbay_check
finds it. The password never leaves the credential store and is never printed. Each run opens its
OWN browser context (`browser.new_context()`, an incognito-like profile: no shared cookies or
storage) and ends with the e-IRB's own logout.

Phases
  map    log in, enter the e-IRB, open 신규계획, record every input / select / textarea / button
         (id, name, class, placeholder, label, state, select options). Clicks nothing in the form.
         Writes irb_form_baseline.json.
  probe  map, then for each visible radio, checkbox and select option: change it, compare the form
         before and after, restore it (or reopen the form), and write irb_rule_map.json:

           { "<selector>": { "action_value": "Y",
                             "effects": { "show": [...], "hide": [...], "disable": [...],
                                          "enable": [...], "required": [...], "optional": [...],
                                          "options_changed": [...], "filled": [...] },
                             "labels": {"<selector>": "<label>"}, "dialogs": [...] } }

         A select option is keyed "<select selector>::option[value=\"V\"]" so each key has one value.

Safety (the form belongs to a real hospital system)
  * Only radio buttons, checkboxes and select boxes are touched. No button is ever clicked, no text
    typed, no file chosen. A control whose label, text, id, name or value looks like submit, delete,
    save or cancel (제출, 삭제, 저장 ...) is skipped and listed. 임시저장 is never pressed.
  * Alerts and confirms are DISMISSED (an "OK" could confirm a submit or delete); the message text
    is recorded as part of the rule.
  * While probing, any write-looking request (POST/PUT/DELETE to a path with save, insert, delete,
    submit, update, regist, apply ...) and any form-posting navigation is aborted and listed.
  * Caps: --max-tests, a pause between tests (--delay).
"""

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from irb_watch.api.deps import get_credential_store
from irb_watch.crawlers.credentials import KeyringCredentialStore
from irb_watch.crawlers.rbay import BASE_URL, agency_key, agency_url
from irb_watch.crawlers.rbay_login import RbaySession, logout, rbay_site_name, stored_login
from irb_watch.security.url_guard import validate_url

FORM_LINK = "/mng/rvw/testRegTempApl"  # the gate menu's 신규계획 entry (seen in the saved gate page)
FORM_NAME = "신규계획"
MENU_GROUP = "신청(보고) 작성"

# A control that looks like one of these is never touched, whatever its type.
UNSAFE_WORDS = re.compile(
    r"제출|삭제|취소|저장|전송|승인|반려|철회|submit|delete|remove|cancel|save|send|approve|withdraw",
    re.IGNORECASE,
)
# A request like this is aborted while probing (only XHR/fetch writes; plain reads go through).
WRITE_PATH = re.compile(
    r"save|insert|delete|remove|submit|update|regist|apply|confirm|temp|upload|send|sms|mail",
    re.IGNORECASE,
)
VOLATILE = re.compile(r"timer|countdown|clock", re.IGNORECASE)
EFFECT_KEYS = ("show", "hide", "disable", "enable", "required", "optional", "options_changed", "filled")

# Runs in every frame. One record per input / select / textarea / button (not type=hidden).
SNAPSHOT_JS = r"""
() => {
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&');
  const txt = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const shown = (el) => {
    if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true})) return false;
    const r = el.getClientRects();
    return r.length > 0 && (r[0].width > 0 || r[0].height > 0);
  };
  const labelFor = (el) => (el.id ? document.querySelector('label[for="' + esc(el.id) + '"]') : null);
  const visible = (el) => { const l = labelFor(el); return shown(el) || (l !== null && shown(l)); };
  const label = (el) => {
    if (el.labels && el.labels.length) return txt(el.labels[0].textContent);
    const a = el.getAttribute('aria-label') || el.getAttribute('title');
    if (a) return txt(a);
    const wrap = el.closest('label');
    if (wrap) return txt(wrap.textContent);
    const tr = el.closest('tr');
    if (tr) { const th = tr.querySelector('th'); if (th && !th.contains(el)) return txt(th.textContent); }
    const prev = el.previousElementSibling;
    return prev ? txt(prev.textContent) : '';
  };
  const css = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id) return tag + '#' + esc(el.id);
    if (el.name) {
      let s = tag + '[name="' + el.name.replace(/"/g, '\\"') + '"]';
      if (el.type === 'radio' || el.type === 'checkbox') s += '[value="' + String(el.value).replace(/"/g, '\\"') + '"]';
      if (document.querySelectorAll(s).length === 1) return s;
    }
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 7) {
      let p = n.tagName.toLowerCase();
      if (n.id) { parts.unshift(p + '#' + esc(n.id)); break; }
      const same = n.parentNode ? [...n.parentNode.children].filter((c) => c.tagName === n.tagName) : [];
      if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      parts.unshift(p);
      n = n.parentElement;
    }
    return parts.join(' > ');
  };
  const out = [];
  for (const el of document.querySelectorAll('input, select, textarea, button')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (type === 'hidden') continue;
    const rec = {
      css: css(el), tag: el.tagName.toLowerCase(), type: type, id: el.id || '', name: el.name || '',
      cls: el.className && el.className.baseVal === undefined ? txt(el.className) : '',
      placeholder: el.getAttribute('placeholder') || '', label: label(el), visible: visible(el),
      disabled: !!el.disabled, required: !!el.required || el.getAttribute('aria-required') === 'true',
      readonly: !!el.readOnly, checked: !!el.checked, value: String(el.value === undefined ? '' : el.value),
    };
    if (rec.tag === 'select') {
      rec.options = [...el.options].map((o) => ({value: o.value, text: txt(o.textContent), disabled: o.disabled, selected: o.selected}));
    }
    if (rec.tag === 'button' || type === 'button' || type === 'submit' || type === 'reset') rec.text = txt(el.textContent || el.value);
    out.push(rec);
  }
  return out;
}
"""


class ProbeError(Exception):
    """The form could not be reached or read; the message says where it stopped."""


# -- reading the form -----------------------------------------------------------------------------


def snapshot(page: Page) -> dict[str, dict]:
    """Every interactive element of the page and its frames, key -> record. The key is the CSS
    selector, prefixed with the frame for frames other than the main one; a repeated key gets '@2'."""
    out: dict[str, dict] = {}
    for index, frame in enumerate(page.frames):
        try:
            records = frame.evaluate(SNAPSHOT_JS)
        except PlaywrightError:
            continue  # a frame that went away or cannot be read
        prefix = "" if frame == page.main_frame else f"frame[{index}:{frame.name or urlsplit(frame.url).path}] >>> "
        for rec in records:
            key = prefix + rec["css"]
            n = 2
            while key in out:
                key = f"{prefix}{rec['css']}@{n}"
                n += 1
            rec["frame"], rec["key"] = index, key
            out[key] = rec
    return out


def signature(snap: dict[str, dict]) -> dict[str, tuple]:
    """The state of the form, per element: visible, disabled, required, checked and value. Any
    difference from the baseline after a restore means the form must be reopened."""
    return {
        k: (e["visible"], e["disabled"], e["required"], e["checked"], e["value"])
        for k, e in snap.items()
        if not is_volatile(e)
    }


def is_volatile(el: dict) -> bool:
    """A box the page rewrites on its own (the session countdown): never part of a rule or a restore check.
    (Treating it as state made every trial look unrestorable and reopened the form each time.)"""
    return bool(VOLATILE.search(f"{el.get('id', '')} {el.get('name', '')}"))


def is_unsafe(el: dict) -> bool:
    text = " ".join(str(el.get(f, "")) for f in ("label", "text", "id", "name", "value"))
    return bool(UNSAFE_WORDS.search(text))


# -- what to try, and what changed ----------------------------------------------------------------


@dataclass(frozen=True)
class Trial:
    key: str  # the element's key in the snapshot
    action_value: str  # what the rule file calls this action
    value: str  # the option value to select, for a select
    rule_key: str  # the key in the rule file


def plan_trials(base: dict[str, dict], max_tests: int) -> tuple[list[Trial], list[str]]:
    """The visible, enabled radios, checkboxes and select options to try, and the ones skipped as
    unsafe. A checkbox is toggled (checked or unchecked, whichever it is not); a radio is selected
    if it is not already; a select goes through each option except the current one."""
    trials: list[Trial] = []
    skipped: list[str] = []
    for key, el in base.items():
        kind = el["type"]
        if not el["visible"] or el["disabled"]:
            continue
        if kind not in ("radio", "checkbox") and el["tag"] != "select":
            continue
        if is_unsafe(el):
            skipped.append(key)
            continue
        if kind == "checkbox":
            value = f"unchecked:{el['value']}" if el["checked"] else el["value"]
            trials.append(Trial(key, value, "", key))
        elif kind == "radio":
            if not el["checked"]:
                trials.append(Trial(key, el["value"], "", key))
        else:
            current = next((o["value"] for o in el.get("options", []) if o["selected"]), None)
            for opt in el.get("options", []):
                if is_unsafe({"label": opt["text"], "value": opt["value"]}):
                    skipped.append(f'{key}::option[value="{opt["value"]}"]')  # e.g. an option 삭제 or 제출
                    continue
                if opt["value"] != current and not opt["disabled"]:
                    label = opt["value"].replace('"', '\\"')
                    trials.append(Trial(key, opt["value"], opt["value"], f'{key}::option[value="{label}"]'))
    return trials[:max_tests], skipped


def diff(before: dict[str, dict], after: dict[str, dict], skip: str) -> dict[str, list[str]]:
    """The effects of one action on every element except the one that was changed."""
    fx: dict[str, list[str]] = {k: [] for k in EFFECT_KEYS}
    for key, a in after.items():
        if key == skip or is_volatile(a):
            continue
        b = before.get(key)
        if b is None:
            if a["visible"]:
                fx["show"].append(key)  # a field that was not in the page and is now visible
            continue
        if a["visible"] != b["visible"]:
            fx["show" if a["visible"] else "hide"].append(key)
        if a["disabled"] != b["disabled"]:
            fx["disable" if a["disabled"] else "enable"].append(key)
        if a["required"] != b["required"]:
            fx["required" if a["required"] else "optional"].append(key)
        if a["tag"] == "select" and [o["value"] for o in a.get("options", [])] != [
            o["value"] for o in b.get("options", [])
        ]:
            fx["options_changed"].append(key)
        if a["type"] not in ("radio", "checkbox") and a["tag"] != "select" and a["value"] != b["value"]:
            fx["filled"].append(key)
    # removed from the page (only what was visible: a hidden field that went is not an effect)
    fx["hide"] += [k for k, b in before.items() if k not in after and k != skip and b["visible"]]
    return {k: v for k, v in fx.items() if v}


# -- doing it -------------------------------------------------------------------------------------


@dataclass
class Recorder:
    """What the page said while one trial ran."""

    dialogs: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # "METHOD path" of aborted requests
    requests: list[str] = field(default_factory=list)  # "METHOD path" of other non-GET requests
    guarding: bool = False  # True only while one trial runs: login and navigation are never blocked
    accept_next: bool = False  # True only while enter_form presses the owner-approved start button

    def on_dialog(self, dialog) -> None:
        self.dialogs.append(" ".join(dialog.message.split())[:200])
        if dialog.type == "beforeunload":
            dialog.accept()  # "leave this page?": leaving drops the unsaved trial edits, which is the point
        elif self.accept_next:
            self.accept_next = False
            dialog.accept()
        else:
            dialog.dismiss()  # never accept in a trial: an "OK" could confirm a submit or a delete

    def attach(self, context: BrowserContext) -> None:
        for page in context.pages:
            page.on("dialog", self.on_dialog)
        context.on("page", lambda p: p.on("dialog", self.on_dialog))
        context.route("**/*", self._guard)

    def _guard(self, route) -> None:
        req = route.request
        label = f"{req.method} {urlsplit(req.url).path}"
        is_write_xhr = req.resource_type in ("xhr", "fetch") and req.method != "GET"
        if self.guarding and (
            (is_write_xhr and WRITE_PATH.search(urlsplit(req.url).path))
            or (req.is_navigation_request() and req.method != "GET")
        ):
            self.blocked.append(label)
            route.abort()
            return
        if req.method != "GET":
            self.requests.append(label)
        route.continue_()

    def clear(self) -> None:
        self.dialogs, self.blocked, self.requests = [], [], []


def _locate(page: Page, el: dict):
    if el["frame"] >= len(page.frames):
        raise ProbeError("the frame of the element is gone")
    return page.frames[el["frame"]].locator(el["css"]).first


def _click(page: Page, el: dict) -> None:
    """Click a box or radio; custom-styled ones hide the input behind its label, so fall back to that."""
    try:
        _locate(page, el).click(timeout=3000)
    except PlaywrightError:
        if not el["id"]:
            raise
        page.frames[el["frame"]].locator(f'label[for="{el["id"]}"]').first.click(timeout=2000)


def perform(page: Page, el: dict, trial: Trial) -> bool:
    """Make the change. False when the control could not be operated (reported, not fatal)."""
    loc = _locate(page, el)
    try:
        if el["tag"] == "select":
            loc.select_option(value=trial.value, timeout=3000)
        else:
            _click(page, el)
        return True
    except PlaywrightError:
        return False


def restore(page: Page, base: dict[str, dict], el: dict) -> None:
    """Put the changed control back as far as it can be (a radio cannot be un-selected)."""
    loc = _locate(page, el)
    try:
        if el["tag"] == "select":
            current = next((o["value"] for o in el.get("options", []) if o["selected"]), None)
            if current is not None:
                loc.select_option(value=current, timeout=3000)
        elif el["type"] == "checkbox":
            if loc.is_checked() != el["checked"]:
                _click(page, el)
        else:
            was = next(
                (e for e in base.values() if e["type"] == "radio" and e["name"] == el["name"] and e["checked"]),
                None,
            )
            if was is not None:
                _click(page, was)
            else:  # nothing was selected at first and a radio cannot be un-clicked: clear it by script
                loc.evaluate(
                    "e => { e.checked = false; e.dispatchEvent(new Event('change', {bubbles: true})); }"
                )
    except PlaywrightError:
        pass  # the signature check after this decides whether the form must be reopened


def settle(page: Page, ms: int) -> None:
    page.wait_for_timeout(ms)
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except PlaywrightTimeout:
        pass


@dataclass
class ProbeResult:
    rules: dict[str, dict] = field(default_factory=dict)
    tried: int = 0
    not_operable: list[str] = field(default_factory=list)
    skipped_unsafe: list[str] = field(default_factory=list)
    reopened: int = 0
    stopped_early: bool = False
    rebased: int = 0  # times the form was carried on from a changed state instead of being reopened
    missing_after_reopen: list[str] = field(default_factory=list)


def probe(
    page: Page,
    reopen: Callable[[], Page],
    recorder: Recorder,
    *,
    max_tests: int = 200,
    max_reopens: int = 0,
    settle_ms: int = 400,
    delay_s: float = 0.8,
    log: Callable[[str], None] = print,
) -> ProbeResult:
    """Try each radio / checkbox / select option once and record what it changes. `reopen` gives a
    fresh copy of the form (used when a change could not be undone)."""
    base = snapshot(page)
    trials, skipped = plan_trials(base, max_tests)
    result = ProbeResult(skipped_unsafe=skipped)
    log(f"probe: {len(trials)} actions planned, {len(skipped)} controls skipped as unsafe")
    for number, trial in enumerate(trials, 1):
        el = base.get(trial.key)
        if el is None:
            result.missing_after_reopen.append(trial.rule_key)
            continue
        before = snapshot(page)
        recorder.clear()
        recorder.guarding = True
        try:
            done = perform(page, el, trial)
            if done:
                settle(page, settle_ms)
            after = snapshot(page) if done else before
        finally:
            recorder.guarding = False
        result.tried += 1
        if not done:
            result.not_operable.append(trial.rule_key)
            continue
        effects = diff(before, after, trial.key)
        if effects or recorder.dialogs or recorder.blocked:
            labels = {k: after.get(k, before.get(k, {})).get("label", "") for v in effects.values() for k in v}
            entry = {
                "label": el["label"],
                "type": el["type"],
                "action_value": trial.action_value,
                "effects": effects,
                "labels": {k: v for k, v in labels.items() if v},
            }
            if result.rebased:
                entry["from_changed_state"] = True  # an earlier trial could not be undone
            if recorder.dialogs:
                entry["dialogs"] = list(recorder.dialogs)
            if recorder.blocked:
                entry["blocked_requests"] = list(recorder.blocked)
            if recorder.requests:
                entry["requests"] = list(recorder.requests)
            result.rules[trial.rule_key] = entry
            log(f"  [{number}/{len(trials)}] {trial.rule_key}: " + ", ".join(f"{k} {len(v)}" for k, v in effects.items()))
        # Restoring fires the page's change handlers again: the write guard stays on for it too.
        recorder.guarding = True
        try:
            restore(page, base, el)
            settle(page, 150)
        finally:
            recorder.guarding = False
        now_sig, base_sig = signature(snapshot(page)), signature(base)
        if now_sig != base_sig:
            changed = [k for k in base_sig if now_sig.get(k) != base_sig[k]][:6]
            log(f"  not restored after {trial.rule_key}: " + "; ".join(
                f"{k} {base_sig[k]} -> {now_sig.get(k)}" for k in changed))
            if max_reopens == 0:
                # The page does not undo this by clicking (e.g. a radio nobody had selected keeps its
                # dependent fields). Reopening makes R-BAY create another draft, so carry on from the
                # changed state; later rules say so (their effects are relative to that state).
                result.rebased += 1
                base = snapshot(page)
            elif result.reopened >= max_reopens:
                log(f"stopped after {result.reopened} reopens: the form cannot be put back by clicking")
                result.stopped_early = True
                break
            else:
                result.reopened += 1
                page = reopen()
                base = snapshot(page)
        page.wait_for_timeout(int(delay_s * 1000))
    return result


# -- login and navigation -------------------------------------------------------------------------


def enter_gate(page: Page, agency: str, timeout_ms: int) -> None:
    """Open the hospital's page and press 입장하기 (the same steps as crawlers/rbay_login)."""
    page.goto(agency_url(agency), wait_until="domcontentloaded")
    enter = page.locator("a:has-text('입장하기')")
    if enter.count() == 0:
        raise ProbeError("this account has no 입장하기 for this hospital (not registered with its e-IRB)")
    enter.first.click()
    try:
        page.wait_for_url("**/mng/gate**", timeout=timeout_ms)
    except PlaywrightTimeout:
        raise ProbeError(f"the e-IRB did not open (stopped at {urlsplit(page.url).path})") from None
    page.wait_for_load_state("domcontentloaded")


def log_in(page: Page, login_id: str, password: str, timeout_ms: int) -> None:
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
    if page.locator("#loginId").count() == 0 or page.locator("#loginPwd").count() == 0:
        raise ProbeError("the login form changed (ID or password box not found)")
    page.fill("#loginId", login_id)
    page.fill("#loginPwd", password)
    page.click("#btnLogin")
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=timeout_ms)
    except PlaywrightTimeout:
        raise ProbeError("the login was not accepted or did not finish") from None


def open_form(context: BrowserContext, gate: Page, wait_s: float = 5.0) -> tuple[Page, bool]:
    """Click 신규계획 in the e-IRB menu. Returns (the page that shows the form, whether it opened as
    a new window). It may also load in the same window (the menu entry says data-popup-yn)."""
    opened: list[Page] = []

    def on_page(page: Page) -> None:  # a plain function: Playwright cannot wrap list.append
        opened.append(page)

    context.on("page", on_page)
    try:
        link = gate.locator(f'a.mnuLink[data-link="{FORM_LINK}"]')
        if link.count() == 0:
            link = gate.locator("a.mnuLink", has_text=FORM_NAME)
        if link.count() == 0:
            raise ProbeError(f"the menu has no {FORM_NAME} entry")
        if not link.first.is_visible():  # the entry sits in a collapsed group: open it first
            group = gate.locator("a.mnuLink", has_text=MENU_GROUP).first
            for open_group in (group.hover, group.click):
                try:
                    open_group(timeout=3000)
                except PlaywrightError:
                    continue
                if link.first.is_visible():
                    break
        if link.first.is_visible():
            link.first.click(timeout=3000)
        else:  # still hidden: send the click to the entry itself (the menu script listens for it)
            link.first.dispatch_event("click")
        for _ in range(int(wait_s * 2)):
            if opened:
                break
            gate.wait_for_timeout(500)
    finally:
        context.remove_listener("page", on_page)
    page = opened[0] if opened else gate
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)  # the form fills itself with scripts after the page loads
    return page, bool(opened)


def enter_form(page: Page, recorder: Recorder, project_type: str, affiliation: str) -> None:
    """From the type picker to the editable form: choose the research type, press 신청서 작성하기
    (accepting its one confirm, approved by the owner), then pick the affiliation row. R-BAY creates
    a draft application at that button; nothing is ever saved or submitted after it."""
    label = page.locator("label", has_text=project_type).first
    if label.count() == 0:
        raise ProbeError(f"the type picker has no '{project_type}' choice")
    label.click(timeout=3000)
    recorder.accept_next = True
    try:
        # no_wait_after: the click itself must not wait for the navigation its confirm starts
        page.locator("#btnNewAplGoReg").click(timeout=15_000, no_wait_after=True)
        page.wait_for_url("**/mergeTempAplRegMod", timeout=15_000)
    except (PlaywrightError, PlaywrightTimeout) as exc:
        where = urlsplit(page.url).path
        confirmed = not recorder.accept_next  # the handler clears it when it accepts the confirm
        raise ProbeError(
            f"the form did not open after 신청서 작성하기 (page: {where}; {type(exc).__name__}: {str(exc)[:120]}; "
            f"confirm {'accepted' if confirmed else 'never appeared'}; dialogs: {recorder.dialogs[-2:]})"
            + ("; R-BAY may already have created a draft, check before running again" if confirmed else "")
        ) from None
    finally:
        recorder.accept_next = False
    page.wait_for_load_state("domcontentloaded")
    rows = page.locator("tr.selectMuKey")
    try:
        rows.first.wait_for(state="visible", timeout=5000)
    except PlaywrightError:
        return  # no affiliation question for this account
    row = rows.filter(has_text=affiliation)
    if row.count() == 0:
        raise ProbeError(f"no affiliation row contains '{affiliation}'")
    row.first.click(timeout=3000)
    page.locator("#unic_sch_regr").wait_for(state="hidden", timeout=10_000)
    page.wait_for_timeout(1500)


# -- main -----------------------------------------------------------------------------------------


def summarize(page: Page, snap: dict[str, dict]) -> dict:
    tags: dict[str, int] = {}
    for el in snap.values():
        tags[el["tag"]] = tags.get(el["tag"], 0) + 1
    return {
        "page_path": urlsplit(page.url).path,
        "title": page.title(),
        "frames": [{"index": i, "name": f.name, "path": urlsplit(f.url).path} for i, f in enumerate(page.frames)],
        "counts": tags,
        "visible": sum(1 for e in snap.values() if e["visible"]),
    }


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only map of the R-BAY 신규계획 form.")
    parser.add_argument("--user", required=True, help="login label, e.g. owner")
    parser.add_argument("--url", required=True, help="the hospital's R-BAY address (/agency/main/<code>)")
    parser.add_argument("--keyring", action="store_true", help="read the login from Credential Manager")
    parser.add_argument("--phase", choices=("map", "probe"), default="map")
    parser.add_argument("--out-dir", default="rbay_probe_out", help="where the JSON files go")
    parser.add_argument("--project-type", default="의약품 임상시험(생동 포함)", help="the research type to start")
    parser.add_argument("--affiliation", default="미라클러스", help="text of the affiliation row to pick")
    parser.add_argument("--max-tests", type=int, default=200)
    parser.add_argument("--delay", type=float, default=0.8, help="seconds between tests")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args(argv)

    agency = agency_key(args.url)
    if agency is None:
        parser.error("--url must be https://www.r-bay.co.kr/agency/main/<hospital code>")
    for url in (f"{BASE_URL}/login", agency_url(agency)):
        validate_url(url)
    store = KeyringCredentialStore() if args.keyring else get_credential_store()
    credentials, site = stored_login(store, args.user, rbay_site_name(agency))
    if credentials is None:
        print(f"No stored login for '{args.user}' under site '{site}'.")
        return 1
    print(f"0. using the login stored under site '{site}'")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    timeout_ms = 20_000
    code = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context()  # isolated: no cookies or storage shared with any other run
        session: RbaySession | None = None
        logged_in = False
        try:
            recorder = Recorder()
            recorder.attach(context)
            gate = context.new_page()
            gate.set_default_timeout(timeout_ms)
            log_in(gate, credentials.login_id, credentials.password, timeout_ms)
            logged_in = True
            enter_gate(gate, agency, timeout_ms)
            print("1. logged in and entered the e-IRB")

            form, popup = open_form(context, gate)
            print(f"2. opened {FORM_NAME} as {'a new window' if popup else 'the same window'}")
            enter_form(form, recorder, args.project_type, args.affiliation)
            print(f"   entered the form for '{args.project_type}' as '{args.affiliation}'")

            def reopen() -> Page:
                nonlocal form, popup
                if popup:
                    form.close()
                else:
                    enter_gate(gate, agency, timeout_ms)  # the same window: back to the e-IRB start
                form, popup = open_form(context, gate)
                enter_form(form, recorder, args.project_type, args.affiliation)
                return form

            base = snapshot(form)
            info = summarize(form, base)
            print(f"3. baseline: {len(base)} elements ({info['visible']} visible) {info['counts']}; "
                  f"frames: {len(info['frames'])}")
            write_json(out / "irb_form_baseline.json", {"form": info, "elements": list(base.values())})
            print(f"   wrote {out / 'irb_form_baseline.json'}")

            if args.phase == "probe":
                result = probe(form, reopen, recorder, max_tests=args.max_tests, delay_s=args.delay)
                write_json(out / "irb_rule_map.json", result.rules)
                print(f"4. tried {result.tried} actions; {len(result.rules)} had an effect; "
                      f"reopened the form {result.reopened} times; carried on from a changed state "
                      f"{result.rebased} times"
                      f"{' (STOPPED EARLY: too many reopens)' if result.stopped_early else ''}")
                for label, items in (
                    ("skipped as unsafe", result.skipped_unsafe),
                    ("could not be operated", result.not_operable),
                    ("gone after a reopen", result.missing_after_reopen),
                ):
                    if items:
                        print(f"   {label}: {len(items)}: {', '.join(items[:8])}{' ...' if len(items) > 8 else ''}")
                print(f"   wrote {out / 'irb_rule_map.json'}")
        except (ProbeError, PlaywrightError) as exc:
            code = 1
            print(f"FAILED: {type(exc).__name__}: {exc}")
        finally:
            # Whatever happened after the login (even an unexpected error): take the session as it
            # is NOW (cookies and the e-IRB's header form may have changed during the run) so the
            # e-IRB's own logout can be sent once the browser is closed.
            if logged_in:
                for candidate in [gate, *reversed(context.pages)]:  # the gate may be mid-navigation or closed
                    try:
                        candidate.wait_for_load_state("domcontentloaded", timeout=5000)
                        ua = candidate.evaluate("navigator.userAgent")
                        cookies = [
                            c for c in context.cookies() if c.get("domain", "").lstrip(".").endswith("r-bay.co.kr")
                        ]
                        session = RbaySession(args.user, agency, cookies, ua, candidate.content(), {})
                        break
                    except PlaywrightError:
                        continue
                if session is None:
                    print("the session could not be read for the logout; it ends by R-BAY's idle timeout")
            browser.close()
            # Here, not after the block: an unexpected error must not skip the logout.
            if session is not None:
                print(f"5. logout accepted: {logout(session)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
