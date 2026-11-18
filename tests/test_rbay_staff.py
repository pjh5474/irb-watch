"""The manager page parser (crawlers/rbay_contacts.parse_staff), on the markup the owner copied."""

import pytest

from irb_watch.crawlers.errors import LayoutChangedError
from irb_watch.crawlers.rbay_contacts import MAX_STAFF_ENTRIES, parse_staff

SEEN = """
<div class="ct_body_wrap">
<ul class="cts_tabbox">
<li><button class="on" type="button"><span>IRB </span></button></li>
<li><button type="button"><span>임상시험센터(연구행정팀)</span></button></li>
<li><button type="button"><span>QA, 통계, DRB 등</span></button></li>
</ul>
<div class="cts_tab_view cts_tab_view1" style="display: flex;">
<div class="ct_tel_guidebox"><p class="cttg_title">IRB (정규/신속) 심의 A패널 </p><p class="cttg_name">IRB 행정간사 </p>
<p class="cttg_email"> irb@schmc.ac.kr </p><p class="cttg_tel"> 032-621-6363</p></div>
<div class="ct_tel_guidebox"><p class="cttg_title">기타 문의</p><p class="cttg_name">IRB 행정간사 </p>
<p class="cttg_email"> irb@schmc.ac.kr </p><p class="cttg_tel"> 032-621-6350, 6363</p></div>
</div>
<div class="cts_tab_view cts_tab_view2" style="display: none;">
<div class="ct_tel_guidebox"><p class="cttg_title">연구계약, 연구비 사용, 모니터링 </p><p class="cttg_name">계약 담당자</p>
<p class="cttg_email">ctc@schmc.ac.kr</p><p class="cttg_tel">032-621-6358</p></div>
</div>
<div class="cts_tab_view cts_tab_view3" style="display: none;">
<div class="ct_tel_guidebox"><p class="cttg_title">임상약국</p><p class="cttg_name">임상약국담당자</p>
<p class="cttg_email">keumsk@schmc.ac.kr</p><p class="cttg_tel">032-621-5795</p></div>
</div>
</div>
"""


def test_the_seen_page_gives_three_tabs_with_their_cards():
    groups = parse_staff(SEEN)
    assert [g.tab for g in groups] == ["IRB", "임상시험센터(연구행정팀)", "QA, 통계, DRB 등"]
    first = groups[0].entries[0]
    assert (first.title, first.name, first.email, first.tel) == (
        "IRB (정규/신속) 심의 A패널", "IRB 행정간사", "irb@schmc.ac.kr", "032-621-6363")
    assert groups[0].entries[1].tel == "032-621-6350, 6363"
    assert len(groups[1].entries) == 1 and len(groups[2].entries) == 1


def test_hidden_tabs_are_read_too():
    assert sum(len(g.entries) for g in parse_staff(SEEN)) == 4  # tabs 2 and 3 are display:none


def test_a_page_with_no_cards_and_no_structure_is_empty_not_an_error():
    assert parse_staff("<html><body><a>로그아웃</a></body></html>") == []


def test_a_hospital_with_no_managers_filled_in_gives_an_empty_list_not_a_failure():
    html = (
        '<div class="contents_wrap"><div class="ct_sub_title">업무별 담당자 안내</div>'
        '<div class="ct_body_wrap">\n<ul class="cts_tabbox">\n</ul>\n</div></div>'
    )
    assert parse_staff(html) == []


def test_an_empty_tab_structure_is_a_layout_change_never_no_managers():
    html = '<ul class="cts_tabbox"><li><button>IRB</button></li></ul><div class="cts_tab_view cts_tab_view1"></div>'
    with pytest.raises(LayoutChangedError):
        parse_staff(html)


def test_cards_with_no_tab_wrapper_are_one_group_without_a_name():
    html = '<div class="ct_tel_guidebox"><p class="cttg_title">IRB</p><p class="cttg_tel">02-1</p></div>'
    groups = parse_staff(html)
    assert len(groups) == 1 and groups[0].tab == "" and groups[0].entries[0].tel == "02-1"


def test_a_wrapper_whose_class_only_contains_the_name_does_not_shift_the_tabs():
    wrapped = SEEN.replace(
        '<div class="cts_tab_view cts_tab_view1"',
        '<div class="cts_tab_view_wrap"><div class="cts_tab_view cts_tab_view1"', 1,
    ).replace("</div>\n<div class=\"cts_tab_view cts_tab_view2\"", "</div></div>\n<div class=\"cts_tab_view cts_tab_view2\"", 1)
    groups = parse_staff(wrapped)
    assert [g.tab for g in groups] == ["IRB", "임상시험센터(연구행정팀)", "QA, 통계, DRB 등"]
    assert sum(len(g.entries) for g in groups) == 4  # no entry counted twice


def test_loose_cards_skip_empty_ones_and_respect_the_cap():
    empty_card = '<div class="ct_tel_guidebox"><p class="cttg_title"> </p></div>'
    card = '<div class="ct_tel_guidebox"><p class="cttg_title">x</p></div>'
    html = empty_card * 5 + card * (MAX_STAFF_ENTRIES + 30)
    groups = parse_staff(html)
    assert len(groups) == 1 and len(groups[0].entries) == MAX_STAFF_ENTRIES


def test_a_card_missing_a_field_gives_an_empty_text_for_it():
    html = '<div class="cts_tab_view1"><div class="ct_tel_guidebox"><p class="cttg_title">DRB</p></div></div>'
    entry = parse_staff(html)[0].entries[0]
    assert entry.title == "DRB" and entry.name == "" and entry.email == "" and entry.tel == ""


def test_markup_inside_a_card_stays_plain_text():
    html = ('<div class="cts_tab_view1"><div class="ct_tel_guidebox"><p class="cttg_title">'
            "&lt;img src=x onerror=1&gt; IRB</p></div></div>")
    assert parse_staff(html)[0].entries[0].title == "<img src=x onerror=1> IRB"


def test_the_total_is_capped():
    card = '<div class="ct_tel_guidebox"><p class="cttg_title">x</p></div>'
    html = f'<div class="cts_tab_view1">{card * (MAX_STAFF_ENTRIES + 20)}</div>'
    assert sum(len(g.entries) for g in parse_staff(html)) == MAX_STAFF_ENTRIES
