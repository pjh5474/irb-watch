# IRB pipeline: target design (sites per PI, documents per step, one step per page)

Status: **design only, revision 3 of 2026-11-13. Nothing in this file is built.** No backend,
frontend or migration was changed. It replaces `~/.claude/plans/irb-pipeline.md` as the plan
for the next pipeline work. What is built today is in `decisions.md` ("IRB pipeline: admin
setup", "IRB pipeline: user pipelines", "Hospitals", "Draft jobs", "IRB pipeline: AI drafts
per step").

Revision 2 reviews revision 1 against **all 38 real project IRB folders** (section 1) and the
owner's second message. What changed is listed in section 12.

Owner decisions of 2026-11-13:

| # | Decision | Chosen |
|---|---|---|
| D1 | How a PI is modelled | **One pipeline per PI.** A project holds sites (hospital + PI + site number); each site has its own pipeline. Several sites may share one hospital |
| D2 | Where shared study files live | **A study library per project** (Protocol, CRF, master ICF, ...), versioned, uploaded once; a step attaches a chosen version by reference |
| D3 | Unit of work inside a step | **Document slots.** A step is a list of documents; each has its own kind, status, files and AI drafts. The admin defines the default list per step type |
| D4 | The same step at many sites at once | **Designed now, built later** ("study event"); tables are shaped for it from the start |
| D5 | Frontend | **One step per page.** The pipeline page becomes an overview. Functions may be added and spread over several pages so that users can organise their document work |
| D6 | PIs of one hospital | **One hospital has one IRB, so its PIs share the required-documents list and the forms.** The design therefore keeps lists, forms and reference files per hospital and lets PIs of one hospital reuse each other's work (second message) |

Decided in the third question round (2026-11-13):

| # | Topic | Chosen |
|---|---|---|
| D7 | Site fields | **No extra fixed fields.** A site has hospital, site number and PI; everything else (CRA, CRC, Sub-I, 연구비, ...) is a free fact **with dated history**, and a change can be tied to the step that reported it (4.1) |
| D8 | Site identity | Loose: PI and site number optional; a PI name once per hospital; a site number, when given, unique in the project |
| D9 | First steps | A new pipeline starts with **초기 심의 only**; later steps are added as they happen |
| D10 | Step types and follow-ups | The seed list of 4.2; a follow-up hangs under the original step only (one level), in numbered rounds |
| D11 | IRB result | **A separate result field** next to the status, from an admin-edited list (4.2) |
| D12 | Sharing | **Built with sites (phase B).** The owner adds colleagues of the same organization by pick. **An organization's admin reads and edits every pipeline project of that organization. Each project has one PM who reads and edits it. A CRA sees only the projects they own or were added to** (4.8) |
| D13 | Shared drafts | Drafts made in a pipeline document belong to the project: every project member sees and edits them. Standalone drafts stay private |
| D14 | Hospital document list | Replaces the global list for that step type; it starts as a copy and is then independent |
| D15 | Copy to sibling PIs | Slots, subject and library links only; no files, no drafts |
| D16 | Draft to file | **Both ways, always versioned:** a "Use as final file" button on a draft, and download, review in Word or Hangul, upload as the next version |
| D17 | Missing documents at "submitted" | Warn and allow |
| D18 | Site details to the AI | **Always sent** with a draft; the dialog says so; no switch |
| D19 | Study library | Flags sites still on an older version; offers "start amendment" at chosen sites after a new version; keeps its own visible change log. The site ICF is uploaded by hand (not chosen as an AI draft) |
| D20 | Work page | `/work` is a menu item, not the start page |
| D21 | MFDS | Not now; the design must not block it |

What is still open is in section 10.

## 1. What the real project folders show

Source: `Z:\MIRACULUS\1. Project`, every `01. MFDS & IRB` folder (38 found), read on
2026-11-13.

**What was read:** folder names, up to 5 levels below each `01. MFDS & IRB` (5,265 folders),
and the company's empty folder template (`99. 기타\00. Folder tree`). File names were read for
4 sites of one study only (셀트리온 암로젯정 PMS). **No file was opened.** Counts below come
from name matching and are approximate.

### 1.1 Three project shapes

| Shape | Projects | Layout under `02. IRB` |
|---|---|---|
| **Sites** | 13 of 38, holding 113 site folders (largest: 28, 24, 15 sites) | `<site no>. <hospital>(PI <name>)` / steps |
| **One IRB, no site folder** | about 21 of 38 | steps directly (`01. 초기심의`, `02. 1차 변경심의`, ...) |
| **Shared IRB** (`공용 IRB`) | 4 of 38; one of them also has `기관 IRB` with site folders under it | `01. 공용 IRB` / steps, and `02. 기관 IRB` / site / steps |

- **So a "site" is really one IRB track of a study.** Most projects have exactly one; the
  PI is often not in the folder name at all (it is in the project name, or nowhere).
- **Several PIs at one hospital** occur in 2 of the 13 site projects: 동아대학교병원 (3 PIs),
  강원대학교병원, 세브란스병원, 순천향대학교 서울병원, 삼육서울병원 (2 each), and
  고려대학교 안산병원 (4 site folders). Their step histories differ in number and dates.
- **A PI can change during the study:** `03. 전북대학교병원(PI 정영범)→(PI 신유섭)`, done
  through a follow-up step named `(PI 변경, ...)`. The site and its history stay.
- Site numbers are sometimes 3 digits (`010.`, `003.`) and have gaps.

### 1.2 Steps

About 1,300 step folders. Names, with the round number removed:

| Group | Names seen (count) |
|---|---|
| Initial | 초기심의 (about 124), 신규과제 (3) |
| Amendment | N차 변경심의 (about 400), **N차 변경보고 (109)**, 계획변경 (8), 연구비 심의 / 연구비 변경심의 (7) |
| Continuing | 중간보고 (44), 지속심의 (35), 연차지속심의 (7) |
| Deviation | 위반보고 (112), 미준수보고 (53) |
| Safety | 이상반응보고, 이상사례 보고, 중대한이상사례 보고, SUSAR 보고, 안전성보고, 타기관 SAE 보고 (about 25 together) |
| Problem / other reports | 문제보고, 예상하지 못한 문제 보고 (11), 연구자보고 (6), 기타보고 (3) |
| End | 종료보고 (about 13), 결과보고 (about 10) |
| **Follow-ups of another step** | N차 행정수정요청 / 행정수정 답변 / 행정의견 (about 120), 시정보고 / 시정심의 / 시정승인 / 시정답변 (about 25), 재심의, 보완, 보완심의, 수정요청, 심의의견 답변, 질의답변, 검토의견에 대한 답변서 |
| Not a step | `IRB membership list` (5), `old`, `문서이관`, `미정` |

- **The same kind of step has different names per hospital** (변경심의 / 변경보고 / 계획변경;
  위반 / 미준수; 중간보고 / 지속심의 / 연차지속심의).
- **Follow-ups are everywhere** and come in rounds (`01-1. 1차 행정수정요청`,
  `01-2. 2차 행정수정요청`). They are filed either next to the parent (`02-1`) or inside
  its folder. One step can have several.
- **The result is written into the folder name:** `(승인)`, `(수정 후 승인)`, `(시정승인)`,
  `(보완 후 재심의)`.
- **The reason is written into the name too:** `(연구비 변경)`, `(PT 2.0)`,
  `(CRA 1차 변경)`, `(PT2.0, CRF2.0, ICF2.0_2.0, PICF 2.0_2.0)`: which study document
  versions the step is about.
- Deviation and safety reports recur many times and are not tied to a document version.

### 1.3 Inside a step

Almost every step has the same two folders (this is also the company template):

| Folder | Count | Holds |
|---|---|---|
| `신청서 및 통지서` | about 945 | the application (신청서), sometimes a receipt (접수증), the IRB's answer (통지서 / 통보서) |
| `제출서류` / `제출파일` / `제출자료` | about 940 | the submitted documents; sometimes marked `제출 서류 없음` |
| `draft` (inside the submission folder) | everywhere in the template | working versions before the final file |
| `old`, `참고자료`, `양식`, `IRB 사전 검토`, `최종 제출`, `심의 서류 바인더` | few | superseded files, references, the hospital's blank form, paper binders |

From the file names of 4 sites of one study:

- **study-wide files, identical at every site:** `Protocol_V2.0`, `CRF_V1.1`,
  `Protocol 변경대비표_V1.2 to V2.0`, `제품설명서`, `허가증`, the master ICF;
- **per-site files** with site number and PI in the name: site ICF
  (`ICF_master V1.0_..._Site 01_V1.0`), `연구진 Delegation Log`, `취약한 대상자 보호대책`,
  `연구대상자 모집문건 미제출사유서`, `연구비 변경대비표`, `연구담당자(CRA) 변경대비표`,
  신청서, 통지서;
- **two PIs of 동아대학교병원 submitted the same list of documents** at 초기심의 (D6 holds
  in this one case; not checked elsewhere);
- one study change (Protocol V2.0) is "4차 변경심의" at one site and "2차" at two others;
- file name pattern:
  `<study code>_<site no> <hospital>(PI <name>)_<document>_<date>[_final]`;
- formats: mostly PDF; HWP and HWPX for forms and comparison tables.

### 1.4 The company's folder template (outside the IRB folder)

- `03. Study Document`: `01. Protocol`, `02. CRF`, `03. ICF`, `04. Others` (참고문헌). **This
  is the study library of D2.**
- `04. Investigator & Site / <기관번호> <병원> (PI명)`: `01. CV & GCP` (PI, CRC, Sub-I),
  `02. PDCF`, `03. Training Log`. **Per-site files that IRB submissions reuse** (CVs, GCP
  certificates).
- `01. MFDS & IRB / 01. MFDS` (`01. IND`): the regulator's track, next to the IRB track.
  Out of scope here (D21).

## 2. Terms

| Term | Meaning |
|---|---|
| Project | A user's study (table `projects`). Unchanged |
| Hospital | Platform-wide catalog row (`hospitals`). **It stands for the IRB a site submits to.** A shared IRB (`공용 IRB`) is one more catalog row, added by the platform admin |
| **Site** | New. One IRB track of a project: a hospital, usually a PI, a site number. PI and number may be empty (single-IRB projects) |
| **Sibling sites** | New. Sites of one project at the same hospital (same IRB, other PIs) |
| Pipeline | The IRB lifecycle of one **site** (today: of one project + hospital) |
| Step | One submission round in a pipeline (초기심의, 2차 변경심의, 위반보고, ...) |
| **Follow-up step** | New. A step that answers another step's review (행정수정요청, 보완, 시정, 재심의) |
| **Document slot** | New. One document a step needs or received: a kind, a status, uploaded versions and AI drafts. Extends today's `step_documents` |
| **Document kind** | New. Admin-defined entry in a step type's default document list (form, AI instructions, guides, origin), global or for one hospital |
| **Study library** | New. Versioned files kept outside steps, at three levels: the project, one hospital of the project, one site |
| **Study event** | New, later. One study change applied to many sites: adds the same step to each and queues the drafts |

## 3. What exists today and what is missing

Built (offline tests only; migrations applied; see `decisions.md`):

- Tables: `step_statuses`, `step_types` (AI instructions, guides, form, input rules,
  `required_documents text[]`), `pipeline_templates` (global + per hospital), `pipelines`
  (`project_id`, `hospital_id`; unique live pair), `pipeline_steps`, `step_documents` (a named
  document), `step_document_versions`, `pipeline_events`, `draft_jobs`,
  `drafts.pipeline_step_id`, `drafts.step_files`.
- Code: `services/pipelines.py` (`PipelineService`, `PipelineStore`), `db/pipelines.py`,
  `api/pipelines.py`, `services/pipeline_admin.py`, `api/admin_pipeline.py`,
  `services/draft_jobs.py`, step drafts in `api/docgen.py`
  (`POST /docgen/steps/{step_id}/drafts`).
- Frontend: `/pipelines/[id]` renders `components/pipelines/pipeline-view.tsx`: **every step
  as a full card on one page** (status, dates, note, checklist, documents, upload, drafts),
  plus add step, history and remove. `step-drafts.tsx` holds the draft dialog.

Gaps against the real work:

| Gap | Today | Needed |
|---|---|---|
| PI | One live pipeline per project + hospital | Several per hospital, one per PI (D1); PI optional; PI may change |
| Same IRB, several PIs | Nothing shared | Per-hospital document lists and forms; reuse a sibling site's step and files (D6) |
| Shared files | Every file is uploaded into one step of one pipeline | Upload once, attach by reference (D2); also hospital-level and site-level files |
| Documents per step | A checklist of names; one AI setup per step type; one open draft job per step | Per-document form, instructions, status, drafts and final file (D3) |
| Step types | 6 seeded | The groups of 1.2; reports that recur; names that differ per hospital |
| Follow-up steps | Flat list, free names | A step may answer another, in numbered rounds |
| Result | Statuses 승인 / 반려 / 보완 요청 | Also 수정 후 승인, 시정승인, 보완 후 재심의 |
| Received documents | Not distinguished | 접수증, 통지서 are received, never drafted |
| Page size | All steps on one page | One step per page; pages per project, hospital, site, step (D5) |
| Across studies | Nothing | One list of what is due and what is missing over all projects |
| Many sites | Nothing | Copy a step to sibling sites (early); study event (D4, later) |

## 4. Target data model

All new tables: RLS on, `revoke all` first, then exact `service_role` grants, no browser
access (the server serves owners, as for every pipeline table today). Each needs a new
migration; applied migrations are never edited. Column lists are proposals to be fixed when
the phase is built.

### 4.1 Sites (phase B)

```
project_sites
  id               bigint identity pk
  project_id       bigint not null -> projects (id)      on delete no action
  hospital_id      bigint not null -> hospitals (id)     on delete no action
  user_id          uuid   not null -> profiles (id)      on delete cascade
  organization_id  uuid   not null default <Default org> -> organizations (id)
  site_no          text   not null default ''   check length <= 20    -- "01", "010"; text, keeps zeros
  pi_name          text   not null default ''   check length <= 100
  note             text   not null default ''   check length <= 4000
  created_at, updated_at, deleted_at
  unique (id, project_id, hospital_id)                     -- target of the pipelines foreign key
  unique live (project_id, hospital_id, lower(btrim(pi_name)))   where deleted_at is null
  unique live (project_id, lower(btrim(site_no)))                where deleted_at is null and site_no <> ''

site_facts                                       -- append-only: one row per change (D7)
  id          bigint identity pk
  site_id     bigint not null -> project_sites (id) on delete cascade
  name        text   not null  check length 1..100      -- "CRA", "CRC", "Sub-I", "증례당 연구비", "PI"
  value       text   not null  check length <= 2000     -- '' = the fact was removed
  valid_from  date   not null                            -- the day the new value applies
  step_id     bigint -> pipeline_steps (id) on delete set null   -- the step that reported the change
  changed_by  uuid -> profiles (id) on delete set null
  created_at  timestamptz not null default now()
  index (site_id, lower(btrim(name)), valid_from desc, id desc)
```

- **Site facts (D7).** The current value of a fact is its newest row (by `valid_from`, then
  `id`). Rows are never updated or deleted (`service_role`: select, insert), so the site
  page shows each fact's history: value, from which day, who changed it, and the step it
  was reported in ("3차 변경심의: CRA 변경"). At most 40 live fact names per site. The page
  suggests the usual names (PI 직위, 진료과, Sub-I, CRC, CRA, IRB 과제번호, 증례당
  연구비); none is required.
- **A change made on a step page** ("Record a change of site details" on an amendment
  step) writes the rows with that `step_id`. A draft of a comparison table (변경대비표) in
  that step gets the old and the new values (6.1).
- **The PI name is both** the column `pi_name` (identity, uniqueness, page titles) and a
  fact named `PI` (history): the service writes both in one database function.

- **PI and site number are optional.** About 21 of the 38 real projects have one IRB and no
  site folder: there the project gets one site with an empty PI, made together with its
  pipeline in one click ("Start IRB pipeline" on a hospital, as today). The empty PI name
  also keeps today's rule (one unnamed site per hospital) until the user names it.
- **A shared IRB (`공용 IRB`)** is a row in the `hospitals` catalog (the platform admin adds
  it), so a project can hold one site for the shared IRB and others for hospital IRBs. No
  extra column. The catalog name "hospital" stays in the database; the pages may say
  "IRB / hospital".
- **PI change:** editing `pi_name` keeps the site, its pipeline and its files. The change is
  a `site_facts` row (name `PI`, the date, the step), and the site page shows "PI 정영범
  until 2026-03-01". Drafts use the current PI.
- `pipelines` gets `site_id bigint not null` with a **composite foreign key**
  `(site_id, project_id, hospital_id) -> project_sites (id, project_id, hospital_id)`, so a
  pipeline can never disagree with its site. `project_id` and `hospital_id` stay on
  `pipelines` (guides, forms, boards and statistics already key on them).
- `pipelines_project_hospital_key` is dropped; new unique index: one **live pipeline per
  site** (`site_id` where `deleted_at is null`).
- **Why a separate table and not PI columns on `pipelines`:** a site exists before its
  pipeline is started (the project page lists sites like the folder list), a removed pipeline
  can be started again for the same PI, and study events, site files and standalone drafts
  refer to a site, not to a lifecycle.
- **Backfill:** one site per existing pipeline (live or removed, not yet purged) with empty
  PI and number. On 2026-11-12 the project held 1 live pipeline; **count again before
  writing the migration.**
- `project_hospitals` stays as it is (the browser writes it; it drives the project's
  document list and feed filter). A site needs its hospital in the project when it is made.
  Removing the hospital from the project keeps its sites and pipelines, shown under
  "Hospitals no longer in this project" as today.
- Limits: at most 100 live sites per project (proposal; the largest real project has 28).
- Site delete is soft; refused while it has a live pipeline; purged after 30 days with the
  same 03:30 job.

### 4.2 Steps (phase B)

`pipeline_steps` gets:

```
  parent_step_id  bigint -> pipeline_steps (id) on delete set null   -- follow-up of that step
  sequence_no     integer                                            -- "2" in 2차 변경심의
  subject         text not null default '' check length <= 200       -- "PT 2.0, CRF 1.1", "연구비 변경"
  result_key      text -> step_results (key)                         -- the IRB's answer; null: none yet
  study_event_id  bigint -> study_events (id) on delete set null     -- phase D; null otherwise
```

New admin list (D11), shaped like `step_statuses`:

```
step_results
  key           text primary key  check (key ~ '^[a-z][a-z0-9_]{0,39}$')
  label         text not null     check length 1..60
  approved      boolean not null default false   -- counts as approved (status table, 4.6)
  needs_answer  boolean not null default false   -- the page offers "Add follow-up step"
  position      integer not null
  archived_at, created_at
seed: approved 승인 (approved) · approved_after_revision 수정 후 승인 (approved)
      · corrective_approval 시정승인 (approved, needs answer)
      · revise_and_resubmit 보완 후 재심의 (needs answer) · rejected 반려
```

`step_types` gets:

```
  category   text not null default 'review'
               check in ('initial', 'amendment', 'continuing', 'deviation', 'safety',
                         'report', 'closing', 'follow_up')
  numbered   boolean not null default false      -- shows "{n}차"
```

- **Follow-up step:** a step with `parent_step_id` set, of a step type in category
  `follow_up`. It keeps its own status, dates and documents, and must be in the same
  pipeline as its parent (checked in the service). One level only: an answer to a follow-up
  points at the same parent (D10). It is shown indented under the parent (`02-1`, `02-2`).
- **Status and result are two things (D11).** The status stays the work state (시작 전, 준비
  중, 제출됨, and closed ones). The result is the IRB's answer, set when the 통지서 arrives;
  setting it fills the decision date when empty. **A result with `needs_answer` offers "Add
  follow-up step".** Backfill: steps with status `approved` / `rejected` /
  `revisions_requested` get the matching result and keep their status; the admin may then
  rename or archive those statuses (for example one closed status 완료). Whether the seeded
  statuses are changed by the migration is open (O7).
- **Numbering:** `sequence_no` is filled by the server when a step of a numbered type is
  added: the count of live steps of that type in the pipeline (for a follow-up: under the
  same parent), plus one. Shown as `{n}차 {name}`; the user can edit it. Removing a step
  does not renumber the others.
- **`subject`** takes the reason that real folders put in brackets. The existing `name`
  stays the step's label and is still renameable, which covers hospitals that say 변경보고
  or 미준수보고 for the same type.
- **Seeded step types** (D10; the admin can add and rename them): 초기 심의 (initial), 변경 심의 (amendment, numbered), 지속 심의 / 중간보고
  (continuing, numbered), 위반/이탈 보고 (deviation, numbered), 중대한 이상사례 보고
  (safety, numbered), 문제 보고, 연구자 보고 (report), 종료 보고, 결과 보고 (closing),
  행정수정 요청 답변, 보완 / 시정 답변, 재심의 (follow_up, numbered).
- **A new pipeline starts with 초기 심의 only (D9):** the migration sets the global step
  order (`pipeline_templates`, `hospital_id` null) to that one type. A hospital's own order,
  if the admin made one, is left as it is. Existing pipelines are not changed.

### 4.3 Document kinds per step type, global or per hospital (phase C, admin)

```
step_type_documents
  id                bigint identity pk
  step_type_id      bigint not null -> step_types (id) on delete cascade
  hospital_id       bigint -> hospitals (id) on delete cascade     -- null: the global list
  name              text   not null   check length 1..200       -- "신청서", "연구비 변경대비표"
  section           text   not null   check in ('application', 'submission', 'received', 'reference')
  origin            text   not null   check in ('draft', 'upload', 'library', 'received')
  required          boolean not null default true
  form_template_id  bigint -> form_templates (id) on delete set null
  instructions      text   not null default ''  check length <= 4000
  guide_ids         bigint[] not null default '{}'
  library_name      text   not null default ''   -- origin 'library': the library document's name to offer
  position          integer not null
  archived_at       timestamptz
  created_at, updated_at, updated_by
  unique live (step_type_id, coalesce(hospital_id, 0), lower(btrim(name))) where archived_at is null
```

- **One hospital, one IRB, one list (D6, D14).** For a step type, a hospital either has its
  own list or uses the global one, exactly like the step order (`pipeline_templates`): a
  new step copies **the hospital's own list if it has one, else the global list**. Making a
  hospital's list copies the global one; from then on the two are independent (a later
  change of the global list does not reach it). "Remove this hospital's list" goes back to
  global.
- `section` matches the two real folders: `application` and `received` together are
  `신청서 및 통지서`; `submission` is `제출서류`; `reference` is 참고자료 and the like.
- `origin`:
  - `draft`: the AI can draft it (신청서, 사유서, 보호대책, 변경대비표);
  - `upload`: the user uploads it (site ICF, Delegation Log, signed forms; D19);
  - `library`: taken from the study library (Protocol, CRF, 제품설명서, 허가증, CVs);
  - `received`: comes back from the IRB (접수증, 통지서); never sent to a draft by default.
  The origin only sets what the slot offers first. Every slot can always take an uploaded
  file.
- **A kind's own instructions, guides and form win over the step type's.** The step type's
  values stay the default for free slots and for kinds that leave theirs empty. In the
  prompt both go into the same untrusted-admin block as today (`<admin_instructions>`,
  closing tag defused), the step type's first.
- `step_types.required_documents` is **backfilled** into the global list (one row per name:
  section `submission`, origin `upload`, required) and no longer read. The column is dropped
  in a later migration, after both repos run on the new table.
- Server-wide settings (platform admin), like step types. A hospital's list is the same for
  every customer of that hospital, which is what D6 says.
- At most 50 live kinds per step type and hospital (the current checklist limit).

### 4.4 Document slots (phase C)

`step_documents` becomes the slot. New columns:

```
  kind_id                    bigint -> step_type_documents (id) on delete set null
  section                    text not null default 'submission'  (same check as 4.3)
  origin                     text not null default 'upload'      (same check as 4.3)
  required                   boolean not null default false
  status                     text not null default 'needed'
                               check in ('needed', 'in_progress', 'final', 'not_needed')
  position                   integer not null default 0
  study_document_version_id  bigint -> study_document_versions (id) on delete set null   -- phase D
  copied_from_document_id    bigint -> step_documents (id) on delete set null            -- 4.5
  note                       text not null default '' check length <= 2000
  updated_at                 timestamptz not null default now()
```

`step_document_versions` gets `from_draft_id uuid` and `from_draft_version integer` (both
null for a plain upload): which draft version an uploaded final file came from.

`drafts` gets `step_document_id bigint -> step_documents (id) on delete cascade`.

- **Slots are copied when the step is made**: one slot per live kind of the step's type
  (the hospital's list or the global one), with the kind's name, section, origin and
  required flag. Later edits of the kinds do not change existing steps. `kind_id` stays as
  a pointer so the kind's form, guides and instructions are **read when a draft is made**
  (today's rule for step types).
- The user may add a free slot (name, section; origin `upload`), rename a free slot, set any
  slot to `not_needed`, reorder slots, and soft-remove a slot (existing remove/restore).
- **Status** (fixed in code, proposal):
  - `needed`: nothing yet;
  - `in_progress`: set by the server when the first draft or version arrives;
  - `final`: set by the user ("Mark final") on a slot that has a version or an attached
    library version; a newer upload sets it back to `in_progress`;
  - `not_needed`: set by the user; counts as done (the real `제출 서류 없음`).
  **The working versions and the final file are one slot:** every upload is a version, and
  "final" marks the newest. This replaces the `draft` and `old` folders.
- **Checklist rule (replaces "a live document with that name has a version"):** a required
  slot is done when its status is `final` or `not_needed`. Setting a step to `submitted`
  with required slots not done is allowed; the page lists what is missing and asks to
  confirm first (D17).
- **A draft belongs to a slot** (`drafts.step_document_id`). `drafts.pipeline_step_id` stays
  and is always the slot's step. Existing step drafts (no slot) are shown on the step page
  under "Earlier drafts of this step".
- **From draft to file, two ways, both versioned (D16):**
  - **"Use as final file"** on a draft version: the server builds the export (the filled
    form or the plain DOCX, as `POST /docgen/drafts/{id}/export` does today) and stores it
    as the slot's next version with `from_draft_id` / `from_draft_version`;
  - **download, review, upload:** the user downloads the draft's file, opens and corrects
    it in Word or Hangul, and uploads the result into the slot as the next version. The
    upload form offers "made from draft <title>, version N" (pre-chosen when the slot has
    one draft) so the link to the draft is kept; it may be left empty.

  Either way nothing is overwritten: every file is a version (number, file name, who,
  when, from which draft), any version can be downloaded, and a later change (a signed
  PDF, a corrected file) is the next version. A new version sets a `final` slot back to
  `in_progress` until the user marks it final again.
- **Job limit:** "one open job per step" becomes **one open draft job per slot**: the unique
  index `draft_jobs_open_step_key` is replaced by one on a new
  `draft_jobs.step_document_id`. The per-user limit (2 open) and the daily limit are
  unchanged in phase C.
- Existing `step_documents` rows are backfilled: section `submission`, origin `upload`,
  status `in_progress` when they have a version, `required` true when the name is on the
  step type's old checklist.

### 4.5 Reuse between PIs of one hospital (phase C; D6)

Same IRB, so the next PI's submission is mostly the first PI's with other names.

- **"Copy step to sibling sites"** (D15) on a step page: the user ticks sibling sites (same
  project, same hospital, live pipeline). For each, the server adds a step of the same type
  with the same `subject`, the same slots (kinds and the user's free slots, in the same
  order) and the same attached library versions. **Files and drafts are not copied.** Each
  new slot points at its origin (`copied_from_document_id`). A sibling that already has a
  step copied from this one is skipped and named in the answer. It runs in the request (a
  few rows per site), all sites in one database function, all or nothing.
- **"Start from the sibling's file"** in a slot's draft dialog: when the slot was copied, or
  a sibling site has a final file in a slot of the same kind, that file is offered as an
  input, ticked, with the line "written for PI <name>; rewrite for this site". The AI then
  changes the site facts and keeps the rest. It is the user's own file; the consent list
  names it like any other.
- **Hospital-level files** (the hospital's blank forms, its IRB membership list, its ICF
  wording) live in the study library at hospital level (4.6), so all its PIs attach the
  same file.
- The project page groups sites by hospital, and the hospital page (7.1) shows its PIs side
  by side per step type, so the user sees which PI is behind.
- This is the small, early form of the study event (4.7): same hospital only, no batch
  drafts. It needs no new limits.

### 4.6 Study library, three levels (phase D)

```
study_documents
  id            bigint identity pk
  project_id    bigint not null -> projects (id) on delete no action
  hospital_id   bigint -> hospitals (id) on delete no action        -- set: for this hospital's sites only
  site_id       bigint -> project_sites (id) on delete no action    -- set: for this site only
  folder        text   not null default 'others'
                  check in ('protocol', 'crf', 'icf', 'product', 'people', 'forms', 'others')
  name          text   not null  check length 1..200        -- "Protocol", "ICF master", "CV PI"
  created_at, deleted_at
  check (site_id is null or hospital_id is not null)
  unique live (project_id, coalesce(hospital_id, 0), coalesce(site_id, 0), lower(btrim(name)))
              where deleted_at is null

study_document_versions
  id            bigint identity pk
  document_id   bigint not null -> study_documents (id) on delete cascade
  version_no    integer not null check >= 1                  -- upload order
  label         text   not null default '' check length <= 50    -- "V2.0", as printed on the file
  dated_on      date                                          -- the document's own date
  filename, storage_path (unique), sha256, size_bytes, body_text, has_text (generated), text_note
  uploaded_by   uuid -> profiles (id) on delete set null
  created_at
  unique (document_id, version_no)
```

| Level | Columns set | Real counterpart | Examples |
|---|---|---|---|
| Project | `project_id` | `03. Study Document` | Protocol, CRF, master ICF, 제품설명서, 허가증 |
| Hospital | + `hospital_id` | the `양식` folders, `IRB membership list` | the IRB's blank forms, membership list, hospital ICF wording |
| Site | + `site_id` | `04. Investigator & Site / <site>` | CV and GCP certificate of PI, Sub-I, CRC; site ICF; Delegation Log |

- `folder` mirrors the company template (Protocol, CRF, ICF, Others) plus product, people
  and forms; it only groups the list. Fixed in code.
- Same upload rules as step documents (one file per upload, `MAX_FILE_BYTES`, bytes must
  match the name, text read for PDF/HWP/HWPX/DOCX, identical file refused with 409). The file
  step of `PipelineService.upload` is shared, not copied.
- Storage: the existing private bucket `pipeline-files`, path
  `proj<project>/lib/<random>/<safe name>`.
- For whoever may open the project (4.8), through the server. Soft delete of a whole document,
  30-day purge; a single version is never deleted. **A library document that a live slot
  points at cannot be purged** (the purge skips it and logs why).
- **Attach:** any slot may point at one library version
  (`step_documents.study_document_version_id`) that its site may use: project level, its
  hospital's, or its own. It is a reference, not a copy: the slot shows the library file
  name, label and download link. Each site's step records which version it submitted.
- **Status table** (project page, D19): rows are sites, columns are the project-level
  documents; each cell is the newest version that site has in a step whose result is
  `approved`, **flagged when the library holds a newer version**. This answers "which sites
  are still on Protocol V1.2", which today needs opening 24 folders.
- **"Start amendment" (D19):** after a new version is uploaded, the page offers to add a
  step at chosen sites (default: every flagged site): the user picks the step type
  (default 변경 심의) and a subject; the server makes one `study_events` row (4.7) and, per
  site, a step with that hospital's slots and the new version attached to the matching
  `library` slot. No AI drafts are started. A site that already has a step of this event is
  skipped and named. All sites in one database function, all or nothing.
- **Change log (D19):** table `study_document_events` (project, document, version, actor,
  event type, payload, time; append-only for the server), shown on the library tab newest
  first: uploaded, removed, restored, attached to a step, amendment started.
- **Comparison tables** (`Protocol 변경대비표_V1.2 to V2.0`): the draft dialog of a slot lets
  the user pick two library versions of one document (from, to). The server sends both
  texts and the list of changes between them, built with the existing `core/diff.py`
  (as the docs generator does for board documents with "compare with").
- **A blank form kept at hospital level is a reference file, not a form template.** Filling
  a form still needs the admin's `.docx` template library; HWP forms cannot be filled
  (section 11).

### 4.7 Study events (table and "apply" in phase D; batch drafts in phase E)

```
study_events
  id              bigint identity pk
  project_id      bigint not null -> projects (id) on delete no action
  user_id, organization_id
  name            text not null             -- "Protocol V2.0"
  step_type_id    bigint -> step_types (id) on delete set null
  library_version_ids bigint[] not null default '{}'   -- the versions this change is about
  created_at, deleted_at
```

- **Phase D, "apply to sites"** (the "start amendment" of 4.6): the user ticks sites of any
  hospital; for each, the server adds one step of the event's step type
  (`pipeline_steps.study_event_id`), with slots from **that site's hospital's** list and
  the event's library versions attached to the matching `library` slots. A site that
  already has a step of this event is skipped and named. The project page lists events
  with each site's step and its status.
- **Phase E, "draft for all":** one draft job per site and per chosen `draft` slot, each
  written with that site's facts. Needs `draft_jobs.batch_id` and a batch status view.
- **The job limits must change for this** (2 open jobs per user, a 2-thread pool in the API
  process, a per-user daily limit of 10): 28 sites x 2 documents is 56 jobs. Proposal: batch
  jobs wait in `queued` without counting against the 2-open limit, run one at a time per
  user, and count against a separate, admin-set batch limit. This is also where the worker
  should leave the API process (SaaS rule: the table is already the queue). **Not decided.**
- `pipeline_steps.study_event_id` is added in phase D with the table.

### 4.8 Sharing a project (phase B; D12, D13)

Today projects are private per user and read **directly by the browser under RLS**
(`projects`, `project_hospitals`); pipelines are served by the server to their owner. Both
paths change.

**Who may open a project** (one rule, used by RLS and by the server):

| Who | How they get it | May |
|---|---|---|
| Owner | made the project | everything, incl. members, PM, hand-over, delete |
| **PM** (one per project) | set by an organization admin or by the owner | read and edit everything in the project; not delete it |
| Member (a CRA) | added by the owner or the PM, picked from **the owner's organization** | read and edit everything in the project; not members, not delete |
| **Organization admin** | `organization_members.role` in (`owner`, `admin`) of the project's organization | read and edit every project of that organization; set the PM; hand a project over |
| **Platform admin** (support; changed 2026-11-13, O25) | `profiles.role = 'admin'` | read everything of every project, change nothing (403 with the reason); no extra audit |
| Anyone else | - | nothing (404) |

```
project_members
  project_id  bigint not null -> projects (id) on delete cascade
  user_id     uuid   not null -> profiles (id) on delete cascade
  role        text   not null check in ('pm', 'member')
  added_by    uuid -> profiles (id) on delete set null
  added_at    timestamptz not null default now()
  primary key (project_id, user_id)
  unique (project_id) where role = 'pm'          -- one PM per project
```

- **This changes a standing rule.** Until now "organizations only group users and give
  statistics; they never grant access". From phase B an organization role grants access
  **to pipeline projects of that organization only**. Private board documents stay per
  person (`source_members`); nothing else changes. CLAUDE.md and decisions.md
  "Organizations (SaaS step 1)" must be updated when phase B is built.
- **Organization roles become real:** the platform admin sets who is an organization's
  admin (admin Organizations tab; `PUT /admin/users/{id}/organization-role`). Today every
  platform admin is `owner` of the Default organization from the backfill: **check that
  list before phase B**, or every platform admin sees every project of Default.
- **One function decides:** `public.can_open_project(project_id)` (security definer, stable,
  `search_path = ''`, reads `auth.uid()`): owner, or a `project_members` row, or an
  organization admin of `projects.organization_id`. The RLS policies of `projects` and
  `project_hospitals` use it for select; update and delete policies use a second function
  `can_edit_project` (the same today, kept separate so a read-only role can be added
  later). The server uses the same rule through one helper in `services/access.py` for
  sites, pipelines, steps, documents, library and events.
- **Same organization only:** a member must be in the project's organization when added;
  the check is repeated on every read (a user moved to another organization loses access
  without any clean-up; the row stays and is shown as "no longer in this organization").
- **Hand-over** (CRA leaves a study): the owner or an organization admin sets a new owner
  from the organization; `projects.user_id` changes; the old owner becomes a member or is
  removed (asked in the dialog). Audit entry `project.handed_over` written first (fail
  closed). Sites, pipelines, files and drafts stay where they are.
- **Who did what stays visible:** `pipeline_events.actor_id`, `uploaded_by`, `changed_by`
  already exist; the history shows the person's name to project members instead of "by
  someone else". `pipelines.user_id` and `project_sites.user_id` mean "created by".
- **Limits and usage stay per person:** an AI draft counts against the daily limit and the
  2-open-jobs limit of the user who starts it; the audit entry names that user.
- **Shared drafts (D13):** a draft made in a pipeline (`pipeline_step_id` set; from phase C
  also `step_document_id`) belongs to the project. **This starts in phase B1**, because
  step drafts already exist. Today the browser reads `drafts` under RLS (own rows only), so
  pipeline drafts are **served by the server** to project members
  (`GET /docgen/drafts/{id}` and the list of a pipeline's drafts check `can_open_project`),
  and editing or revising is open to members. Their jobs (`draft_jobs` with a step) are
  shown to members too, so two people do not start the same draft. Two people saving at
  once is already handled (409 "reload"). Standalone drafts stay private, and the Drafts
  page lists a pipeline draft only for the user who started it.
- **Consent in a shared project:** the member who starts a draft consents to sending the
  ticked files, including files a colleague uploaded. The dialog and the audit entry
  already name every file.
- **Board documents stay per person, with one leak to know:** a member who may read a
  members-only board can put its text into a shared draft; other members, who may not read
  that board, then read the draft. The draft dialog must say "this draft is shared with the
  project's members" when such a board is ticked. Whether to refuse instead is open (O22).
- Project-level limits: at most 30 members per project (proposal).

## 5. Target API

Through the server, as today. Phase A: owner only. From phase B1: whoever may open the
project (4.8). A stranger's or unknown id answers 404. Every failure carries a reason.

### 5.1 Changed

| Route | Change | Phase |
|---|---|---|
| `GET /pipelines/{id}` | Becomes the **overview**: pipeline, site, steps with status, dates, `parent_step_id`, `sequence_no`, `subject` and counts (`slots`, `slots_done`, `required_missing`, `drafts`, `open_jobs`), statuses, step types. **No documents or versions** | A |
| `GET /projects/{project_id}/pipelines` | Each row also names its site | B |
| `POST /projects/{project_id}/pipelines` | Body `site_id`, or `hospital_id` alone (makes the unnamed site and its pipeline in one call) | B |
| `POST /pipelines/{id}/steps` | Also takes `parent_step_id`, `subject`; fills `sequence_no`; copies slots | B, C |
| `POST /pipelines/{id}/steps/{step_id}/documents` | Uploads into a slot (`document_id`) or makes a free slot (`name`, `section`) | C |
| `GET /pipelines/{id}/steps/{step_id}/draft-inputs` | Takes `document_id` (the slot); returns the kind's form, guides, whether it has instructions, own files, a sibling's file, library versions | C, D |
| `POST /docgen/steps/{step_id}/drafts` | Body gets `step_document_id`, later `library_versions` and `compare` | C, D |
| `GET /pipelines/{id}/history` | Optional `step_id` filter | A |

### 5.2 New

| Route | Purpose | Phase |
|---|---|---|
| `GET /pipelines/{id}/steps/{step_id}` | **The step page in one call:** step, its slots with versions, checklist, drafts of the step, open and recently failed jobs, follow-up steps, previous and next step ids, statuses | A |
| `GET /work` | The user's open work over all projects: steps due or overdue, steps waiting for the IRB, required documents missing, drafts being written or failed | A2 |
| `GET/POST /projects/{project_id}/sites`, `PATCH/DELETE /sites/{id}`, `POST /sites/{id}/restore` | Sites | B |
| `GET /sites/{id}/facts`, `POST /sites/{id}/facts` (`name`, `value`, `valid_from`, `step_id`) | Site facts: current values with history; a change is a new row | B |
| `GET/PUT/DELETE /projects/{project_id}/members...`, `PUT /projects/{project_id}/pm`, `POST /projects/{project_id}/hand-over`, `GET /organization/users` (people the caller may pick) | Sharing (4.8) | B |
| `GET /organization/projects` | Organization admin: every pipeline project of the organization | B |
| `GET/POST/PATCH /admin/pipeline/results...`, `PUT /admin/users/{id}/organization-role` | Admin: IRB results list; organization admins | B |
| `GET /projects/{project_id}/hospitals/{hospital_id}` | The hospital page: its sites with each one's steps (type, number, status, dates), the hospital's document lists in use | B |
| `PATCH /pipelines/{id}/steps/{step_id}/documents/{document_id}` | Slot: `status`, `name` (free slots), `note`, `section`, `study_document_version_id` | C, D |
| `PUT /pipelines/{id}/steps/{step_id}/documents/order` | Reorder slots (one database function, all or nothing, like steps) | C |
| `POST /pipelines/{id}/steps/{step_id}/copy` | Copy the step to sibling sites (`site_ids`) | C |
| `POST /docgen/drafts/{id}/use-as-final` | Store a draft version's export as the slot's next version | C |
| `GET/POST/PATCH /admin/pipeline/step-types/{id}/documents`, `.../hospitals/{hospital_id}/documents` | Admin: document kinds, global and per hospital | C |
| `GET/POST /projects/{project_id}/library` (`hospital_id`, `site_id` filters), `POST /library/{document_id}/versions`, `.../download-url`, `.../remove`, `.../restore` | Study library | D |
| `GET /projects/{project_id}/library/status`, `GET /projects/{project_id}/library/history` | The status table and the change log (4.6) | D |
| `POST /projects/{project_id}/events` (makes the event and applies it to `site_ids`), `GET /projects/{project_id}/events`, `GET /events/{id}` | Study events: "start amendment" | D |
| `POST /events/{id}/drafts` | Batch drafts | E |

Uploads stay base64 in JSON with the declared-size check (known limit, see CLAUDE.md).

### 5.3 Service structure

`services/pipelines.py` is 821 lines and would roughly double. Split when phase B starts, by
table group, each with its own store protocol in `db/` (one place per table for a tenant
filter later):

| Module | Holds | Store |
|---|---|---|
| `services/sites.py` | sites, site facts, the hospital page, PI change | `SiteStore` |
| `services/project_access.py` | who may open or edit a project, members, PM, hand-over | `ProjectAccessStore` |
| `services/pipelines.py` | pipelines, steps, follow-ups, history, purge | `PipelineStore` (smaller) |
| `services/step_documents.py` | slots, versions, upload, download, checklist, copy to siblings | `StepDocumentStore` |
| `services/study_library.py` | library documents and versions, status table | `StudyLibraryStore` |
| `services/pipeline_work.py` | the cross-project work list (read only) | reads the stores above |
| `services/pipeline_admin.py` | statuses, step types, step order, **document kinds** | existing |
| `services/study_events.py` | events and batches | phase E |

Shared by slots and the library: the file step (size, type check, text reading, store file
then row, remove the file if the row fails) as one function. `PipelineService.context` (the
docs generator's `StepReader`) moves to `step_documents` and learns slots, sibling files and
library versions. Ownership is always checked through the pipeline or project, in one helper
per service.

## 6. AI drafts per document

### 6.1 Start

"Draft" on a slot opens today's dialog (`step-drafts.tsx`) for that slot:

1. the user's own files: newest version of every live slot of this step, and of earlier
   steps when the step type allows; **`received` slots unticked by default**, except in a
   follow-up step, where the parent step's `received` files (the IRB's opinion being
   answered) are ticked; at most 15 (unchanged);
2. **a sibling site's final file of the same kind** (4.5), when there is one;
3. study library versions (phase D): those attached to this step's slots, ticked;
4. board documents of the hospital, when the step type allows (unchanged);
5. the form: the kind's form, else the step type's, else none; hospital forms offered first;
6. the consent text, now also saying that the site's details go to the AI service (6.3);
7. in a step where site facts were changed (4.1): the old and new values, for a
   comparison table.

The server checks, counts, audit-logs and queues exactly as today
(`DocgenService.prepare_draft`, 202 with a job). The audit entry `docgen.generate` gains the
slot id and name, and the library versions (ids and labels, never text).

### 6.2 Prompt

Order of blocks (new ones marked):

1. fixed safety rules (code);
2. admins' added text for `docgen`;
3. step type instructions, then **the kind's instructions (new)**;
4. guides: global, hospital, step type, **kind (new)**, attached;
5. **site facts (new)**, as data;
6. documents: own step files, **a sibling's file (new)**, **library versions and their
   changes (new)**, board documents;
7. the form's fields, when a form is filled.

### 6.3 Site facts are untrusted and personal

- PI name, site number, `subject` and the site facts (names and values) are typed by
  users. Like the step name today, **they never enter the trusted instruction block**;
  they go in a data block with the closing tag defused (`ai/tags.py`).
- **The site's current details are always sent with a draft made in a pipeline (D18).**
  A PI's name and staff names are personal data; so are the names inside a sibling's file,
  CVs and Delegation Logs. There is no switch: the dialog says next to the file list that
  the site's details (it names the fact names, not hidden) go to the AI service; starting
  is the consent (today's rule for own files); the audit entry records that site facts were
  sent and their names, never their values.
- **CVs and GCP certificates (site-level library, `people`) are never ticked by default**
  in a draft dialog; they are attached to slots as files, not read by the AI, unless the
  user ticks them.
- Only **current** fact values are sent, plus the old values of facts changed in this
  step. Removed facts (empty value) are not sent.

### 6.4 Gateway and models per job (proposal; asked by the owner 2026-11-13)

**Today** (read in the code on 2026-11-13):

- Three OpenAI clients are built in `ai/factory.py` from the same four settings
  (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, timeout): the summarizer
  (`ai/openai_summarizer.py`), the chat model (`agent/openai_model.py`) and the draft writer
  (`docgen/openai_writer.py`). All use the **Responses API** with `store=False`
  (`responses.parse` for typed answers, `responses.create` streamed for the chat).
- So **one gateway address covers every OpenAI call**: scan summaries, on-demand and admin
  summaries, the chat, drafts, revisions, form answers and the admin test box. **No gateway
  is chosen or set up** (`decisions.md`, "Model control": "Gateway not chosen yet").
- **Not covered: Jev** (`ai/jev.py`). It uses the TypeSafe SDK with its own key and no
  address setting, so it bypasses a gateway.
- **One model for everything:** `OPENAI_MODEL`. There is no per-feature or per-job model.
- **Not recorded:** which model wrote a draft, and token counts of any call. Usage is
  counted per call (audit entries) only.

**Needed for "a different model per job":**

1. **Model profiles, not model names, in the database.** Settings map a profile to a model:
   `OPENAI_MODEL` (the default) plus optional `OPENAI_MODEL_SUMMARY`, `OPENAI_MODEL_CHAT`,
   `OPENAI_MODEL_DRAFT`, `OPENAI_MODEL_DRAFT_STRONG`, each falling back to `OPENAI_MODEL`.
   Still no model name in code. With a gateway the values can be the gateway's own aliases,
   so a model is swapped at the gateway without a deploy.
2. **A profile per document kind:** `step_type_documents.ai_profile text not null default
   'draft'` (checked against the profile list in code), so a long 신청서 can use the strong
   model and a short 사유서 the cheap one. The draft writer takes the model per call
   instead of per instance (`OpenAIDraftWriter` holds one model today).
3. **Record what ran:** `draft_jobs.model text`, `draft_jobs.input_tokens`,
   `draft_jobs.output_tokens` (from the response's usage), and the model on each AI draft
   version. Needed for cost per organization (SaaS rule: usage per user and action) and for
   comparing models.
4. **Gateway requirements, to check before choosing one:** it must pass the Responses API
   (many "OpenAI-compatible" gateways pass chat completions only), typed output
   (`responses.parse`), streaming, and `store=False`; it must not keep request bodies
   (study documents and PI names go through it); and if another provider's model is put
   behind it, the typed output and the injection defences must be tested again per model.

None of this is built or decided. Item 3 is cheapest to add with phase C, where
`draft_jobs` changes anyway. Open choices: O29.

## 7. Frontend (repo `irb-watch-frontend`)

The pages follow the real folder levels: study, hospital (IRB), site (PI), step, document.

### 7.1 Routes

| Route | Content | Phase |
|---|---|---|
| `/organization/projects` | Organization admins only: every pipeline project of the organization with owner, PM, sites and open steps; set PM, hand over | B |
| `/work` | **My IRB work** (a menu item, not the start page; D20), over all projects the user may open: overdue and due steps, steps waiting for the IRB (submitted, no decision), required documents missing, drafts being written or failed. Each line links to its step page. A CRA with many studies starts here | A2 |
| `/projects/[id]` | Tabs. **Sites**: grouped by hospital; per site: number, PI, current step, its status, next due date, "Start pipeline" / "Open". A hospital with one unnamed site shows as one line. **Study documents**: the project-level library, the status table with "start amendment", the change log (phase D). **Board documents**: today's hospital list and feed. **Members**: owner, PM, members; add from the organization, hand over | B, D |
| `/projects/[id]/hospitals/[hospitalId]` | **The hospital (IRB) page**: its PIs side by side (one column per site, one row per step type and number, status in the cell); the hospital-level files (forms, membership list); which document lists and forms apply here; the hospital's board documents. Shown only for a hospital with more than one site or with hospital-level files; otherwise the site page covers it | B, D |
| `/pipelines/[id]` | **The site page (overview only).** Header: project, hospital, PI, site no, progress. **Site details**: the facts with their current values, "Change" (value, from which day, optional step), and each fact's history. One row per step: number (follow-ups indented as `02-1`), name, subject, status, IRB result, due / submitted / decided dates, "3 of 5 documents", a mark when a draft is being written. Each row links to the step page. Also: add step, reorder, removed steps, whole-pipeline history, the site's own files (phase D), remove pipeline. **No uploads or drafts here** | A |
| `/pipelines/[id]/steps/[stepId]` | **The step page** (7.2) | A |
| `/drafts/[id]` | Unchanged editor; gets a "Back to step" link and, in phase C, "Use as final file" | A, C |
| `/admin/pipeline` | Step type editor gets category, numbered, and a **Documents** list (global, and per hospital with "start from the global list") | B, C |

### 7.2 The step page

Top to bottom:

1. **Breadcrumb and step switcher:** project › hospital › site (PI) › step; previous / next
   step links (in overview order, removed steps skipped).
2. **Step header:** editable name, `{n}차`, subject, type, status select, **IRB result
   select**, due / submitted / decided dates, note. Same behaviour and API as today's
   `StepCard`. A follow-up shows "Answer to: <parent step>". On an amendment step:
   "Record a change of site details" (4.1).
3. **Documents**, grouped as the real folders are:
   - **신청서 및 통지서**: `application` slots, then `received` slots;
   - **제출 서류**: `submission` slots;
   - **참고**: `reference` slots.

   One row per slot:
   - name, required mark, origin tag, status;
   - the current file: newest version (file name, size, date, download), or the attached
     library version (name, label), or "nothing yet";
   - actions by origin: **Draft** (draft), **Upload**, **Attach from library** (phase D),
     **Mark final / Not needed**;
   - expandable: **all versions** (number, file name, who, when, "from draft N", download
     each), the slot's drafts (newest first, link to `/drafts/[id]`, download the draft's
     file), the job being written or why the last one failed (`DraftJobLines`), the note.

   "Add document" at the end of each group; removed documents under a toggle.
4. **Missing:** a one-line summary of required slots not done, shown again as the warning
   when the status is set to `submitted`.
5. **Follow-up steps** of this step, with "Add follow-up step".
6. **Copy this step to other PIs of this hospital** (phase C; shown when there are sibling
   sites).
7. **History of this step** (`GET /pipelines/{id}/history?step_id=`), collapsed.
8. Remove step.

### 7.3 Code changes

- `pipeline-view.tsx` (581 lines) splits into `pipeline-overview.tsx` (list, add step,
  history, remove) and `components/pipelines/step/` (`step-page.tsx`, `step-header.tsx`,
  `slot-list.tsx`, `slot-row.tsx`, `slot-upload.tsx`); `step-drafts.tsx` becomes the
  per-slot draft dialog.
- React Query keys: `["pipelines", "detail", id]` (overview) and
  `["pipelines", "step", id, stepId]` (step page). A change on the step page invalidates
  both. Only the step page polls for jobs, and only while one of its jobs is open.
- Old links keep working: `/pipelines/[id]` still exists; a `#step-<id>` anchor is not
  kept.
- Messages go in the `pipelines` namespace (ko and en), as today.

**Phase A can ship before any schema change:** the step page then shows today's documents
(named, versioned) and today's step-level drafts, and gains slots in phase C.

## 8. Phases

Each phase: ask its open choices first (section 10), then build, offline tests, code review,
migration dry-run, the owner's "go" before applying, then both repos deployed together.

| Phase | Content | Migration | Depends on |
|---|---|---|---|
| **A. One step per page** | `GET /pipelines/{id}/steps/{step_id}`, lighter `GET /pipelines/{id}`, `history?step_id=`; frontend overview + step page. No behaviour change otherwise | none | - |
| **A2. My IRB work + reminders** | `GET /work`, page `/work`; the old phase 4 (digest lines for steps due within 14 days) reads the same list. **Built 2026-11-13** (decisions.md "IRB pipeline redesign, phase A2") | `step_reminders` (one column: the reminders' own switch) | A |
| **B1. Sharing** | `project_members`, PM, organization admins, hand-over, the two access functions, new RLS policies on `projects` and `project_hospitals`, server access helper for today's pipelines, steps, files, step drafts and their jobs, Members tab, organization projects page. **A2 follow-up:** `/work` (`PipelineService.work_steps`) and the reminder recipients (`reminder_steps`) key on `pipelines.user_id` today; switch both to project access (`/work`: every project the user may open, with "mine only"; reminders: owner and PM), or after a hand-over the old creator keeps the reminders and the new owner's `/work` is empty | `project_sharing` (**changes RLS on live tables: test with a rolled-back RLS test and a real second user before applying**) | A |
| **B2. Sites per PI** | `project_sites` (PI optional), `site_facts` with history, `pipelines.site_id`, follow-up steps, numbering, subject, step type category and seeds, `step_results`, global order = 초기 심의; service split; project Sites tab, hospital page, site details | `project_sites` (drops the project+hospital unique index: **old server breaks on start-pipeline; deploy both**) | A, B1 |
| **C. Document slots** | `step_type_documents` (global + per hospital), slot columns, per-slot drafts and job limit, shared pipeline drafts, "use as final" and upload-from-draft, copy step to sibling sites, sibling file as draft input, admin kinds editor | `step_document_slots` | A, B2 |
| **D. Study library** | library tables at three levels, attach by reference, status table with flags, change log, `study_events` + "start amendment", library versions in drafts, from/to comparison | `study_library` | B2, C |
| **E. Batch drafts** | "draft for all" on a study event, new job limits, worker outside the API process | `draft_batches` | D |

Order: A, A2, B1, B2, C, D, E. Sharing first inside B because every later table is read
through the same access rule; B2 before C because sibling reuse and per-hospital lists
need sites. Phase B is now the largest phase: B1 and B2 are separate migrations and can be
deployed separately.

Tests per phase (offline, in-memory fakes in `tests/fakes_pipelines.py`, as today): owner
checks (stranger gets 404), limits, soft delete and restore, purge order (files first, rows
after), races that the unique indexes must catch (two sites with one PI name, two open jobs
on one slot, a step copied twice to one sibling), and the backfill rules as plain SQL
checked read-only after the migration.

## 9. SaaS notes

- `organization_id` (default = Default organization) on `project_sites` and `study_events`;
  library documents, slots and versions follow their project or pipeline (no column), like
  today's children.
- Document kinds, global and per hospital, are **server-wide** settings (platform admin),
  like step types: one hospital's list serves every customer working with that hospital.
  This is a shared setting on purpose; an `organization_id` can be added to `step_types` and
  `step_type_documents` later (an organization's own list winning over the hospital's)
  without changing the feature.
- All access stays behind the store protocols. **Organizations now grant one thing:** an
  organization's admins open that organization's pipeline projects (D12, 4.8). This makes
  `projects.organization_id` an access column: it must be set from the owner's organization
  when a project is made (the browser must not choose it; today a column default fills
  Default), and it must follow a hand-over. Moving a user to another organization does not
  move their projects (open, O23).
- Usage: draft jobs are already counted per user and action. A batch (phase E) must count
  each job, and the audit entry must carry the batch id.
- Storage paths start with the project or pipeline id, not an organization id. Adding a
  tenant prefix later means new paths for new files only (paths are stored per row).
- Slow work: "use as final" builds a DOCX in the request (small, already done for export);
  copying a step to siblings writes rows only. Batch drafts are jobs.
- **MFDS (D21):** not designed. It could later be one more review body in the catalog with
  its own step types; a project-level track would need a site without a PI, which the
  optional PI already allows.

## 10. Open choices (ask before the phase is built)

Answered on 2026-11-13 and moved to the decisions at the top (D7 to D21): site fields and
rules, first steps, step types, the result field, sharing, the hospital list, sibling copy,
draft to file, missing documents, site details to the AI, the library, where `/work` sits,
MFDS. Still open (numbers kept from revision 2 where the question is the same):

| # | Phase | Question | Proposal |
|---|---|---|---|
| O7 | B2 | The seeded statuses 승인 / 반려 / 보완 요청 overlap the new result list. Does the migration rename them (one closed status 완료, 보완 요청 kept as a work state), or leave them for the admin to tidy? | Leave them; the admin tidies |
| O15 | E | Batch limits, and moving the worker out of the API process | Decide when E starts |
| O16 | C | 서류 바인더 (paper copies), `IRB 사전 검토`, `참고자료`: tracked as `reference` slots, or left out? | `reference` slots, user-added |
| O18 | A2 | `/work`: are the four lists of 7.1 the right ones, and does a PM or organization admin see the work of all their projects there? | **Decided 2026-11-13: the four lists; from B1 everything they may open, with a "mine only" filter. Submitted steps are never due or overdue** |
| O20 | B2 | A shared IRB (`공용 IRB`): added to the catalog by the platform admin (as designed), or a fixed built-in row? | Admin adds it |
| O21 | B1 | Who sets the PM: organization admins only, or the owner too? May the PM add members? | **Decided 2026-11-13: owner or org admin sets it; the PM adds and removes members** |
| O22 | B1 | A shared draft that used a members-only board another member may not read: warn (as designed) or refuse to share that draft? | **Decided 2026-11-13: warn** (brought into B1: step drafts are shared from B1) |
| O23 | B1 | A user moved to another organization: do their projects stay with the old organization (they lose access unless still a member there), or move with them? | **Decided 2026-11-13: stay**; the move also resets the organization role to member |
| O24 | B1 | Is a read-only role needed (sponsor contact, auditor)? The owner's answer named only admin, PM and CRA | Assumed no (to confirm); the level function leaves room |
| O25 | B1 | May a platform admin open any organization's projects (support), audit-logged? The owner chose "organization admin, own organization only" | **Decided 2026-11-13: read-only, every project, no extra audit** |
| O26 | B2 | The suggested fact names on the site page (PI 직위, 진료과, Sub-I, CRC, CRA, IRB 과제번호, 증례당 연구비): fixed in code or an admin-edited list? | Fixed in code |
| O27 | A2 | Due-date reminder mail in a shared project: who gets it (owner, PM, every member)? | **Decided 2026-11-13: owner and PM** (the PM is added with B1) |
| O28 | C | **The global document lists start empty** (no step type has a checklist today). Seed 초기 심의 and 변경 심의 from the real folders (신청서, 통지서, Protocol, CRF, ICF, 제품설명서, 변경대비표, ...), or does the admin type them? | Seed a short list the owner confirms first |
| O29 | C | Models per job (6.4): which profiles exist, may an admin pick a profile per document kind, and which gateway? | Profiles default / strong; admin picks per kind; record model and tokens in phase C; gateway chosen separately |

## 11. Not verified

- **No file was opened.** Which documents can really be drafted by the AI, which fields
  the hospital forms have, and whether PDFs are scanned (no text) are unknown. Before phase
  C, the owner should confirm the kind list for 초기 심의 and 변경 심의 against two or three
  real sites, at least two of them PIs of one hospital.
- **D6 (same hospital, same list) was seen in one case only**: the 초기심의 file names of
  two 동아대학교병원 PIs. The other multi-PI hospitals were not compared.
- The counts in section 1 are from name matching over folders at most 5 levels deep;
  spelling variants were merged by hand rules, and project shapes were sorted by a
  heuristic (a folder name with 병원, 의료원, 센터, PI). Treat them as sizes, not exact
  numbers.
- Most real files are PDF; form templates accept only `.docx`. HWP and HWPX hospital forms
  (seen in the samples) cannot be filled today.
- All projects belong to one company, so the folder layout is its own convention; another
  CRO may organise differently.
- Existing pipeline features are tested offline only and not deployed; draft quality with
  the real model has not been seen (see `plan.md`).

## 12. Changes in revision 2

| Area | Revision 1 | Revision 2 | Why |
|---|---|---|---|
| Site | Hospital + PI, PI expected | PI and number optional; one click makes site + pipeline | About 21 of 38 projects have one IRB and no site folder |
| Shared IRB | Not covered | A catalog row like a hospital | 4 projects use `공용 IRB`, one with hospital IRBs too |
| PI change | Not covered | Site kept, change in the history | Seen in a real site folder |
| Document lists | Global per step type; per hospital "not now" | Global, or the hospital's own list | D6: one IRB per hospital |
| Reuse between PIs | Only the later study event | Copy step to sibling sites; sibling's file as draft input; hospital page | D6 |
| Library | Project level only | Project, hospital and site levels, with folders | Company template (`Study Document`, `Investigator & Site`), `양식` and membership-list folders |
| Step types | 6 seeded + 3 follow-ups | Categories, a wider seed, numbered follow-up rounds, `subject` | Section 1.2 |
| Results | Existing statuses | Three more statuses proposed | Results in folder names |
| Sections on the step page | Four flat sections | The two real folders (신청서 및 통지서, 제출 서류) plus 참고 | 1.3 |
| Pages | Project, pipeline, step | + `/work`, + hospital page, project tabs | D5; a CRA has many studies |
| Phases | A, B, C, D, E | A, A2, B, C, D, E; sibling copy moved into C | Reuse is needed long before batch drafts |

## 13. Changes in revision 3 (owner's answers, third round)

| Area | Revision 2 | Revision 3 |
|---|---|---|
| Site fields | PI title, department, a `facts` json | Only site number and PI are columns; `site_facts` rows with dated history and a step link (D7) |
| IRB result | Three more statuses | Separate `step_results` list and `pipeline_steps.result_key` (D11) |
| Sharing | Owner only, "ask later" | Phase B1: members from the organization, one PM per project, organization admins see their organization's projects, hand-over (D12). **Organizations now grant access to pipeline projects** |
| Drafts | Private to their author | Pipeline drafts are shared with the project's members (D13) |
| Draft to file | A button | Button, or download / review / upload with the draft link kept; always a new version (D16) |
| Site details to the AI | A switch proposed | Always sent, stated in the dialog (D18) |
| Site ICF | Proposed as an AI draft | Uploaded by hand (D19) |
| Library | Audit entries only | Own change log; older-version flags; "start amendment" (D19) |
| Study events | All in phase E | Table and "apply to sites" in phase D; only batch drafts in E |
| Phases | A, A2, B, C, D, E | A, A2, B1 (sharing), B2 (sites), C, D, E |
