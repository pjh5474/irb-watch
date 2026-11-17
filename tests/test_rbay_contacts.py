"""The R-BAY contact cards parser (crawlers/rbay_contacts.py), on the markup the owner copied."""

import pytest

from irb_watch.crawlers.errors import LayoutChangedError
from irb_watch.crawlers.rbay_contacts import MAX_FIELD_CHARS, parse_contacts

SEEN = """
<ul class="ctn_difx_box cdb2 ">
  <li class="ct_guide_telbox">
    <div class="ct_int_title2"><em>전화</em></div>
    <div class="ctn_gdb_text ck-content"><p>031-999-1158&nbsp; IRB&nbsp;</p></div>
  </li>
  <li class="ct_guide_faxbox">
    <div class="ct_int_title2"><em>팩스</em></div>
    <div class="ctn_gdb_text ck-content"></div>
  </li>
  <li class="ct_guide_emailbox">
    <div class="ct_int_title2"><em>이메일</em></div>
    <div class="ctn_gdb_text ck-content"><p>irb@gwhmail.com&nbsp; IRB</p></div>
  </li>
</ul>
"""


def test_the_seen_markup_gives_phone_and_email_and_an_empty_fax():
    got = parse_contacts(SEEN)
    assert got.card_found
    assert got.phone == "031-999-1158 IRB"
    assert got.fax == ""
    assert got.email == "irb@gwhmail.com IRB"


def test_several_paragraphs_and_line_breaks_stay_separate_lines():
    html = """<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ctn_gdb_text">
        <p>02-111-2222 (심사)</p><p>02-111-3333<br>02-111-4444</p></div></li></ul>"""
    assert parse_contacts(html).phone == "02-111-2222 (심사)\n02-111-3333\n02-111-4444"


def test_a_hospital_list_with_line_breaks_and_dashes_gives_clean_lines():
    html = (
        '<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ct_int_title2"><em>전화</em></div>'
        '<div class="ctn_gdb_text ck-content"><p>- 심의(A패널), 문서보관: 032-621-6363<br>'
        "- 심의(B패널), 심의비: 032-621-6350<br>- 계약,연구비,모니터링: 032-621-6358<br>"
        "- 연구대상자보호센터(내부점검): 032-621-6945<br>- 임상약국: 032-621-5795<br>"
        "- 데이터심의위원회(DRB,의료원): 02-710-3245<br>- 데이터심의위원회(DRB,부천병원): 02-621-5586"
        "</p></div></li></ul>"
    )
    lines = parse_contacts(html).phone.split("\n")
    assert len(lines) == 7
    assert lines[0] == "심의(A패널), 문서보관: 032-621-6363"
    assert lines[6] == "데이터심의위원회(DRB,부천병원): 02-621-5586"
    assert all(not line.startswith("-") for line in lines)


def test_a_dash_inside_a_number_is_kept():
    html = '<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ctn_gdb_text"><p>031-999-1158</p></div></li></ul>'
    assert parse_contacts(html).phone == "031-999-1158"


def test_text_outside_a_paragraph_is_not_dropped():
    html = ('<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ctn_gdb_text">'
            "02-123-4567<p>IRB office</p><div>팩스 02-9</div><ul><li>02-5</li></ul></div></li></ul>")
    assert parse_contacts(html).phone == "02-123-4567\nIRB office\n팩스 02-9\n02-5"


def test_inline_markup_does_not_split_a_number():
    html = ('<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ctn_gdb_text">'
            "<p><strong>02</strong>-123-<span>4567</span> (IRB)</p></div></li></ul>")
    assert parse_contacts(html).phone == "02-123-4567 (IRB)"


def test_text_without_paragraphs_is_read_too():
    html = '<ul class="ctn_difx_box"><li class="ct_guide_faxbox"><div class="ctn_gdb_text">02-1 / 02-2</div></li></ul>'
    assert parse_contacts(html).fax == "02-1 / 02-2"


def test_a_missing_card_is_an_empty_field_and_the_page_still_has_cards():
    html = '<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ctn_gdb_text"><p>1</p></div></li></ul>'
    got = parse_contacts(html)
    assert got.card_found and got.phone == "1" and got.fax == "" and got.email == ""


def test_a_start_page_with_no_card_says_so():
    got = parse_contacts("<html><body><button>입장하기</button></body></html>")
    assert not got.card_found
    assert (got.phone, got.fax, got.email) == ("", "", "")


def test_the_manager_page_tile_tells_that_the_hospital_has_one():
    tile = (
        '<li class="ct_int_work " id="workMngr"><span></span><div class="ct_int_title1">'
        "<em>업무별 담당자</em></div><a href=\"#\"><em>바로가기</em></a></li>"
    )
    assert parse_contacts(SEEN + tile).has_staff_page
    assert not parse_contacts(SEEN).has_staff_page


def test_the_link_in_the_intro_block_is_the_same_signal():
    intro = (
        '<li><div class="ct_int_title1"><em>IRB INTRODUCTION</em></div>'
        '<a class="ct_int_worklink" href="#" id="workMngr"><em>업무별 담당자 안내</em><span></span></a></li>'
    )
    assert parse_contacts(SEEN + intro).has_staff_page


def test_another_element_with_a_similar_name_is_not_the_signal():
    other = '<a class="ct_int_worklink" href="#" id="workMngrX"><em>x</em></a><li class="ct_int_work"></li>'
    assert not parse_contacts(SEEN + other).has_staff_page


def test_a_page_that_is_not_a_start_page_is_a_layout_change():
    with pytest.raises(LayoutChangedError):
        parse_contacts("<html><body><h1>점검중입니다</h1></body></html>")


def test_markup_in_the_text_stays_plain_text():
    html = ('<ul class="ctn_difx_box"><li class="ct_guide_emailbox"><div class="ctn_gdb_text">'
            "<p>a@b.kr &lt;script&gt;x&lt;/script&gt;</p></div></li></ul>")
    assert parse_contacts(html).email == "a@b.kr <script>x</script>"


def test_a_very_long_card_is_cut():
    html = f'<ul class="ctn_difx_box"><li class="ct_guide_telbox"><div class="ctn_gdb_text"><p>{"9" * 900}</p></div></li></ul>'
    assert len(parse_contacts(html).phone) == MAX_FIELD_CHARS


def test_a_card_with_no_text_box_is_empty():
    html = '<ul class="ctn_difx_box"><li class="ct_guide_telbox"><em>전화</em></li></ul>'
    got = parse_contacts(html)
    assert got.card_found and got.phone == ""
