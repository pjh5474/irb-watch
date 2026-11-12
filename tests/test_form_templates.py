"""Docs generator: form templates (owner decisions 2026-11-11). A hospital's blank DOCX form;
the AI answers per field; export fills a copy of the form."""

import base64
import io
import zipfile
from dataclasses import replace

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_docgen_service, get_user_store, get_verifier
from irb_watch.docgen.form_fields import MAX_FIELDS, FormError, detect_fields
from irb_watch.docgen.form_fill import fill, render_markdown
from irb_watch.docgen.prompt import FORM_INSTRUCTIONS
from irb_watch.main import app
from irb_watch.services.docgen import (
    DocChoice,
    DocgenService,
    FieldEdit,
    UploadedFile,
)
from irb_watch.services.errors import (
    AuditUnavailableError,
    ConflictError,
    NotFoundError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeTemplateFiles, FakeWriter
from tests.test_admin_users_api import FakeVerifier

A, B = 1, 2
BOX, TICK = chr(0x2610), chr(0x2611)


def form_bytes(title_marker: str = "연구제목") -> bytes:
    d = Document()
    p = d.add_paragraph()
    p.add_run("연구 제목: {{" + title_marker[:1])  # Word splits a marker into pieces
    p.add_run(title_marker[1:] + "}} 끝")
    d.add_paragraph(f"신청 구분 {BOX} 최초 {BOX} 변경")  # body/1
    d.add_paragraph("연구 기간:")  # body/2
    d.add_paragraph("")  # body/3: the blank after a ':' line
    d.add_paragraph("책임자 서명 ________ 날짜 ______")  # body/4
    t = d.add_table(rows=3, cols=3)  # body/5
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(0, 2).text = "항목", "변경 전", "변경 후"
    t.cell(1, 0).text = "대상자 수"
    t2 = d.add_table(rows=1, cols=2)  # body/6
    t2.cell(0, 0).text = "연구 번호"
    d.add_paragraph("위험 증가")  # body/7
    d.add_paragraph(f"{BOX} 예")  # body/8: one box per line, grouped
    d.add_paragraph(f"{BOX} 아니오")
    d.add_paragraph("다시 {{" + title_marker + "}}")  # the same marker again
    d.sections[0].header.paragraphs[0].text = "IRB No. {{IRB번호}}"
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def b64(content: bytes) -> str:
    return base64.b64encode(content).decode()


def keys(fields) -> dict:
    return {f.key: f for f in fields}


def read_docx(content: bytes) -> str:
    d = Document(io.BytesIO(content))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts += [c.text for c in row.cells]
    parts += [p.text for p in d.sections[0].header.paragraphs]
    return "\n".join(parts)


# -- finding fields ------------------------------------------------------------------------


def test_fields_are_found_markers_first():
    fields = detect_fields("f.docx", form_bytes())
    k = keys(fields)
    assert [f.key for f in fields[:2]] == ["m:연구제목", "m:IRB번호"]  # split marker, header marker
    assert sum(1 for f in fields if f.key == "m:연구제목") == 1  # repeated: one field
    assert k["c:body/1"].kind == "choice" and k["c:body/1"].options == ("최초", "변경")
    assert k["c:body/1"].label == "신청 구분"
    assert k["c:body/8"].options == ("예", "아니오") and k["c:body/8"].label == "위험 증가"
    assert k["a:body/3"].label == "연구 기간"
    assert k["a:body/4#u0"].label == "책임자 서명" and k["a:body/4#u1"].label == "날짜"
    assert k["a:body/5/r1/c1"].label == "대상자 수 / 변경 전"
    assert k["a:body/5/r1/c2"].label == "대상자 수 / 변경 후"
    assert k["a:body/5/r2/c0"].label == "항목 2"  # first column: the column's top header
    assert k["a:body/6/r0/c1"].label == "연구 번호"
    assert all(f.source == "auto" for f in fields[2:])


def test_detection_is_the_same_for_the_same_file():
    content = form_bytes()
    assert detect_fields("f.docx", content) == detect_fields("f.docx", content)


@pytest.mark.parametrize("name", ["form.doc", "form.docm", "form.hwp", "form.hwpx"])
def test_other_formats_are_refused_with_how_to_fix_it(name):
    with pytest.raises(FormError, match="save it as .docx"):
        detect_fields(name, form_bytes())


def test_a_file_that_is_not_a_docx_is_refused():
    with pytest.raises(FormError, match="not a valid .docx"):
        detect_fields("f.docx", b"not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(FormError, match="not a Word .docx"):
        detect_fields("f.docx", buf.getvalue())


def test_a_zip_bomb_and_macros_are_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "0" * 31_000_000)  # compresses to a few kB
    assert len(buf.getvalue()) < 1_000_000
    with pytest.raises(FormError, match="unpacks to more than"):
        detect_fields("f.docx", buf.getvalue())
    macro = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(form_bytes())) as src, zipfile.ZipFile(macro, "w") as z:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"document.main+xml", b"document.macroEnabled.main+xml")
            z.writestr(item, data)
    with pytest.raises(FormError, match="macros"):
        detect_fields("f.docx", macro.getvalue())


def test_too_many_fields_or_none_are_refused():
    d = Document()
    for i in range(MAX_FIELDS + 1):
        d.add_paragraph("{{f" + str(i) + "}}")
    out = io.BytesIO()
    d.save(out)
    with pytest.raises(FormError, match=f"at most {MAX_FIELDS}"):
        detect_fields("f.docx", out.getvalue())
    d = Document()
    d.add_paragraph("A plain letter with no blanks.")
    out = io.BytesIO()
    d.save(out)
    with pytest.raises(FormError, match="no field was found"):
        detect_fields("f.docx", out.getvalue())


# -- filling -------------------------------------------------------------------------------


def test_fill_writes_answers_ticks_boxes_and_keeps_the_layout():
    content = form_bytes()
    fields = detect_fields("f.docx", content)
    values = {
        "m:연구제목": "심부전 연구",
        "c:body/1": ["변경"],
        "a:body/3": "2026-01-01 ~\n2027-12-31",
        "a:body/4#u0": "홍길동",
        "a:body/5/r1/c1": "100명",
        "a:body/6/r0/c1": "",  # left empty on purpose
        "c:body/8": ["아니오"],
    }
    text = read_docx(fill("f.docx", content, fields, values))
    assert "연구 제목: 심부전 연구 끝" in text and "다시 심부전 연구" in text
    assert f"신청 구분 {BOX} 최초 {TICK} 변경" in text
    assert "2026-01-01 ~\n2027-12-31" in text
    assert "책임자 서명 홍길동 날짜 [확인 필요: 날짜]" in text  # unanswered: a placeholder
    assert f"{BOX} 예" in text and f"{TICK} 아니오" in text
    assert "IRB No. [확인 필요: IRB번호]" in text
    d = Document(io.BytesIO(fill("f.docx", content, fields, values)))
    assert d.tables[1].cell(0, 1).text == ""  # empty on purpose stays empty
    assert d.tables[0].cell(1, 1).text == "100명"
    assert d.tables[0].cell(0, 1).text == "변경 전"  # labels untouched
    assert "{{" not in text


def test_a_marker_the_admin_removed_becomes_nothing():
    content = form_bytes()
    fields = tuple(f for f in detect_fields("f.docx", content) if f.key != "m:IRB번호")
    text = read_docx(fill("f.docx", content, fields, {}))
    assert "IRB No. " in text and "{{" not in text and "IRB번호" not in text


def test_an_answer_that_looks_like_a_marker_or_a_line_stays_as_written():
    content = form_bytes()
    fields = detect_fields("f.docx", content)
    values = {
        "m:연구제목": "{{IRB번호}} 참고",
        "a:body/4#u0": "홍길동 ____",
        "a:body/3": "{{연구제목}}",
    }
    text = read_docx(fill("f.docx", content, fields, values))
    assert "연구 제목: {{IRB번호}} 참고 끝" in text
    assert "책임자 서명 홍길동 ____ 날짜 [확인 필요: 날짜]" in text
    assert "\n{{연구제목}}\n" in text


def test_tracked_change_marks_are_not_copied_into_answers():
    d = Document()
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "성명"
    p = t.cell(0, 1).paragraphs[0]
    rpr = OxmlElement("w:rPr")
    p._p.get_or_add_pPr().append(rpr)
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "1")
    ins.set(qn("w:author"), "x")
    rpr.append(ins)
    rpr.append(OxmlElement("w:b"))
    out = io.BytesIO()
    d.save(out)
    content = out.getvalue()
    fields = detect_fields("f.docx", content)
    filled = Document(io.BytesIO(fill("f.docx", content, fields, {fields[0].key: "홍길동"})))
    run = filled.tables[0].cell(0, 1).paragraphs[0].runs[0]._r
    assert run.find(qn("w:rPr")).find(qn("w:ins")) is None
    assert run.find(qn("w:rPr")).find(qn("w:b")) is not None  # the look is kept
    assert filled.tables[0].cell(0, 1).text == "홍길동"


def test_blanks_inside_tick_box_lines_and_content_controls_are_found():
    d = Document()
    d.add_paragraph(f"연구 유형 {BOX} 관찰 {BOX} 중재 {BOX} 기타(________)")
    d.add_paragraph(f"{BOX} 예")
    d.add_paragraph(f"{BOX} 기타: ______")
    body = d.element.body
    sdt = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    sdt.append(content)
    body.insert(len(body) - 1, sdt)  # before the section properties
    p1 = OxmlElement("w:p")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "연구 목적:"
    r.append(t)
    p1.append(r)
    content.append(p1)
    content.append(OxmlElement("w:p"))
    out = io.BytesIO()
    d.save(out)
    data = out.getvalue()
    fields = detect_fields("f.docx", data)
    k = keys(fields)
    assert k["c:body/0"].options == ("관찰", "중재", "기타(________)")
    assert k["a:body/0#u0"].label == "기타"
    assert k["c:body/1"].options == ("예", "기타: ______")
    assert k["a:body/2#u0"].label == "기타 (2)"  # labels are made unique
    assert k["a:body/4"].label == "연구 목적"  # the blank line inside the content control
    values = {"c:body/0": ["기타(________)"], "a:body/0#u0": "후향적", "a:body/4": "목적"}
    text = read_docx(fill("f.docx", data, fields, values))
    assert f"{TICK} 기타(후향적)" in text


def test_markdown_copy_lists_every_field():
    fields = detect_fields("f.docx", form_bytes())
    md = render_markdown("제목", fields, {"m:연구제목": "X", "c:body/1": ["최초"]})
    assert md.startswith("# 제목") and "## 연구제목\n\nX" in md
    assert "[x] 최초 [ ] 변경" in md and "[확인 필요: IRB번호]" in md


# -- the service ---------------------------------------------------------------------------


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    audit = FakeAudit()
    store = FakeDocgenStore(audit)
    store.add_source(A, "Hospital A")
    store.add_source(B, "Hospital B")
    store.add_document(10, A, "Amendment notice")
    store.add_document(20, B, "Other notice")
    writer = FakeWriter(title="변경 신청")
    files = FakeTemplateFiles()
    service = DocgenService(users, store, writer, audit, daily_limit=10, clock=lambda: NOW, files=files)
    return service, store, writer, audit, files


def upload(content=None, name="form.docx"):
    return UploadedFile(name, b64(content if content is not None else form_bytes()))


def ready_template(service, sources=(A,)):
    t = service.create_template("admin-1", "변경신청서", "amendment", upload())
    service.save_template_fields(
        "admin-1", t.id, [FieldEdit(f.key, f.label, f.hint, f.multi) for f in t.fields]
    )
    if sources:
        service.set_template_hospitals("admin-1", t.id, list(sources))
    return service.template("admin-1", t.id)


def test_a_new_template_waits_for_the_field_check(env):
    service, store, _, audit, files = env
    t = service.create_template("admin-1", "변경신청서", "amendment", upload())
    assert t.status == "review" and t.storage_path in files.files
    assert audit.entries[-1]["event_type"] == "docgen.template_created"
    service.set_template_hospitals("admin-1", t.id, [A])
    assert service.templates_for("user-1", [A]) == []
    with pytest.raises(NotFoundError):
        service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_id=t.id)
    with pytest.raises(ConflictError, match="field check"):
        service.create_draft("admin-1", [DocChoice(10)], None, [], "", [], template_id=t.id)


def test_review_keeps_renames_and_restores_fields(env):
    service, *_ = env
    t = service.create_template("admin-1", "변경신청서", "", upload())
    first = t.fields[0]
    saved = service.save_template_fields(
        "admin-1", t.id, [FieldEdit(first.key, "  연구   과제명 ", "한글로", False)]
    )
    assert saved.status == "ready" and len(saved.fields) == 1
    assert saved.fields[0].label == "연구 과제명" and saved.fields[0].hint == "한글로"
    # A removed field can be added back from the detected list.
    detected = service.detected_fields("admin-1", t.id)
    assert len(detected) == len(t.fields)
    choice = next(f for f in detected if f.kind == "choice")
    again = service.save_template_fields(
        "admin-1", t.id, [FieldEdit(first.key, "a"), FieldEdit(choice.key, "b", multi=True)]
    )
    assert [f.multi for f in again.fields] == [False, True]
    with pytest.raises(ValidationError, match="no field"):
        service.save_template_fields("admin-1", t.id, [FieldEdit("m:nope", "x")])
    with pytest.raises(ValidationError, match="twice"):
        service.save_template_fields("admin-1", t.id, [FieldEdit(first.key, "x"), FieldEdit(first.key, "y")])
    with pytest.raises(ValidationError, match="at least one"):
        service.save_template_fields("admin-1", t.id, [])


def test_which_templates_a_user_is_offered(env):
    service, *_ = env
    linked = ready_template(service, sources=(A,))
    glob = ready_template(service, sources=())
    service.update_template("admin-1", glob.id, None, None, True)
    assert [t.id for t in service.templates_for("user-1", [A])] == [glob.id, linked.id]
    assert [t.id for t in service.templates_for("user-1", [B])] == [glob.id]
    with pytest.raises(NotFoundError):  # not offered for B's documents
        service.create_draft("user-1", [DocChoice(20)], None, [], "", [], template_id=linked.id)
    service.archive_template("admin-1", linked.id)
    assert [t.id for t in service.templates_for("user-1", [A])] == [glob.id]
    with pytest.raises(ConflictError, match="global"):
        service.set_template_hospitals("admin-1", glob.id, [A])


def test_a_template_draft_is_one_checked_answer_per_field(env):
    service, store, writer, audit, _ = env
    t = ready_template(service)
    writer.answers = {
        "m:연구제목": "심부전 연구",
        "c:body/1": ["변경", "최초"],  # one allowed: the first is kept
        "c:body/8": ["모름"],  # not an option: ignored
        "x:unknown": "dropped",
    }
    detail = service.create_draft("user-1", [DocChoice(10)], None, [], "변경 신청서 작성", [], template_id=t.id)
    instructions, text = writer.calls[-1]
    assert instructions.startswith(FORM_INSTRUCTIONS[:40]) and "Return one answer for EVERY key" in instructions
    assert "<form>" in text and "key: c:body/1 | label: 신청 구분 | type: choice (one)" in text
    v = detail.versions[0]
    assert v.fields["m:연구제목"] == "심부전 연구"
    assert v.fields["c:body/1"] == ["변경"] and v.fields["c:body/8"] == []
    assert v.fields["a:body/3"] == "[확인 필요: 연구 기간]"  # missing answer
    assert "x:unknown" not in v.fields
    assert v.markdown.startswith("# 변경 신청") and "## 연구제목" in v.markdown
    assert detail.draft.template.template_id == t.id
    assert audit.entries[-1]["payload"]["template"] == {"template_id": t.id, "filename": "form.docx"}


def test_hand_edits_of_a_template_draft_are_checked(env):
    service, _, writer, _, _ = env
    t = ready_template(service)
    detail = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_id=t.id)
    did = detail.draft.id
    with pytest.raises(ValidationError, match="send its answers"):
        service.save_edit("user-1", did, "# text")
    with pytest.raises(ValidationError, match='no field "nope"'):
        service.save_edit("user-1", did, "", fields={"nope": "x"})
    with pytest.raises(ValidationError, match='no option "모름"'):
        service.save_edit("user-1", did, "", fields={"c:body/1": ["모름"]})
    with pytest.raises(ValidationError, match="only one"):
        service.save_edit("user-1", did, "", fields={"c:body/1": ["최초", "변경"]})
    with pytest.raises(ValidationError, match="too long"):
        service.save_edit("user-1", did, "", fields={"m:연구제목": "x" * 4001})
    # "" for a tick-box group means nothing picked, as in the AI's answers.
    v = service.save_edit("user-1", did, "", fields={"c:body/1": ""})
    assert v.fields == {"c:body/1": []}
    v = service.save_edit("user-1", did, "", base_version_no=2, fields={"m:연구제목": "새 제목"})
    assert v.version_no == 3 and v.fields == {"m:연구제목": "새 제목"} and v.origin == "user_edit"
    assert service.save_edit("user-1", did, "", fields={"m:연구제목": "새 제목"}).version_no == 3
    # A Markdown draft refuses answers.
    plain = service.create_draft("user-1", [DocChoice(10)], None, [], "", [])
    with pytest.raises(ValidationError, match="has no form"):
        service.save_edit("user-1", plain.draft.id, "", fields={"a": "b"})


def test_revise_sends_the_current_answers_and_keeps_the_form(env):
    service, _, writer, _, _ = env
    t = ready_template(service)
    detail = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_id=t.id)
    service.save_edit("user-1", detail.draft.id, "", fields={"m:연구제목": "내가 고친 제목"})
    writer.answers = {"m:연구제목": "내가 고친 제목", "a:body/3": "2026년"}
    v = service.revise("user-1", detail.draft.id, "기간을 채워줘")
    instructions, text = writer.calls[-1]
    assert "<current_answers>" in text and "내가 고친 제목" in text and "REVISION" in instructions
    assert v.fields["a:body/3"] == "2026년" and v.origin == "ai_revision"


def test_export_fills_the_draft_s_own_copy_of_the_form(env):
    service, store, writer, _, files = env
    t = ready_template(service)
    writer.answers = {"m:연구제목": "심부전 연구", "c:body/1": ["최초"]}
    detail = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_id=t.id)
    # Replacing the template's file later changes nothing for this draft.
    new = service.replace_template_file("admin-1", t.id, upload(form_bytes("과제명")))
    assert new.status == "review" and new.storage_path != t.storage_path
    assert t.storage_path in files.files  # the old file is kept
    content, name = service.export_docx("user-1", detail.draft.id, None)
    text = read_docx(content)
    assert "연구 제목: 심부전 연구 끝" in text and f"{TICK} 최초" in text
    assert name.endswith("v1.docx")
    files.fail_get = OSError("down")
    with pytest.raises(UnavailableError, match="could not be read"):
        service.export_docx("user-1", detail.draft.id, None)


def test_a_replaced_file_keeps_the_admin_s_review_only_for_the_same_fields(env):
    service, *_ = env
    t = service.create_template("admin-1", "변경신청서", "", upload())
    service.save_template_fields(
        "admin-1",
        t.id,
        [
            FieldEdit("m:IRB번호", "IRB 승인번호", "숫자만"),
            FieldEdit("a:body/3", "연구 기간(시작~끝)", "YYYY-MM-DD"),
            FieldEdit("c:body/1", "구분"),
        ],  # everything else dropped
    )
    new = service.replace_template_file("admin-1", t.id, upload(form_bytes("과제명")))
    k = keys(new.fields)
    assert k["m:IRB번호"].label == "IRB 승인번호" and k["m:IRB번호"].hint == "숫자만"
    assert k["a:body/3"].label == "연구 기간(시작~끝)"  # same place, same detected label
    assert "m:과제명" in k and "m:연구제목" not in k  # a new marker starts as detected
    assert "a:body/4#u0" not in k and "a:body/6/r0/c1" not in k  # dropped before: stays dropped
    with pytest.raises(ConflictError, match="same file"):
        service.replace_template_file("admin-1", t.id, upload(form_bytes("과제명")))


def test_a_field_that_moved_does_not_inherit_another_field_s_label(env):
    service, *_ = env
    t = service.create_template("admin-1", "변경신청서", "", upload())
    service.save_template_fields("admin-1", t.id, [FieldEdit("a:body/3", "연구 기간(시작~끝)", "YYYY-MM-DD")])
    # A paragraph added at the top: every body blank moves one place down.
    d = Document(io.BytesIO(form_bytes()))
    d.paragraphs[0].insert_paragraph_before("머리말")
    out = io.BytesIO()
    d.save(out)
    new = service.replace_template_file("admin-1", t.id, upload(out.getvalue()))
    k = keys(new.fields)
    assert k["a:body/4"].label == "연구 기간"  # the moved blank: as detected, not carried
    assert "a:body/3" not in k or k["a:body/3"].label != "연구 기간(시작~끝)"
    assert all(f.hint != "YYYY-MM-DD" for f in new.fields)


def test_a_one_off_form_lives_and_dies_with_its_draft(env):
    service, store, writer, _, files = env
    name, fields = service.check_template_file("user-1", upload())
    assert name == "form.docx" and fields[0].key == "m:연구제목"
    assert files.files == {}  # checking stores nothing
    detail = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload())
    path = detail.draft.template.storage_path
    assert detail.draft.template.template_id is None and path.startswith("drafts/") and path in files.files
    service.delete_draft("user-1", detail.draft.id)
    assert path in files.deleted


def test_a_one_off_form_is_removed_when_the_draft_is_not_saved(env):
    service, store, writer, _, files = env
    writer.fail = RuntimeError("model down")
    with pytest.raises(UpstreamError):
        service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload())
    assert files.files == {} and len(files.deleted) == 1
    writer.fail = None
    store.fail_create_draft = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload())
    assert files.files == {}


def test_a_storage_failure_costs_nothing(env):
    service, _, writer, audit, files = env
    files.fail_put = OSError("down")
    with pytest.raises(UnavailableError, match="could not be stored"):
        service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload())
    assert writer.calls == [] and not any(e["event_type"] == "docgen.generate" for e in audit.entries)


def test_purge_removes_the_files_of_exactly_the_deleted_drafts(env):
    service, store, _, _, files = env
    gone = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload())
    kept = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload(form_bytes("다른")))
    library = service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_id=ready_template(service).id)
    store.drafts[kept.draft.id] = replace(store.drafts[kept.draft.id], updated_at="2099-01-01T00:00:00")
    assert service.purge_old(days=-1) == 2  # cutoff one day after NOW
    assert files.deleted == [gone.draft.template.storage_path]  # never the library file
    assert kept.draft.template.storage_path in files.files
    assert library.draft.template.storage_path in files.files


def test_both_a_list_template_and_a_file_is_refused(env):
    service, *_ = env
    t = ready_template(service)
    with pytest.raises(ValidationError, match="not both"):
        service.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_id=t.id, template_file=upload())


def test_without_storage_templates_are_switched_off(env):
    service, store, writer, audit, _ = env
    off = DocgenService(service._users, store, writer, audit, clock=lambda: NOW)
    assert off.templates_for("user-1", [A]) == []
    with pytest.raises(UnavailableError, match="not set up"):
        off.create_draft("user-1", [DocChoice(10)], None, [], "", [], template_file=upload())
    # Markdown drafts still work.
    assert off.create_draft("user-1", [DocChoice(10)], None, [], "", []).draft.template is None


def test_template_sources_are_logged_before_they_change(env):
    service, store, _, audit, _ = env
    t = ready_template(service, sources=())
    audit.fail_required = True
    with pytest.raises(AuditUnavailableError):
        service.set_template_hospitals("admin-1", t.id, [A])
    assert store.template_links == set()


# -- the API -------------------------------------------------------------------------------


@pytest.fixture
def client(env):
    service = env[0]
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: service._users
    app.dependency_overrides[get_docgen_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_the_template_api(client, env):
    h = auth("admin-1")
    file = {"filename": "form.docx", "content_base64": b64(form_bytes())}
    res = client.post("/admin/templates", headers=h, json={"title": "변경신청서", "doc_type": "amendment", "file": file})
    assert res.status_code == 201 and res.json()["status"] == "review"
    tid = res.json()["id"]
    fields = res.json()["fields"]
    assert len(client.get(f"/admin/templates/{tid}/detected", headers=h).json()) == len(fields)
    body = {"fields": [{"key": f["key"], "label": f["label"]} for f in fields]}
    assert client.put(f"/admin/templates/{tid}/fields", headers=h, json=body).json()["status"] == "ready"
    res = client.put(f"/admin/templates/{tid}/hospitals", headers=h, json={"hospital_ids": [A]})
    assert res.json()["hospital_ids"] == [A]

    u = auth("user-1")
    offered = client.get(f"/docgen/templates?source_id={A}", headers=u).json()
    assert [t["id"] for t in offered] == [tid]
    check = client.post("/docgen/templates/check", headers=u, json={"file": file})
    assert check.status_code == 200 and check.json()["fields"][0]["key"] == "m:연구제목"
    bad = client.post("/docgen/templates/check", headers=u, json={"file": {"filename": "x.hwp", "content_base64": b64(b"x")}})
    assert bad.status_code == 422 and "save it as .docx" in bad.json()["detail"]

    res = client.post("/docgen/drafts", headers=u, json={"documents": [{"document_id": 10}], "template_id": tid})
    assert res.status_code == 201
    draft = res.json()["draft"]
    assert draft["template"]["template_id"] == tid and "storage_path" not in draft["template"]
    did = draft["id"]
    res = client.put(f"/docgen/drafts/{did}/markdown", headers=u, json={"fields": {"m:연구제목": "X", "c:body/1": ["최초"]}})
    assert res.status_code == 200 and res.json()["fields"]["c:body/1"] == ["최초"]
    res = client.post(f"/docgen/drafts/{did}/export", headers=u)
    assert res.status_code == 200 and "연구 제목: X 끝" in read_docx(res.content)


def test_template_admin_routes_are_admin_only(client):
    h = auth("user-1")
    assert client.get("/admin/templates", headers=h).status_code == 403
    assert client.get("/admin/templates/1", headers=h).status_code == 403
    assert client.put("/admin/templates/1/fields", headers=h, json={"fields": []}).status_code == 403
    assert client.put("/admin/templates/1/hospitals", headers=h, json={"hospital_ids": []}).status_code == 403
    assert client.delete("/admin/templates/1", headers=h).status_code == 403
