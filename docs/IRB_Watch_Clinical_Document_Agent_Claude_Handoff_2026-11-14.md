# IRB Watch × Clinical Document Agent — 아키텍처 전환 및 개발 인수인계 문서

> **문서 성격:** Claude Code CLI가 기존 IRB Watch의 맥락과 앞으로의 방향을 이해하기 위한 기획·설계 인수인계 문서. **즉시 구현 명령서가 아니다.**
>
> **작성 기준일:** 2026-11-14 (Asia/Seoul)  
> **대상:** `pjh5474/irb-watch`(FastAPI), `pjh5474/irb-watch-frontend`(Next.js), 향후 별도로 구축할 `clinical-document-agent`(Cloudflare Think)  
> **우선순위:** 기존 저장소의 실제 코드·마이그레이션·테스트 및 `CLAUDE.md` 지침이 사실 확인의 1차 근거. 이 문서는 **제품 방향과 목표 구조**의 근거.

---

## 0. Claude Code에 가장 먼저 전달할 내용

### 0.1 소유자 의도 — 절대 놓치지 말 것

1. **IRB Watch는 Claude Code CLI로 작업을 이어가는 기존 프로젝트다.** 지금까지 구현한 데이터 수집·변경 감지·버전 관리·프로젝트 및 IRB Pipeline을 보존하면서, **IRB 일정·마감일·통합 캘린더 중심의 업무 관리 기능을 강화**한다.
2. **AI 문서 작성 기능은 장기적으로 IRB Watch에서 분리한다.** 독립된 `Clinical Document Agent`가 IRB·식약처(MFDS) 관련 문서·SIV·MV·COV·계약서 등 CRA 문서 작업을 지원한다.
3. **Cloudflare Think 프로젝트의 구현은 소유자가 직접 진행**한다. ChatGPT/Claude 등에게 설계 의견·피드백을 받되, **Claude Code가 IRB Watch 작업을 한다는 이유만으로 외부 Think Agent 저장소·Worker·Skills를 만들거나 수정하지 않는다.** 소유자의 명시적 지시가 있을 때만 수행한다.
4. 기존 IRB Watch의 **읽기 전용 AI Assistant는 유지**한다. 공식 문서·버전·변경사항의 검색 및 답변이 주 역할이다. **문서 작성·수정·대량 생성 Agent와 혼동하지 않는다.**
5. **이미 동작하는 Draft 기능을 당장 삭제하지 않는다.** 새 Agent와의 기능 대체, 데이터 호환성, 보안·품질 검증, 이관 계획이 완성되기 전까지 안정적으로 유지한다.
6. 현 단계에서는 **새로운 프론트엔드를 만들지 않는다.** IRB Watch의 Next.js UI에서 필요한 경우 독립 Worker로 연결한다. 장기적으로 독립적인 Clinical Workspace UI가 필요해질 때 별도 검토한다.
7. 이 문서의 설계안은 대부분 **합의한 방향/권고안**이지 실제 구현·운영 완료 사실이 아니다. 상태를 반드시 구분해 기록한다.

### 0.2 Claude Code 첫 응답에서 기대하는 행동

- `CLAUDE.md`, `docs/plan.md`, `docs/decisions.md`, `docs/pipeline-design.md`, 실제 Supabase migrations 및 해당 프론트엔드 저장소의 `AGENTS.md`/계획 문서를 읽는다.
- **현재 구현된 것 / 계획 중인 것 / 이 문서에서 새로 제안하는 것**을 나눠 차이점과 충돌을 보고한다.
- 우선 **설계 검토와 작업 분할안**을 제시한다. 코드 변경·마이그레이션·실데이터 테스트·배포·커밋은 기존 `CLAUDE.md`의 사전 승인 규칙에 따른다.
- 현재 작업 중인 IRB Pipeline B2 및 공유 권한 정책을 훼손하지 않는 최소 변경 순서를 제안한다.
- 추측으로 기존 테이블 이름, API URL, 작업 상태를 확정하지 말고 저장소와 마이그레이션으로 확인한다.

---

## 1. 프로젝트 배경과 장기 비전

CRA(Clinical Research Associate)는 한 Study에서 10~20개 이상의 Site(의료기관)를 담당할 수 있다. 동일 Study의 Protocol·IB 등의 공통 정보를 사용하지만, 병원별 IRB 서식·일정·요구자료·담당 PI가 다르며 심의 단계마다 상당수의 문서를 작성·재제출한다. 연구자는 여러 Study를 동시에 관리할 수도 있다.

이를 해결하기 위해 두 서비스를 분리하여 발전시킨다.

### 1.1 IRB Watch — 규제 문서 및 IRB 업무 운영 서비스

- 병원별 IRB 게시판/서식/공지의 자동 수집, 변경 감지, 버전 이력, 원본 파일 제공.
- **읽기 전용 Assistant**로 수집한 문서 정보 및 버전을 검색·질의응답.
- Study → Site(병원) → IRB 심의 단계별 업무 현황, 문서 제출 상태, 담당자 및 일정 관리.
- **여러 Study·Site를 통합해 보여주는 캘린더**와 마감 알림.
- 병원 공식 접수마감·심의 개최 정보와, Study별 제출 목표·실제 보완 마감 등을 구분.
- 추후 Google Calendar 연동을 통한 개인 일정 확인 및 알림.

### 1.2 Clinical Document Agent — CRA 전체 문서 작성 실행 엔진

- IRB 초기심의/변경심의/지속심의/종료보고/보완답변서.
- MFDS 관련 제출·보완자료 작성 지원(역할·법적 승인 범위·SOP 준수).
- SIV(Site Initiation Visit), MV(Monitoring Visit), COV(Close-Out Visit) 등 방문 기록·Checklist·Report·Follow-up Letter.
- 계약서, 연구비 서류, 안내문, 공문, 회의록 등. 단, **계약 및 임상시험 의학적·규제적 내용의 최종 검토/승인은 사람에게 있다.**
- 기존 템플릿 + 참고 문서 + 사용자 지침 + 프로젝트 데이터로 Draft 작성·부분 수정·보완.
- **Study 공통 내용을 한 번 추출하여 다수 Site/PI 문서를 Batch 생성**하는 기능.
- 병원별 공식 지침과 검증된 심의 보완 사례를 사전 검토에 활용하는 기능.

### 1.3 장기적으로는 Clinical Workspace

- IRB Watch가 모든 임상시험 자료의 영구적 소유자가 될 필요는 없다.
- 장기적으로 `Clinical Workspace` 또는 공통 Study/Document API가 Study·Site·PI·Document·Visit·Draft·Review 등 도메인의 권위 있는 저장/접근 계층이 될 수 있다.
- **그러나 지금 별도 Workspace DB/서비스까지 즉시 구축하는 것은 미결정**이다. 현 시점에는 기존 Supabase와 IRB Watch의 안정된 모델을 활용해 단계적으로 분리한다.

---

## 2. 현재 시스템과 저장소: 확인된 사실

### 2.1 저장소 링크

| 저장소 | 역할 | 링크 |
|---|---|---|
| `irb-watch` | FastAPI 백엔드, 수집/분석/문서/IRB 업무 API, **Supabase migrations의 기준 저장소** | https://github.com/pjh5474/irb-watch |
| `irb-watch-frontend` | Next.js 웹앱, Supabase Auth/RLS 기반 조회, 백엔드 API 호출 | https://github.com/pjh5474/irb-watch-frontend |
| `cra-assistant-agent` | **기존 실습·포트폴리오**: Cloudflare Workers + Think/Agents SDK, 외부 API Tool 연계 | https://github.com/pjh5474/cra-assistant-agent |
| `clinical-document-agent` | **제안된 별도 신규 프로젝트**, 아직 이 문서만으로 실재·배포가 확인된 것이 아님 | 저장소 이름은 제안값 |

### 2.2 현재 배포 구조

```mermaid
flowchart TB
    U[사용자] --> FE[IRB Watch Next.js / Vercel]
    FE -->|인증된 사용자 읽기 / RLS| DB[(Supabase PostgreSQL)]
    FE -->|쓰기·다운로드·관리 작업| API[FastAPI / AWS EC2]
    API -->|서버 권한 검증| DB
    API --> STORE[(Supabase Storage)]
    API --> CRAWL[IRB 사이트 크롤링·변경 감지]
    API --> EMAIL[Resend 메일·리포트]
```

- 프론트엔드 README 기준: **Next.js 16 App Router, React 19, TypeScript, Tailwind 4, next-intl, TanStack Query, Supabase SSR**. 예전 Next.js 14를 전제로 설계하지 말 것.
- 백엔드 README 및 `CLAUDE.md` 기준: Python 3.12/FastAPI/uv, EC2 배포, Supabase DB·Auth·Storage, 정기 스캔, 원본·버전 저장, 이메일, 프로젝트/파이프라인, Assistant, Draft Generator.
- Supabase **service role/secret 키는 신뢰 가능한 서버에만 존재**한다. 프론트엔드에 넘기지 않는다.
- 병원별 원본 문서 및 버전, 추출 텍스트, 감사 이력 등은 기존 자산으로 반드시 보존한다.

### 2.3 이미 있거나 진행된 기능(일부 검증 수준 주의)

- 병원 IRB 게시판 수집 및 문서 변경·삭제·재등장 감지, 파일 원본 및 추출 텍스트 저장.
- 신규/변경 문서 AI 요약 및 일간 메일·관리자 리포트.
- 병원(`hospitals`) 카탈로그와 다양한 게시판 소스 연계.
- 회원 제한 문서/게시판 접근 정책(소스별 권한).
- 사용자 프로젝트 및 병원별 IRB Pipeline, 단계별 날짜·상태·문서 슬롯, 작업 이력.
- **프로젝트 공유 모델**(조직 관리자·PM·CRA·프로젝트 멤버 등)이 2026-11-13 전후 구현·확장 중. `docs/pipeline-design.md` 4.8 및 실제 코드/마이그레이션 확인 필요.
- `docgen` 기반 Draft 생성, 서식·가이드 관리, DOCX 필드 채우기, 초안/수정 작업(Job) 처리 등이 이미 존재한다.
- 여러 IRB Pipeline 개선 단계(A/A2/B1/B2 등)가 진행 중이다. 새로운 캘린더 설계가 기존 `/work`와 마감 알림 기능을 중복 구현하지 않도록 해야 한다.

> **중요:** README 및 문서 일부는 서로 다른 날짜의 상태를 기술하며 migration 수·배포·테스트 상태가 일치하지 않을 수 있다. 2026-11-14 기준 HEAD, 실제 migrations, 서비스 테스트를 먼저 확인하고 **확인된 사실만** 확정한다. 이 문서가 배포 완료를 보증하지 않는다.

---

## 3. 새로운 책임 분리: 무엇을 어디에 남길 것인가

| 기능 | 주 책임 | 비고 |
|---|---|---|
| IRB 게시판 수집·원본 보관·버전 관리 | **IRB Watch** | 기존 FastAPI 유지 |
| 새로 올라온 서식 목록/변경사항에 대한 Q&A | **IRB Watch Read-only Assistant** | 원칙적으로 문서 변경·생성 권한 없음 |
| 프로젝트/기관/심의 Pipeline·업무 상태 | **IRB Watch(현재)** | 향후 Workspace 경계 재검토 |
| 병원 공식 IRB 일정 및 프로젝트별 마감 | **IRB Watch** | 신규 캘린더/알림 기능 |
| Google Calendar 연결·이벤트 동기화 | **IRB Watch API** | 후순위, 단방향 우선 |
| 개별 문서 Draft 작성·재작성·보완 | **Clinical Document Agent** | TypeScript/Cloudflare Think |
| IRB 심의의견 기반 부분 수정·답변 | **Clinical Document Agent** | 근거·수정 비교·승인 필요 |
| Site/PI별 일괄 문서 생성 | **Clinical Document Agent + Workflow** | Batch, 멱등성, 부분 실패 |
| 병원별 서식/공식 지침 원본 | **IRB Watch/공통 Knowledge API** | Agent가 버전 고정 조회 |
| 병원별 검증된 문서 작성 지식 | **별도 구조화 Knowledge 계층(장기)** | Think 메모리와 분리 |
| 정밀 DOCX/HWPX 처리 | **문서 처리 엔진(초기에는 Python 재사용 가능)** | 실행 주체와 파일 렌더러의 분리를 허용 |
| 영구 Draft/문서 버전·감사 이력 | **Supabase의 권위 있는 도메인 저장소** | Agent Workspace가 유일한 보관처가 아님 |

### 3.1 현재 docgen의 이전 원칙

- **Orchestration(작업 계획·AI 생성·Tool 호출)**은 별도 Think Agent로 옮기는 목표.
- 원본·버전·권한 검사와 기존 데이터 API는 IRB Watch/공통 API가 맡을 수 있다.
- `docgen`의 유용한 PDF/DOCX 파싱·서식 인식·필드 매핑·렌더링은 당장 폐기하지 말고 **도구/API로 재사용하거나 향후 별도 엔진으로 추출**한다.
- 저장소 내 구형 UI/API/테이블은 **새 Agent가 품질·보안·완성도를 검증받고 데이터 이전까지 확인한 이후** 단계적으로 비활성화·삭제한다.
- IRB Watch에 남는 것은 문서 **조회·진행상태·필수문서 체크리스트·제출 이력·버전 링크**. 외부 Agent를 연결하는 UI 진입점은 남길 수 있다.

---

## 4. 목표 아키텍처

```mermaid
flowchart TB
    USER[CRA / PM / 조직 관리자] --> FRONT[IRB Watch Frontend / Next.js]
    FRONT -->|읽기: RLS / 인증 API| IRB[IRB Watch FastAPI]
    FRONT -->|문서 작업 연결: 권한 검증된 세션| AGENT[Clinical Document Agent / Cloudflare Think]
    AGENT -->|Scoped Tools / 인증된 서버 간 API| IRB
    AGENT -->|제한된 도구| DOC[Document Engine / Python API 등]
    AGENT -->|Batch Job| WORK[Cloudflare Workflows]
    WORK --> DOC
    IRB --> DB[(Supabase DB + Storage)]
    DOC -->|검증된 Draft 저장 API| IRB
    IRB --> KNOW[(Hospital Knowledge / 공식 서식 버전)]
    IRB --> CAL[IRB Schedule + Calendar Sync]
```

#### 중요한 기술 선택

- **OpenClaw와 Cloudflare Think는 다른 기술이다.** 이 논의에서 이전에 'OpenClaw'라고 부른 실습 저장소는 실제로 **Think/Agents SDK** 기반이었다.
- 이번 독립 Agent의 **초기 선택은 TypeScript + Cloudflare Think/Agents SDK**다. OpenClaw Gateway를 반드시 설치할 필요가 없다.
- 기존 `cra-assistant-agent`는 `Think`, 외부 FastAPI Tool 연동, React `useAgent`/`useAgentChat`, Workflow 등의 **참고 구현**이다. 코드를 무작정 복사하지 않는다.
- 현재 Think 문서에서는 `@cloudflare/computer` Workspace가 권장되며 `@cloudflare/shell`은 2026-10-20 deprecated되었다. 새 Worker 개발 시 라이브러리/마이그레이션을 **소유자가 직접** 확인한다.
- Next.js UI는 추후 `useAgent`/`useAgentChat` 또는 인증을 추가한 `useDocumentAgent`로 연결할 수 있다. **Hook 사용이 권한 검사를 대신하지 않는다.**
- Worker를 별도 프로젝트로 개발한다는 것은 **독립 배포·수명주기·보안 경계**가 있다는 의미이지, 별도 React UI 또는 EC2 서버가 필수라는 의미는 아니다.

---

## 5. 도메인 모델: Study, Site, PI, Document, Schedule

### 5.1 기본 계층

```text
Organization / Tenant
  └── Study / Project
       ├── Study-level references
       │    ├── Protocol (versioned)
       │    ├── IB, Monitoring Plan, SOP, shared budgets
       │    └── approved master content
       └── Study-Site (한 연구에 속한 실시기관)
            ├── Hospital → official forms, schedules, knowledge
            ├── Principal Investigator / site personnel / contacts
            ├── IRB Pipeline → steps, submission, decision, follow-up
            ├── site-specific facts and documents
            ├── Visits → SIV / MV #1 / MV #2 / COV
            ├── Document Slots → versions, drafts, approvals
            └── Events → review, submission deadline, reminders
```

- **Hospital**은 기관 공통 특성(정규심의/공식 서식)을 나타낸다.
- **Study-Site**는 특정 Study의 해당 병원 운영 단위(PI·계약·제출·버전·담당자)를 나타낸다.
- 동일 병원이 여러 Study에 참여하더라도 비공개 문서가 혼합되어서는 안 된다.
- 동일 Study의 서로 다른 병원에서는 공통 Protocol은 공유하되 IRB 서식·심의 상태는 별개로 유지한다.
- 여러 PI가 같은 Site와 연관되는 경우 또는 Site별 별도 제출 단위가 있는 경우를 모델링할 수 있게 한다. **실제 B2a/B2b 모델의 키 구조부터 확인한 후 추가한다.**

### 5.2 식별자 및 버전 원칙

- 클라이언트 표시 이름(병원명, 연구명)을 식별자로 사용하지 않는다. 불변 UUID 등 내부 ID 사용.
- 각 공식 서식은 `template_id + version_id`, 참고 문서는 `document_id + version_id`로 고정한다.
- 생성 결과에는 당시의 템플릿 버전, 참고 문서 버전, Study/Site/PI 데이터 스냅샷, 지침, 모델 정보, 작업 ID를 남긴다.
- **Draft 생성 / 검토 / 승인 / 실제 제출 / 심의 결과 / 재제출**은 서로 다른 상태다.
- PDF/DOCX/HWPX의 파일 포맷별 한계를 표시하고 지원하지 않는 필드는 명확히 검토 필요 처리한다.

---

## 6. IRB Watch의 새로운 주력 기능: 통합 일정 관리

### 6.1 목표

CRA가 여러 Study에서 10~20개 Site를 맡더라도 **하나의 캘린더에서 기관별 접수 마감일·심의일·내부 준비일정·보완 제출 마감**을 파악하도록 한다.

### 6.2 일정은 두 레벨로 구분

| 레벨 | 예시 | 원본·기준 | 프로젝트별 차이 |
|---|---|---|---|
| **Hospital Official Schedule** | IRB 정규심의일, 해당 회차 접수 마감일 | 병원 공식 공지/관리자 확인 | 여러 Study가 참조 가능 |
| **Study-Site Event / Task** | Study A 초기심의 제출 목표, 심의보완 제출기한, 내부 최종검토일 | CRA/PM 입력, IRB 결과 통지 | 해당 Study-Site 소유 |

- '접수 마감'과 '심의 개최'는 별도 이벤트 종류다.
- '공식 일정'과 '사용자 업무 목표일'을 혼동하지 않는다.
- 심의 종류(정규·신속 등), 해당 회차/기관 공지별 적용 범위를 보존한다.
- 공지에 시각 정보가 없으면 임의의 17시 등을 넣지 않고 `date_only`/`time_unknown`으로 보존한다.
- 기본 시간대는 한국 기관에 대해 `Asia/Seoul`을 지원하되, 추후 국제 Site 확장이 가능하도록 이벤트별 timezone을 둔다.
- 확정/잠정/취소/변경 여부와 근거(공지 링크·문서 버전·확인자·확인일)를 남긴다.

### 6.3 기능 요구사항

**MVP**

1. Study·Site/병원별 공식/업무 일정의 등록·수정·취소·조회.
2. 월간/주간/목록 보기, 여러 Study를 합친 캘린더, Study/Site/담당자/심의 종류별 필터.
3. 캘린더 일정 클릭 시 Study-Site Pipeline 해당 단계로 이동.
4. 기한 임박·기한 초과 및 미완료 업무 표시, 권한 기반 보기.
5. 기존 Pipeline의 `due/submitted/decision` 등 날짜 필드와 **중복된 소스 오브 트루스가 생기지 않도록** 읽기 모델 또는 관계를 설계.
6. 변경/오류/취소 시 사용자에게 명확한 이유 표시(일정 없음으로 위장 금지).

**후속**

7. 공식 IRB 공지에서 후보 일정을 추출, 근거 표시, 사용자/관리자 검증 후 적용.
8. 알림 정책: 사전 14/7/3/1일 등의 사용자 설정(예시), 수신 대상/휴일 처리/중복 방지.
9. 캘린더에서 문서 준비상태 표시 및 **Clinical Document Agent 문서 작성 딥링크** 연결.
10. Google Calendar 단방향 동기화(선택 동의), 이후 필요시 양방향 검토.

### 6.4 데이터 모델 제안(아직 확정 아님)

아래 테이블명은 **개념안**이다. 기존 Pipeline 이벤트/알림 테이블과 비교해 재사용 또는 확장할 것.

```text
hospital_review_sessions?                 # 병원 공식 심의 회차 / 종류 / 일정
  id, hospital_id, review_type, session_date, cutoff_at,
  timezone, source_document_version_id, verification_status,
  applicable_scope, effective_from, effective_until, updated_at

calendar_events?                           # Study-Site 일정/업무 이벤트
  id, organization_id, study_id, study_site_id?,
  pipeline_step_id?, hospital_review_session_id?,
  event_type, title, start_at?, due_at?, all_day,
  timezone, status, source_type, source_ref?,
  assigned_user_id?, confirmed_by?, created_at, updated_at

reminder_policies? / notification_deliveries?
  id, user_id/role, event_type, days_before, channel,
  event_id, send_for, delivery_status, dedupe_key

calendar_connections? / calendar_event_mappings?
  id, user_id, provider, calendar_id, oauth_secret_ref,
  event_id, remote_event_id, last_synced_at, sync_status, error_reason
```

- 기존 `project_sites`, `pipelines`, `pipeline_steps`, `step_reminders`(실제 이름 확인)와 FK·계산 값이 겹치면 **정규화 및 읽기 뷰**를 우선한다.
- 날짜 변경은 감사 기록에 남기고, 일정 공유/프로젝트 이관 시 접근 권한을 다시 계산한다.
- 같은 공식 병원 일정을 프로젝트별로 무분별하게 물리 복제하지 않도록 한다.

### 6.5 Google Calendar 연동 설계

**1차 권장:** IRB Watch → 사용자 지정 Google Calendar **단방향**.

- Supabase/IRB Watch가 일정의 원본(Source of Truth)이고 Google은 알림·확인 채널이다.
- 사용자가 명시적으로 OAuth 권한을 부여한다. Google 로그인만으로 Calendar 권한이 생기는 것은 아니다.
- 가능하면 애플리케이션 전용 캘린더와 최소 권한 범위 사용.
- 각 내부 이벤트에 대응하는 Google Event ID를 저장하고 변경·삭제 시 동기화한다.
- 재시도 시 이벤트가 중복 생성되지 않게 멱등성 키/이벤트 매핑 및 실패 복구를 둔다.
- 민감한 Study/PI/임상정보는 Google 이벤트 제목/설명에 **최소한으로** 노출(사용자 설정 및 정책 확인).
- Token/Refresh token은 서버측 안전한 저장소에 암호화하고 프론트엔드에는 전달하지 않는다.
- 나중에 양방향 동기화를 도입할 경우 충돌 정책, 외부 삭제, 증분 동기화 토큰(`syncToken`), 410 재동기화 등을 따로 정의한다.

### 6.6 일정과 문서 작업의 연결 예시

```text
IRB Watch Calendar
  → Study A / Site 03 / 초기심의 접수 마감 (5일 남음)
  → 현재 필수문서 일부 미완료 표시
  → "문서 작업" 클릭
  → 인증된 Agent 세션 또는 생성 요청으로 연결
  → Draft 완료/검토 필요 상태가 IRB Watch에 반환
  → **실제 제출 완료는 담당자가 별도로 기록**
```

---

## 7. Clinical Document Agent 요구사항 — 참고 설계(소유자가 직접 구현)

### 7.1 입력 계약

필수 개념 3가지 + 자동 Context:

1. **작성할 기준 템플릿**: 병원별 초기심의 양식, SIV Visit Report 양식, 회사 SOP 템플릿 등.
2. **참고 문서 목록**: Protocol, IB, ICF, 연구비 산정서, 기존 MV 보고서 등. 파일 업로드 허용.
3. **사용자 지침**: 작성 목적, 문체, 포함/제외할 정보, 확인 필요 시 처리 원칙.
4. **자동 Project Context**: 승인된 Study, Study-Site, Hospital, PI, IRB 심의 단계, 관련 일정, 이전 제출본의 버전.

사용자 제공 자료는 암묵적으로 모든 문서에 공유하지 말고 **명시적으로 선택하거나 권한 검증된 정책**으로 포함시킨다.

### 7.2 단일 문서 작성·수정 파이프라인

```text
Identify document type / workflow
 → Authorize study, site, document, reference versions
 → Resolve official / sponsor / local template
 → Gather immutable reference versions and verified facts
 → Produce structured Draft fields + source references
 → Validate required fields and cross-document consistency
 → Render DOCX / supported format
 → Save immutable Draft version and validation report
 → User review / feedback / AI partial revision
 → Approved / submitted states only through explicit authorized actions
```

- AI가 원문에 없는 수치·방문 결과·연구비·동의 관련 정보를 **임의로 보충하면 안 된다.**
- 필드별 `value`, `source_refs`, `confidence_or_status`, `requires_review` 등을 제공한다.
- DOCX 전체 구조를 AI가 자유롭게 재작성하기보다 **구조화된 내용 + 검증된 Renderer**가 기본.
- 공식 서식 표/병합 셀/체크박스/정형 문구를 훼손하지 말 것.
- HWP/HWPX 파일에 대한 편집 지원 범위는 별도 검증 전까지 확정하지 않는다.

### 7.3 IRB 심의의견·보완 요청 처리

1. 실제 심의의견 통지서/보완 요청서 업로드.
2. 각 코멘트를 원문·근거·대상 문서·요청 유형별로 구조화.
3. 기존 제출본과 최신 참고 자료를 비교해 **변경이 필요한 부분만** 수정 Draft 제안.
4. 코멘트별 대응 매트릭스(의견 원문 ↔ 수정 파일/버전/항목 ↔ 답변 ↔ 확인자)를 저장.
5. 사용자가 보완답변서 및 재제출본을 검토·승인.
6. 코멘트의 범용성이 검증된 경우에만 Hospital Knowledge 후보로 등록.

**주의:** 행정적 보완, 수정 후 승인, 재심의, 반려 등은 서로 다른 결과다. 단일 '반송' 상태로 단순화하지 않는다.

### 7.4 Study-Site별 대량 생성(Batch Document Generation)

동일 Study에서 Site/PI별로 수십 개의 IRB 양식, 공문, 계약 관련 자료를 생성할 수 있어야 한다.

**권장 방식:** `Master Content`(구조화된 공통 내용) + `Site/PI Context` + `Template Resolver` + `Renderer` + `Validation`.

| 작업 특성 | 권장 방식 |
|---|---|
| 완전히 동일한 서식 | 공통 내용 1회 생성 → Site 필드 치환 |
| 병원별 서로 다른 서식 | Master Content를 병원별 필드 매핑에 연결 |
| 병원 고유 특수 요구사항 | 그 Site에 한해 Agent 추가 작성·검토 |
| 계약서·연구비 등 민감한 조건 | 승인된 조항/구조화 데이터 적용 후 담당자·법무 검토 |
| MV/SIV Report | 공통 정보만 재사용, 실제 방문 사실은 현장 입력 필수 |

- **공통 원본을 Word 파일 하나로만 관리하지 않는다.** 먼저 구조화된 Master Content를 만들고 필요에 따라 DOCX로 렌더링한다.
- 20개 Site × 3종 문서라면 60개 독립 대화를 시작하지 않는다. **1개의 Batch Job이 여러 Batch Item을 관리**한다.
- Site별 입력 누락·서식 충돌·파일 오류가 있으면 해당 Item만 `blocked`/`failed` 처리, 나머지는 계속 수행.
- Batch Item별 멱등성 키, 서식/참고 버전 스냅샷, 결과 Draft ID, 재시도 기록, 검토 상태 필요.
- 대량 생성의 병렬 처리량·LLM 요청량을 제한하고 비용/토큰 사용을 추적한다.
- 최종적으로는 Site별 개별 검토·승인, 필요하면 ZIP 묶음 다운로드.

### 7.5 Think Session vs Hospital Knowledge

**핵심 설계 결정: `세션 상태`와 `장기 공유 지식`은 분리한다.**

- 사용자 × 병원만으로 세션을 나누면 **같은 병원의 서로 다른 Study 정보가 섞일 수 있다.**
- 우선적으로 **문서 작업 단위(인증된 user / Study-Site / document / job)**의 격리된 Session 또는 Durable Object 인스턴스를 검토한다.
- Think의 Durable Object 상태/Workspace는 **대화 및 임시 작업 상태**에 활용한다.
- Study/Site 문서와 최종 Draft/버전의 원본은 Supabase 등에 영구 저장한다.
- **병원 공통 지식**은 별도 `Hospital Knowledge Base`의 구조화된 항목으로 저장하고, 각 Agent가 제한된 Tool을 통해 조회한다.

```text
Session 1: CRA A / Study X / 안암병원 / 초기심의 Draft
Session 2: CRA B / Study Y / 안암병원 / 변경심의 Draft
    └─ 두 세션은 검증된 안암병원 공식 지침을 참조할 수 있음
    └─ 그러나 두 Study의 비공개 문서·개인 메모리는 자동 공유되지 않음
```

**권한 주의:** Think `name`이나 Session ID는 보안 경계 자체가 아니다. Worker의 연결 시점과 Tool 호출 시점 모두 인증·세션 소유권·Study/Site 접근 권한을 검증한다.

### 7.6 Hospital Knowledge Base — 장기 차별화 포인트

목표는 단순한 '과거 결과를 학습한 AI'가 아니다. **출처와 적용 범위를 추적할 수 있는 병원별 작성 지식**을 축적하는 것이다.

- 공식 서식·버전·필수 제출 목록·명시적 작성 지침.
- 특정 심의 단계에 자주 필요한 확인사항.
- 담당자가 검증한 과거 심의의견 및 수정 사례.
- 검증된 체크리스트·문서 간 불일치 규칙.

권장 메타데이터: `hospital_id`, `knowledge_type`, `source_type`, `source_ref/version`, `review_stage`, `effective_date`, `verification_status`, `visibility_scope`, `reviewed_by`, `last_verified_at`.

**원칙:**

- 공식 규정/지침과 **단일 과거 사례**를 같은 근거 수준으로 취급하지 않는다.
- 과거 한 번의 보완 요청이 모든 Study에 적용되는 병원 규칙이라는 보장은 없다.
- CRO/Sponsor의 비공개 데이터에서 추출한 사례를 타 조직에 자동 공유하지 않는다. 법적 근거·권한·비식별화·담당자 검증 후에만 허용 범위를 넓힌다.
- 성과 지표: 최초 제출 후 보완 요청률, 승인까지 소요 시간, 제출 전 검토 적중률, **오경고율**, 담당자 검토 시간 등으로 실제 효과를 확인.

### 7.7 TypeScript Agent와 Python 문서 처리 엔진의 공존

- Think는 TypeScript로 **계획/추론/작업 상태/Tool routing/사용자 승인** 담당.
- 서식 채우기·원본 파일 파싱·정밀 DOCX 렌더링은 **기존 Python FastAPI docgen의 검증된 로직을 재사용**할 수 있다.
- 향후 필요한 경우 Document Engine만 독립 API 또는 라이브러리로 분리.
- 과거 실습 포트폴리오의 코드는 참조하되, 인프라 권한·민감 문서 접근·Workspace 저장 수명·API 안정성은 새로 설계.

---

## 8. 서비스 간 API 계약 초안 (실제 구현된 API라고 가정하지 말 것)

### 8.1 방향

```text
Next.js (Supabase Auth)
  → 사용자 인증/인가된 서버 경로 또는 Worker 연결 수립
  → Clinical Document Agent (Think)
  → Scoped Tool API (IRB Watch FastAPI / future Workspace API)
  → Supabase (RLS/서버 검증/Storage)
```

- Agent에 광범위한 Supabase `service_role`을 단순 주입하지 않는다.
- 서버 간 Tool API는 사용자/조직/프로젝트/사이트/문서 및 **허용된 작업 범위**를 검증한다.
- 파일 다운로드는 단기 만료된 접근 범위 제한 URL 또는 서버 프록시를 사용한다.
- 요청별 idempotency, audit trail, 모델/도구 사용량, 데이터 외부 전송 동의를 기록한다.

### 8.2 권장 Tool 명세 — 개념안

| Tool | 입력 | 반환/권한 |
|---|---|---|
| `get_project_context` | Study/Study-Site ID | 권한 있는 메타데이터만 |
| `search_irb_documents` | Hospital, 문서 종류, 검색어 | 접근 가능 문서 목록만 |
| `get_document_version` | Document ID + Version ID | 변경되지 않은 정확한 버전 |
| `get_form_template` | Hospital, Workflow, Version | 공식 서식 + 필드 정의 |
| `get_reference_files` | 선택된 reference version IDs | 허용된 파일/추출 텍스트 |
| `get_hospital_knowledge` | Hospital, 심의 단계 | 검증/권한 범위에 맞는 지식 |
| `save_draft_version` | Draft ID, expected version, 생성 결과 | 기존 Draft 덮어쓰기 금지, 새 버전 저장 |
| `save_review_responses` | 코멘트 ID별 대응 내용 | 승인 전 Draft 상태로 저장 |
| `get_calendar_context` | Study-Site, 기간 | 관련 심의·마감 일정 조회(필요한 경우) |

### 8.3 작성 요청 예시 — 논리적 스키마

```json
{
  "study_id": "study-uuid",
  "study_site_id": "site-uuid",
  "workflow_type": "irb.initial_review",
  "template_version_id": "template-version-uuid",
  "reference_document_version_ids": ["protocol-version-uuid", "budget-version-uuid"],
  "instructions": "출처 없는 수치나 사실은 만들지 말고 확인 필요로 표시",
  "idempotency_key": "client-generated-unique-key"
}
```

### 8.4 결과 예시 — 논리적 스키마

```json
{
  "job_id": "job-uuid",
  "status": "review_required",
  "draft_id": "draft-uuid",
  "draft_version_id": "draft-version-uuid",
  "source_version_ids": ["protocol-version-uuid", "budget-version-uuid"],
  "validation": {
    "missing_fields": ["site_contact_phone"],
    "conflicts": [],
    "manual_review_required": true
  }
}
```

**주의:** 위 JSON은 계획을 설명하기 위한 예시다. 실제 DTO/OpenAPI, 에러 모델, 인증, 이벤트 스트림·작업 상태 계약은 Claude가 기존 소스와 대조한 후 **제안·승인**을 받아 확정한다.

### 8.5 세션 연결과 Job 실행은 분리

- 대화/부분 수정: 인증된 Think 세션 + 스트리밍.
- 수십 건 생성: Batch Job/Workflow + 항목별 진행·부분 실패·재처리 상태.
- 브라우저 종료 또는 Agent 세션 재시작이 영구 Draft 손실을 의미해서는 안 된다.
- IRB Watch는 처리 상태를 조회하고 링크를 제공하되 **Agent 내부 실행 상태 자체를 소유할 필요는 없다**.

---

## 9. 데이터 보안, 무결성 및 규제 관련 필수 원칙

1. **조직 간 격리:** Study, Site, 문서, 보완의견, 지식, 스토리지 파일 경로 모두 권한 검증. 조직 관리자·PM·CRA 등 현재 `project_access_level` 로직을 기준으로 삼을 것.
2. **멤버 전용 원본의 별도 권한:** 프로젝트 접근 권한이 있다고 해서 모든 비공개 병원 게시판 문서까지 자동 접근 가능한 것은 아님. 기존 `source_members` 정책과 충돌하지 않도록 한다.
3. **최소 권한:** service role 키는 서버에서만 사용하고 Agent에는 scoped API 계약만 제공. 브라우저·Agent 프롬프트·파일에 비밀값을 노출하지 않는다.
4. **외부 AI 전송:** 기관/의뢰자/Sponsor의 규정과 사용자 동의 범위 안에서만 원본 텍스트를 모델에 전달. 비공개 원문/개인정보/PHI 포함 여부 검토.
5. **근거 추적:** Draft 항목별 참고 원본/버전/페이지/문단·변경 이력을 가능하면 남기고, AI 추론과 검증된 사실을 분리.
6. **사람의 승인:** AI는 임상·규제·법률 판단의 최종 권한을 갖지 않는다. Draft와 공식 제출본, 작성/검토/승인/제출 상태를 구분한다.
7. **감사 기록:** 누가 어떤 자료에 접근해 어떤 모델/도구로 어떤 Draft를 생성·변경·다운로드했는지 기록. 필요에 따라 유지·삭제 정책을 설정한다.
8. **프롬프트 인젝션 방어:** 병원 웹페이지, PDF/DOCX 텍스트, IRB 코멘트를 **명령이 아닌 비신뢰 자료**로 처리. 파일 안에 포함된 Tool 실행 지시를 따르지 않는다.
9. **중복/재시도 방어:** Workflow, Calendar Sync, Draft 저장이 부분 성공/재시도되어도 이중 생성·이중 알림을 방지.
10. **실패의 명시성:** `unknown`, `needs_review`, `failed`, `source_stale`, `unauthorized` 등을 정상 '없음'으로 반환하지 않는다.

---

## 10. 구현 우선순위 및 담당 구분

> 단계 표시는 **제안 로드맵**이다. 자동 구현 지시가 아님.

### Track A — Claude Code가 IRB Watch에서 도울 작업

| 단계 | 우선순위 | 목표 | 완료 판정 |
|---|---|---|---|
| A0 | 즉시 | 현 코드·docs·migrations 상태 확인 및 변경 영향 분석 | 실제 구현/미구현 표, 충돌 목록 작성 |
| A1 | 높음 | 기존 Pipeline/`/work`/알림과의 정합성 확인 | 날짜 Source of Truth 결정, 데이터 소유자 확정 |
| A2 | 높음 | 통합 캘린더 MVP 스키마/API/UI 설계·구현 | 여러 Study/Site 일정 목록·필터·권한 테스트 통과 |
| A3 | 높음 | 공식 병원 일정과 Study-Site 업무 마감 분리 | 근거/확정 상태/수정 이력 포함 |
| A4 | 중간 | 이메일 마감 알림·기존 `/work` 연계 | 대상자/중복/오류 동작 검증 |
| A5 | 중간 | 외부 Agent 읽기 도구용 API 계약 설계 | 권한/버전/감사·미존재 응답 검증 |
| A6 | 선택 | IRB Watch → Agent 진입점, Draft 상태 조회 | 작성 기능을 내부에서 다시 구현하지 않음 |
| A7 | 후순위 | Google Calendar 단방향 OAuth/동기화 | 연결·갱신·삭제·해제·중복 방지 테스트 |
| A8 | 장기 | 구형 docgen 전환/기능 종료 | 외부 대체 검증+마이그레이션+소유자 승인 이후 |

**중요:** `docs/pipeline-design.md`에 이미 정해진 A2/B1/B2 기능을 반복 설계하지 말 것. 새로운 캘린더 작업은 현재 진행 중인 B2 사이트/PI 모델과 병행 가능 여부를 먼저 판단한다.

### Track B — 소유자가 직접 개발하는 Cloudflare Think 프로젝트

| 단계 | 목표 | Claude Code의 역할 |
|---|---|---|
| B0 | TypeScript Worker 전용 프로젝트/Think scaffold | 요청 시 리뷰만 |
| B1 | Study/Site/템플릿 문서 조회용 Tool | IRB Watch API 계약 논의 |
| B2 | 단일 IRB Draft 생성·버전 저장 | 데이터 계약·사용 사례 리뷰 |
| B3 | 보완 코멘트 반영·대응 매트릭스 | 요구사항 검토 |
| B4 | Master Content + Site별 템플릿 매핑 | Schema/validation 의견 |
| B5 | Batch Workflows/병렬·재시도·승인 | 인터페이스·보안 검토 |
| B6 | SIV/MV 등 Skills 확대 | 도메인 요구사항 검토 |
| B7 | Hospital Knowledge/사전 제출 검증 | 지식 승인 정책 논의 |

이 Track의 **코드 작성/배포 책임은 사용자 본인**에게 있다. 두 저장소를 자동으로 결합하거나 새 Worker를 생성하지 않는다.

---

## 11. 기존 코드 변경 시 특히 조심할 사항

- `CLAUDE.md`의 **코드 변경 방식, live test/commit/migration 승인 규칙을 우선**한다.
- 이미 적용된 Supabase migration은 편집하지 않고 신규 migration을 작성한다. 먼저 `--dry-run` 검증.
- `irb-watch`가 DB schema의 기준 저장소다. `irb-watch-frontend`에 임의로 따로 schema migration을 누적하지 않는다.
- RLS, 조직별 공유, 플랫폼 admin read-only, `source_members`, Project PM 권한 등 현재 정책을 무력화하지 않는다.
- 크롤링·스케줄러의 오류 모델(실패를 '변경 없음'으로 처리하지 않음)을 유지.
- 공식 문서 수집과 AI 요약은 계속 별개다. 문서 작성 Agent 이관으로 크롤링이나 사용자 Assistant가 중단되면 안 된다.
- 기존 Draft가 사용자 또는 파이프라인 단계에 묶여 있다면 변경 전 데이터 소유권/공유/다운로드 이력 검토.
- 기존 Python `docgen/form_fields.py`, `docgen/form_fill.py`, `services/docgen.py`, Draft Jobs의 재사용 범위를 명확히 분석. **코드 삭제보다 API 경계 분리**를 선호.
- UI에서는 `Draft 생성 완료`, `검토 완료`, `서류 제출 완료`, `IRB 승인`을 하나의 상태로 합치지 않는다.
- 개인정보·PHI·의뢰자 기밀·계약 조건을 실제 시험 데이터로 테스트하지 않는다. 익명/가상 fixture 사용.
- 새 날짜 기능에서 공식 공지 문구를 파싱해 임의로 확정된 캘린더 일정을 만들지 않는다.
- 다국어 UI는 프론트엔드의 기존 `next-intl` 구조 유지.

---

## 12. 아키텍처 검증 시나리오(수용 기준 제안)

### Case 1 — 여러 프로젝트의 캘린더

- CRA A는 Study X에서 Site 01~15, Study Y에서 Site 01~08을 맡는다.
- 전체 캘린더에서 **접근 권한이 있는 모든 Study-Site의 일정**을 확인한다.
- Study별로 필터링하거나 병원·마감일·심의 개최일만 볼 수 있다.
- 다른 조직의 Study 일정은 조회되지 않는다.
- 프로젝트 공유/이관/권한 회수 후 캘린더 권한도 즉시 반영된다.

### Case 2 — 병원 공식 일정과 개별 업무 일정

- 동일 병원의 정규심의일은 Study X/Y에서 함께 참조하지만, 각 Study의 제출 목표·보완기한은 별개.
- 공식 일정 변경은 원본 출처와 검증 상태를 표시하며, 확인되지 않은 변경은 바로 확정하지 않는다.

### Case 3 — 문서 생성 전환

- IRB Watch 문서 검색 Assistant는 '안암병원 최신 초기심의 양식을 찾아줘'에 답한다.
- '이 양식과 Protocol 2.1로 초안을 작성해줘'는 독립 Agent로 이동한다.
- 인증되지 않은 사용자는 원문/참고 문서를 읽지 못한다.
- 생성된 Draft의 버전·상태는 IRB Watch가 필요한 범위에서 확인할 수 있다.

### Case 4 — 동일 병원, 다른 Study

- 같은 병원에서 Study A와 B의 세션은 독립.
- 공식 공개 지침은 공유 가능, Study A의 비공개 Protocol은 Study B에 보이지 않는다.

### Case 5 — 15개 Site의 일괄 문서 생성

- Study 공통 정보는 한 번 구조화한다.
- 8개 공통서식 Site, 5개 전용서식 Site, 2개 특수 요구사항 Site로 분배할 수 있다.
- 1개 Site가 필수 정보 누락으로 실패해도 나머지 결과는 유지된다.
- 동일 Batch를 재시도해도 기존 파일이 중복 저장되지 않는다.
- 생성본은 Site별 검토 필요 상태이고 제출 완료로 간주하지 않는다.

### Case 6 — 심의 보완의견

- IRB 코멘트가 기존 제출본의 관련 문서/항목에 매핑된다.
- 변경 전후 Diff, 근거, AI 추정 여부, 사람 승인 여부를 확인할 수 있다.
- 단일 보완 사례가 자동으로 전사·전 병원 규칙이 되지 않는다.

### Case 7 — Google Calendar

- 연결 전에는 Google 이벤트가 생성되지 않는다.
- 연결 후 동기화, 시간 수정, 일정 취소, 연결 해제 시 예상된 동작.
- API 오류·토큰 만료로 동기화되지 않으면 **IRB Watch 원본 일정은 유지되고 사용자에게 실패 사유가 보인다.**

---

## 13. 아직 결정하지 않은 사항 (Claude가 제안하고 소유자에게 확인할 것)

다음은 **미결정**이며 임의로 확정하지 않는다.

1. 현재 `projects`/`project_sites`/`pipelines` 구조를 향후 일반 `studies`/`study_sites`로 이름 변경할지, 호환성 유지할지.
2. 프로젝트의 문서·Draft 소유권이 궁극적으로 IRB Watch, 공통 Workspace API, 별도 서비스 중 어디에 속할지.
3. 공식 병원 심의 회차의 소스(게시판 자동 수집, 관리자 입력, 사용자 확인)와 정합성 검증 절차.
4. 심의 일정이 여러 게시판·병원 분원·심의 종류와 1:N일 때의 모델.
5. 주말/공휴일/기관별 마감시간·예외공지를 처리할 상세 정책.
6. 알림 수신자(CRA/PM/owner 등)와 알림 임계값·반복 간격, 프로젝트 공유 시 기본값.
7. Google Calendar 전용 캘린더 생성 여부, 사용자가 삭제한 이벤트 처리 방식, 토큰 저장/철회 정책.
8. 각 문서형식(HWP/HWPX/DOCX/PDF)에서 **원본 서식의 충실한 렌더링 가능 수준**.
9. 회사/Sponsor 내부 SOP와 공식 IRB 서식 충돌 시 템플릿 우선순위.
10. 조직 간 Hospital Knowledge 공유 허용·비식별화·라이선스·보존·동의 정책.
11. Agent Runtime을 Think로 시작한 후 변경할 필요가 생길 때의 호환 API 범위.
12. Agent가 직접 호출할 백엔드 API의 인증 형태(위임 토큰·서버간 단기 토큰 등).
13. 기존 `docgen`의 어느 부분을 언제 API로 노출하고 언제 폐기할지.
14. Clinical Workspace 독립 프론트엔드를 만드는 시기와 서비스 경계.

미결정 문제는 **기존 저장소에서 확인할 수 있는 것은 먼저 확인**하고, 실질적 트레이드오프가 남은 경우에만 권장안과 함께 소유자에게 제시한다.

---

## 14. Claude Code가 바로 실행할 수 있는 '첫 분석' 요청문

아래 요청은 코드를 수정시키기 위한 명령이 아니라, **현 상태 분석 및 설계 정렬을 위한 시작 프롬프트**다.

> 이 저장소의 `CLAUDE.md`, `docs/plan.md`, `docs/decisions.md`, `docs/pipeline-design.md`, 실제 코드 및 Supabase migration을 먼저 검토해 주세요. 또한 `docs/IRB_Watch_Clinical_Document_Agent_Claude_Handoff_2026-11-14.md`를 새로운 제품 방향 문서로 읽어주세요.  
> 기존 IRB Watch에서 문서 작성 AI를 장기적으로 독립 Cloudflare Think Agent로 이관하고, IRB Watch는 문서 수집·버전 관리·읽기 전용 Assistant·Study/Site별 IRB Pipeline·통합 캘린더 및 마감 알림에 집중하려고 합니다.  
> **Think Worker 코드는 제가 직접 작성할 예정이므로 이 저장소에서 구현하지 마세요.** 기존 `docgen`도 대체가 검증되기 전에는 삭제하지 마세요.  
> 우선 (1) 현재 구현/미구현 및 최신 B2 진행상황, (2) 기존 날짜/알림/권한 데이터 모델과 통합 캘린더의 중복·충돌, (3) 가장 작은 안전한 마이그레이션·API·UI 작업 순서, (4) 외부 Agent Tool API에서 재사용 가능한 코드, (5) 결정이 필요한 질문을 정리해 주세요.  
> 계획을 승인받기 전에는 코드 수정·원격 migration·실데이터 변경·배포·커밋하지 마세요.

---

## 15. 공식 참고 자료 및 확인해야 할 문서

### 프로젝트

- IRB Watch Backend — https://github.com/pjh5474/irb-watch
- IRB Watch Frontend — https://github.com/pjh5474/irb-watch-frontend
- CRA Assistant Agent(기존 실습) — https://github.com/pjh5474/cra-assistant-agent
- IRB Watch 기존 Claude 지침 — https://github.com/pjh5474/irb-watch/blob/main/CLAUDE.md
- IRB Watch 기존 계획 — https://github.com/pjh5474/irb-watch/blob/main/docs/plan.md
- IRB Watch 기존 결정 — https://github.com/pjh5474/irb-watch/blob/main/docs/decisions.md
- IRB Pipeline 재설계 — https://github.com/pjh5474/irb-watch/blob/main/docs/pipeline-design.md

### Cloudflare / Google

- Think — https://developers.cloudflare.com/agents/harnesses/think/
- Think getting started / React 연결 — https://developers.cloudflare.com/agents/harnesses/think/getting-started/
- Think tools / Workspace — https://developers.cloudflare.com/agents/harnesses/think/tools/
- Think Workspace의 `@cloudflare/computer` 전환(2026-10-20) — https://developers.cloudflare.com/changelog/post/2026-10-20-think-computer-workspace/
- Google Calendar 이벤트 생성 — https://developers.google.com/workspace/calendar/api/guides/create-events
- Google Calendar 증분 동기화 — https://developers.google.com/workspace/calendar/api/guides/sync

---

## 16. 한 문장으로 정리한 설계 원칙

> **IRB Watch는 '병원 IRB 규제정보와 프로젝트 일정·진행상태를 관리하는 운영 서비스'로, Clinical Document Agent는 'Study·Site 맥락과 검증된 템플릿·참고 문서를 사용해 CRA 문서를 작성·수정·일괄 생성하는 독립 실행 엔진'으로 분리한다. 작업 세션과 장기 지식은 분리하고, 모든 문서·일정은 버전·출처·권한·사람의 최종 검토를 중심으로 설계한다.**

