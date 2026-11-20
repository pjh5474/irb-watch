"""The R-BAY form probe, against a local fake form in a real headless Chromium (no R-BAY)."""

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from irb_watch.scripts.rbay_form_probe import (
    Recorder,
    diff,
    is_unsafe,
    open_form,
    plan_trials,
    probe,
    snapshot,
)

FORM = """
<html><body>
<table><tr><th>연구 유형</th><td>
  <select id="typ"><option value="">선택</option><option value="X">X</option><option value="Y">Y</option>
  <option value="Z">Z</option></select></td></tr></table>
<label><input type="checkbox" id="chk" name="agree" value="Y"> 동의</label>
<div id="extra" style="display:none"><input type="text" id="why" name="why"></div>
<label><input type="radio" name="kind" value="A" checked> A</label>
<label><input type="radio" name="kind" value="B"> B</label>
<select id="sub" disabled><option value="1">1</option></select>
<input type="text" id="yfield" name="yfield" style="display:none">
<label><input type="checkbox" id="del" name="del" value="1"> 삭제</label>
<button id="go" onclick="window.submitted = true">제출</button>
<script>
  chk.onchange = () => { extra.style.display = chk.checked ? 'block' : 'none'; why.required = chk.checked; };
  document.querySelectorAll('[name=kind]').forEach(r => r.onchange = () =>
    { sub.disabled = document.querySelector('[name=kind]:checked').value !== 'B'; });
  typ.onchange = () => {
    yfield.style.display = typ.value === 'Y' ? 'inline' : 'none';
    if (typ.value === 'Z') confirm('진행하시겠습니까?');
  };
  del.onchange = () => alert('deleted!');
</script></body></html>
"""


@pytest.fixture
def page():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PlaywrightError:
            pytest.skip("Chromium is not installed (uv run playwright install chromium)")
        context = browser.new_context()
        p = context.new_page()
        p.set_content(FORM)
        yield p, context
        browser.close()


def test_snapshot_records_the_fields_with_label_state_and_options(page):
    p, _ = page
    snap = snapshot(p)
    typ = snap["select#typ"]
    assert typ["label"] == "연구 유형" and [o["value"] for o in typ["options"]] == ["", "X", "Y", "Z"]
    assert snap["input#chk"]["label"] == "동의" and snap["input#chk"]["visible"]
    assert not snap["input#why"]["visible"] and not snap["input#yfield"]["visible"]
    assert snap["select#sub"]["disabled"]
    assert snap['input[name="kind"][value="B"]']["type"] == "radio"
    assert snap["button#go"]["text"] == "제출"


def test_plan_skips_unsafe_controls_and_never_plans_a_button():
    base = {
        "a": {"type": "checkbox", "tag": "input", "visible": True, "disabled": False, "checked": False,
              "value": "1", "label": "삭제", "id": "", "name": "", "text": ""},
        "b": {"type": "button", "tag": "button", "visible": True, "disabled": False, "checked": False,
              "value": "", "label": "", "id": "", "name": "", "text": "제출"},
        "c": {"type": "checkbox", "tag": "input", "visible": True, "disabled": False, "checked": True,
              "value": "Y", "label": "동의", "id": "", "name": "", "text": ""},
    }
    trials, skipped = plan_trials(base, 50)
    assert skipped == ["a"] and [(t.key, t.action_value) for t in trials] == [("c", "unchecked:Y")]
    assert is_unsafe({"label": "Submit now"}) and not is_unsafe({"label": "동의"})


def test_plan_skips_an_unsafe_select_option_but_keeps_the_others():
    sel = {"type": "select-one", "tag": "select", "visible": True, "disabled": False, "checked": False,
           "value": "", "label": "처리구분", "id": "", "name": "", "text": "",
           "options": [{"value": "", "text": "선택", "disabled": False, "selected": True},
                       {"value": "S", "text": "제출", "disabled": False, "selected": False},
                       {"value": "K", "text": "보관", "disabled": False, "selected": False}]}
    trials, skipped = plan_trials({"s": sel}, 50)
    assert [t.value for t in trials] == ["K"] and skipped == ['s::option[value="S"]']


def test_a_radio_nobody_selected_is_cleared_by_script_and_the_ticking_timer_is_ignored(page):
    p, context = page
    html = "<html><body><label><input type='radio' name='r' value='A'> A</label>" \
           "<label><input type='radio' name='r' value='B'> B</label>" \
           "<input id='sessionTimer' value='29:58'>" \
           "<script>let n = 0; setInterval(() => { sessionTimer.value = '29:' + (n++); }, 50);</script>" \
           "</body></html>"
    p.set_content(html)
    reopened = []

    def reopen():
        reopened.append(1)
        p.set_content(html)
        return p

    result = probe(p, reopen, Recorder(), delay_s=0, settle_ms=120)
    assert result.tried == 2 and not reopened and not result.stopped_early
    assert p.locator("input[name=r]:checked").count() == 0


def test_a_hidden_custom_checkbox_is_restored_through_its_label(page):
    p, context = page
    html = ("<html><body><style>input.c{position:absolute;opacity:0;width:0;height:0}</style>"
            "<input class='c' type='checkbox' id='a'><label for='a'>I</label>"
            "<input type='checkbox' id='b'><label for='b'>II</label>"
            "<script>a.onchange = () => { b.disabled = a.checked; };</script></body></html>")
    p.set_content(html)
    reopened = []
    result = probe(p, lambda: reopened.append(1) or p, Recorder(), delay_s=0, settle_ms=50)
    assert not reopened and result.rules["input#a"]["effects"] == {"disable": ["input#b"]}
    assert not p.locator("#a").is_checked() and not p.locator("#b").is_disabled()


def test_the_probe_stops_instead_of_reopening_without_end(page):
    p, context = page
    html = "<html><body><input type='checkbox' id='a'><input type='checkbox' id='b'>" \
           "<input type='checkbox' id='c'><input type='checkbox' id='d'><input type='checkbox' id='e'>" \
           "<script>document.querySelectorAll('input').forEach(i => i.onchange = () => { i.dataset.x = 1; " \
           "i.disabled = true; });</script></body></html>"  # a box that locks itself: never restorable
    p.set_content(html)
    reopened = []

    def reopen():
        reopened.append(1)
        p.set_content(html)
        return p

    result = probe(p, reopen, Recorder(), delay_s=0, settle_ms=50, max_reopens=2)
    assert result.stopped_early and len(reopened) == 2


def test_by_default_an_undoable_change_is_carried_on_from_and_never_reopens(page):
    p, context = page
    html = ("<html><body><label><input type='radio' name='r' id='ra' value='A'> A</label>"
            "<label><input type='radio' name='r' id='rb' value='B'> B</label>"
            "<input type='checkbox' id='c'><div id='d' style='display:none'><input id='x'></div>"
            "<script>[ra, rb].forEach(r => r.onclick = () => { d.style.display = 'block'; });"
            "c.onchange = () => { c.disabled = true; };</script></body></html>")
    p.set_content(html)
    reopened = []
    result = probe(p, lambda: reopened.append(1) or p, Recorder(), delay_s=0, settle_ms=50)
    assert not reopened and result.rebased >= 1
    assert result.rules["input#ra"]["effects"] == {"show": ["input#x"]}
    assert "input#ra" in result.rules and not result.rules["input#ra"].get("from_changed_state")
    assert "input#rb" not in result.rules  # the field was already shown: no effect from this state


GATE_POPUP = """<html><body><a href="#" class="mnuLink" data-link="/mng/rvw/testRegTempApl" data-mnu-nm="신규계획"
  onclick="window.open('about:blank','_blank'); return false;">신규계획</a></body></html>"""
GATE_SAME = """<html><body><a href="#" class="mnuLink" data-link="/mng/rvw/testRegTempApl" data-mnu-nm="신규계획"
  onclick="document.body.innerHTML = '<input id=x>'; return false;">신규계획</a></body></html>"""


def test_open_form_finds_a_new_window_or_the_same_window(page):
    p, context = page
    p.set_content(GATE_POPUP)
    form, popup = open_form(context, p, wait_s=3)
    assert popup and form is not p
    p.set_content(GATE_SAME)
    form, popup = open_form(context, p, wait_s=1)
    assert not popup and form is p and p.locator("#x").count() == 1


def test_diff_names_show_hide_enable_required_and_new_fields():
    def el(**kw):
        base = {"visible": True, "disabled": False, "required": False, "tag": "input", "type": "text",
                "value": "", "label": ""}
        return {**base, **kw}

    before = {"x": el(), "y": el(visible=False), "z": el(disabled=True), "w": el(), "gone": el()}
    after = {"x": el(visible=False), "y": el(), "z": el(), "w": el(required=True), "new": el(), "clicked": el()}
    fx = diff(before, after, "clicked")
    assert fx == {"show": ["y", "new"], "hide": ["x", "gone"], "enable": ["z"], "required": ["w"]}


def test_probe_finds_the_rules_dismisses_dialogs_and_touches_nothing_unsafe(page):
    p, context = page
    recorder = Recorder()
    recorder.attach(context)
    result = probe(p, lambda: (p.set_content(FORM), p)[1], recorder, delay_s=0, settle_ms=50)

    rules = result.rules
    assert rules["input#chk"]["action_value"] == "Y"
    assert rules["input#chk"]["effects"]["show"] == ["input#why"]
    assert rules["input#chk"]["effects"]["required"] == ["input#why"]
    assert rules['input[name="kind"][value="B"]']["effects"] == {"enable": ["select#sub"]}
    assert rules['select#typ::option[value="Y"]']["effects"] == {"show": ["input#yfield"]}
    # the confirm was dismissed, its text recorded
    assert rules['select#typ::option[value="Z"]']["dialogs"] == ["진행하시겠습니까?"]
    # the unsafe checkbox was never clicked (its alert never ran) and the button never pressed
    assert "input#del" in result.skipped_unsafe and "input#del" not in rules
    assert p.evaluate("window.submitted === undefined")
    assert p.locator("#del").is_checked() is False
    # the form is back as it was
    assert p.locator("#chk").is_checked() is False and p.locator("#typ").input_value() == ""
