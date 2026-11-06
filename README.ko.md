# IRB Watch

[English](README.md) | **한국어**

임상연구코디네이터를 위해 병원 IRB(임상연구윤리심의위원회, 연구윤리 부서) 게시판을 감시하는 서버입니다. 요청이 있을 때 게시판을 읽어 어떤 문서가 새로 올라왔는지, 바뀌었는지, 사라졌는지를 알아내고, 모든 버전을 원본 파일과 함께 보관하며, AI 요약과 우선순위가 붙은 뉴스 카드를 기록합니다. 코디네이터가 병원마다 사이트를 열어 버전을 일일이 비교하지 않아도 됩니다.

이 저장소에는 **서버**와 **데이터베이스 스키마**가 들어 있습니다. 웹 대시보드는 별도의 Next.js 프로젝트이며, 같은 Supabase 데이터베이스를 행 수준 보안(RLS) 아래에서 직접 읽습니다.

> 현재 상태: 스캔, 이력 관리, 요약, 관리자 API는 동작하며 실제 병원 사이트로 검증했습니다. 스캔은 아직 수동으로 시작하고, 매일 자동으로 도는 스케줄러와 대시보드는 아직 없습니다. [docs/plan.md](docs/plan.md)를 보세요.

## 동작 방식

```
관리자가 게시판 URL 등록 ──► 스캔 (지금은 수동)
                                │
   게시판 목록과 모든 게시글 페이지를 읽고 첨부파일을 내려받음
                                │
   원본 파일 ──► 비공개 Supabase Storage 버킷 (파일 해시당 한 번만 저장)
   본문과 파일 텍스트 (PDF, HWP, HWPX, DOCX) ──► 추출 · 정규화 · 해시
                                │
   저장된 상태와 비교 ──► 신규 / 수정 / 삭제(2번 연속 안 보일 때) / 재등장
                                │
   Jev (분류): 우선순위, 실질적 변경인지 단순 수정인지, 카테고리
   OpenAI (요약): 1~3문장, 문서와 같은 언어
                                │
   모든 버전을 전체 텍스트와 함께 저장 ──► 뉴스 카드를 감사 로그에 기록
```

- **버전 이력.** 변경이 있을 때마다 새 버전이 만들어지고, 본문 전체와 각 파일에서 추출한 텍스트가 함께 저장됩니다. 그래서 어떤 두 버전이든 (버전 번호나 날짜로 골라) 단어 단위 차이로 비교할 수 있습니다.
- **게시판의 첫 스캔은 조용한 기준선입니다.** 현재 게시글을 저장하기만 하고 카드는 만들지 않습니다.
- **실패에는 이유가 보입니다.** 게시글을 읽을 수 없거나 첨부파일이 손상됐거나 암호가 걸려 있으면 문서에 `needs_review` 표시와 이유가 붙고, 구독자는 이유를 설명하는 카드를 봅니다. 실패한 스캔은 아무것도 바꾸지 않으며 "변경 없음"처럼 보이지 않습니다.
- **AI는 선택 사항이고 비용을 아끼도록 설계했습니다.** 제목만 바뀌었거나 명백히 단순한 수정은 OpenAI를 부르지 않고 정해진 문장으로 설명합니다. 문서 전체가 아니라 바뀐 부분만 외부로 보내며, OpenAI 쪽에는 아무것도 저장하지 않습니다. 키가 없거나 서비스가 멈춰도 스캔은 계속 동작합니다.
- **사용자가 문서 전체의 요약을 요청할 수 있습니다** (`POST /documents/{id}/summary`). 첫 요청 이후에는 저장된 결과를 돌려주고, 사용자별 하루 요청 횟수에 제한이 있습니다.
- **접근 권한.** 계정은 관리자만 만들 수 있습니다. 로그인한 사용자는 구독한 데이터만 Supabase 행 수준 보안을 통해 읽고, 모든 쓰기는 이 서버를 거칩니다.

현재 지원하는 게시판: SNUH HRPP 게시판(`hrpp.snuh.org`). 새 사이트를 추가하려면 작은 어댑터가 필요합니다 ([docs/decisions.md](docs/decisions.md) 참고).

## 요구 사항

- [uv](https://docs.astral.sh/uv/) (고정된 Python 3.12를 대신 설치해 줍니다)
- Node.js (Supabase CLI를 `npx`로 실행합니다)
- Supabase 프로젝트 (데이터베이스, 인증, 스토리지)
- 요약용 OpenAI API 키와 모델 이름 (선택)
- 분류용 TypeSafe(Jev) API 키 (선택)

## 설치

명령어는 PowerShell 기준이며 저장소 루트에서 실행합니다.

```powershell
# 1. 의존성 설치
uv sync

# 2. 설정 파일 만들기. APP_ENV가 파일을 고릅니다: dev는 .env.dev, prod는 .env.prod를 읽습니다.
Copy-Item .env.example .env.dev
# .env.dev를 채우세요. 절대 커밋하지 마세요 (.gitignore가 .env.*를 이미 제외합니다).
```

변수 목록 (이름만):

| 변수 | 용도 |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | 모든 기능 (프로젝트의 service 또는 secret 키를 사용하며, 서버 안에서만 씁니다) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | AI 요약 (비우면 꺼짐, 기본 모델은 없음). `OPENAI_BASE_URL`은 OpenAI 호환 게이트웨이용이며, OpenAI에 직접 연결하려면 비워 두세요 |
| `TYPESAFE_API_KEY`, `TYPESAFE_MODEL` | Jev 분류 (키를 비우면 꺼짐, 기본 모델은 `jev-latest`) |
| `FRONTEND_ORIGINS` | API를 호출할 수 있는 브라우저 출처 (기본값 `http://localhost:3000`) |
| `AI_SKIP_BELOW`, `AI_MIN_CONFIDENCE`, `SUMMARY_REQUESTS_PER_DAY` | AI 규칙 조정 |

서버는 프로젝트의 공개 서명 키로 로그인 토큰을 검증하므로 JWT 시크릿이 필요 없습니다.

```powershell
# 3. 데이터베이스 스키마 만들기 (마이그레이션이 기준입니다)
npx supabase login
npx supabase link --project-ref <프로젝트-ref>
npx supabase db push --remote --dry-run      # 미리보기
npx supabase db push --remote --yes          # 적용
```

Supabase 대시보드에서 **Authentication → Allow new users to sign up**을 끄세요. 계정은 관리자가 만듭니다.

```powershell
# 4. 첫 관리자 만들기 (비밀번호를 물어봅니다. 직접 터미널에서 실행하세요)
uv run python -m irb_watch.scripts.create_first_admin --email you@example.com --display-name "Your Name"

# 5. 서버 실행
uv run uvicorn irb_watch.main:app --reload
# GET http://127.0.0.1:8000/health  ->  {"status":"ok"}
```

그다음 관리자 API로 게시판을 등록하고 스캔을 시작합니다 (관리자의 로그인 토큰이 필요합니다). 이름과 게시판 URL로 `POST /admin/sources`를 호출한 뒤 `POST /admin/sources/{id}/scan`을 호출하세요. 게시판 하나를 스캔하는 데 약 2~3분이 걸립니다. 모든 엔드포인트, 상태값, 명령어는 [docs/runbook.md](docs/runbook.md)에 있습니다.

### 테스트

```powershell
uv run pytest -q                  # 오프라인, 약 10초
uv run ruff check irb_watch tests
```

실제 Supabase 프로젝트와 실제 병원 사이트를 사용하는 라이브 테스트는 요청할 때만 실행되며, 임시 데이터를 만들고 끝나면 지웁니다 (감사 로그 행은 지울 수 없습니다):

```powershell
$env:RUN_LIVE=1; uv run pytest -m live -q
```

## 프로젝트 구조

```
irb_watch/
  main.py            FastAPI 앱, CORS, 오류 처리
  config.py          환경 파일에서 읽는 설정
  api/               라우트: health, 관리자 사용자, 관리자 소스, 문서 요약
  auth/              로그인 토큰 검증
  services/          업무 규칙: 사용자, 소스, 스캔, 요약
  core/              변경 감지, 차이 비교, 해시: 네트워크나 DB 없는 순수 로직
  crawlers/          예의 바른 수집기(robots.txt, 지연, 리디렉션 검사), SNUH 어댑터와 리더
  parsers/           PDF, HWP, HWPX, DOCX 텍스트 리더
  ai/                Jev 분류기, OpenAI 요약기, 건너뛰기 규칙, 템플릿
  db/                저장소의 Supabase 구현
  security/          URL 안전 검사(내부 주소 차단), 크기 제한
  scripts/           일회성 도구 (첫 관리자)
supabase/migrations/ 데이터베이스 스키마
tests/               인메모리 가짜 객체를 쓰는 오프라인 테스트, RUN_LIVE=1일 때만 라이브 테스트
docs/                계획, 이유가 적힌 결정 기록, 운영 가이드, 일일 보고서
```

수정하기 전에 알아 둘 설계 규칙:

- 업무 로직은 작은 인터페이스에 맞춰 작성하고, `db/`가 Supabase용으로 구현하며, 테스트는 인메모리 가짜 객체를 씁니다.
- `audit_logs` 테이블은 추가만 가능합니다. 카드는 기록한 뒤에는 수정할 수 없으므로 요약은 카드를 쓰기 전에 정합니다.
- 적용한 마이그레이션은 절대 수정하지 않고 새 마이그레이션을 추가합니다. 프로젝트의 기본 권한이 넉넉하므로, 새 테이블은 권한을 부여하기 전에 먼저 `revoke all` 해야 합니다.
- 페이지와 파일의 텍스트는 신뢰할 수 없는 입력입니다. 프롬프트에서 데이터로만 취급하고 크기를 제한하며, 어떤 모델 호출에도 외부에 영향을 주는 도구를 붙이지 않습니다.

각 선택의 이유는 [docs/decisions.md](docs/decisions.md)에, 날짜별 진행 상황은 [docs/reports/](docs/reports/)에 있습니다.

## 라이선스

모든 권리 보유. 아직 어떤 라이선스도 부여하지 않았습니다. 테스트 픽스처에는 병원 웹사이트의 공개 서식 두 개가 들어 있습니다.
