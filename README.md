# SEAP Monitor

> **⚠ Portfolio showcase — viewing only. All rights reserved.**
>
> This repository is published as a public case study of the author's work. The source code is provided **for viewing through the GitHub interface only**.
>
> The following are **prohibited** without prior written permission from the copyright holder:
> - Cloning, downloading, copying, forking, or mirroring the repository
> - Modifying the code or creating derivative works — **regardless of the extent of modification**
> - Refactoring, renaming, restructuring, translating to another language, or extracting partial components
> - Distributing, redistributing, sublicensing, selling, or offering for sale, including any modified version
> - Using the code, in whole or in part, in any commercial product, service, or internal business operation
> - Using the code to train, fine-tune, or prompt machine learning models or AI systems
> - Re-licensing under any other license
>
> A work that is substantially similar in structure, organization, naming, or expression to this code is a derivative work and remains covered by this license, **even if substantially rewritten or translated to another language**.
>
> See [LICENSE](LICENSE) for full terms. For licensing inquiries or custom development: **formatia.melodic.fagaras@gmail.com**

AI-assisted bid-management platform for Romanian public procurement (SEAP / e-licitatie.ro). A three-stage pipeline that scans the public SEAP API for relevant tenders, extracts and analyzes the attached documentation with Claude, then drafts bid documents from reusable templates and a company profile.

## Why this exists

Companies bidding on Romanian public procurement spend hours every day refreshing the SEAP portal, reading multi-hundred-page PDF attachments, and copy-pasting the same boilerplate into every administrative form. The decision of whether a tender is even worth pursuing is buried inside a 70-page caiet de sarcini that often arrives only in scanned form.

SEAP Monitor collapses that into a single workflow:

1. **Monitor** — a background scanner polls the SEAP public API on a configurable interval, filters by CPV codes / authority IDs / keywords, downloads every attachment, and emails the team a digest of new matches.
2. **Analyzer** — when a notice looks interesting, the analyzer extracts text from every PDF/DOCX/ZIP attachment and asks Claude to score the match against the company profile and to surface the eligibility criteria, technical requirements, financial requirements, and deadlines.
3. **Generator** — administrative documents (declaratii, formulare, scrisori de inaintare) are generated from approved templates by merging the company profile and the AI-extracted notice metadata, then packaged as a ZIP ready for upload.

## Features

- **Background scanner** — configurable interval (default 6h), survives restarts, throttles SEAP API calls with a semaphore + 2s spacing, retries with backoff on rate-limit
- **Dual scan strategy** — filter by CPV code list or by monitored contracting authority IDs; "anunturi de participare" + "anunturi de atribuire" both supported via the SEAP API
- **Notice deduplication** — `seap_id` unique constraint guarantees each notice is stored exactly once even if it appears in multiple scans
- **CPV digest email** — on each successful scan, a digest of newly matched notices is sent via Microsoft Graph API (OAuth2 client credentials flow) to configurable recipients
- **Multi-format attachment extraction** — `pdfplumber` for PDFs (incl. scanned with OCR fallback), `python-docx` for DOCX, in-archive extraction for ZIP bundles
- **Claude-driven analysis** — JSON-structured extraction of eligibility criteria, technical specs, financial value, deadlines, and a 0–100 match score against the company profile
- **Reusable bid templates** — administrative / technical / financial / other categories, each with AI-detected fill-in fields rendered as a structured form
- **Company profile** — single source of truth for all generated documents (legal name, CUI, address, bank accounts, representatives, certifications)
- **Generated bid packages** — multiple documents grouped per notice, exported as ZIP for SEAP upload
- **File-based session auth** — bcrypt-hashed credentials, persisted sessions survive container restart, rate-limited login (3 attempts / minute / IP)
- **HTMX-driven UI** — server-rendered Jinja2 templates with HTMX partials; no SPA build pipeline, fast first paint, works on slow connections

## Architecture

```
┌──────────────────────────────────────┐      ┌──────────────────────────┐
│       FastAPI (async)                │◄────►│  SEAP public API         │
│       Jinja2 + HTMX + Tailwind       │      │  e-licitatie.ro/api-pub  │
│                                      │      └──────────────────────────┘
│   ┌─ Monitor ─────────────────┐      │      ┌──────────────────────────┐
│   │  Scanner + email digest   │      │◄────►│  Anthropic Claude API    │
│   ├─ Analyzer ────────────────┤      │      │  (analysis + generation) │
│   │  Extractor + Claude calls │      │      └──────────────────────────┘
│   └─ Generator ───────────────┘      │      ┌──────────────────────────┐
│      Templates + ZIP packages        │◄────►│  Microsoft Graph API     │
└──────────────────────────────────────┘      │  (mail send via OAuth2)  │
            ▲             ▲                   └──────────────────────────┘
            │             │
            ▼             ▼
┌───────────────────┐   ┌───────────────────┐
│  PostgreSQL / SQL │   │  Redis + Celery   │
│  (notices, bids,  │   │  (scheduled scans │
│  templates, …)    │   │  + heavy AI jobs) │
└───────────────────┘   └───────────────────┘
```

- **Backend:** FastAPI + SQLAlchemy 2.0 async + asyncpg / aiosqlite. PostgreSQL in production, SQLite for local dev.
- **Task queue:** Celery on Redis. The lightweight scan scheduler runs in-process as an asyncio task on app startup; heavy attachment-extraction and AI calls are offloaded to Celery workers.
- **AI:** Anthropic SDK. Used for two distinct prompts — notice analysis (structured extraction + match scoring) and template-field analysis (auto-detect placeholders in user-uploaded templates).
- **Document processing:** `pdfplumber` for PDF text, `python-docx` for DOCX read/write, `reportlab` for generated PDFs.
- **Email:** Microsoft Graph API via `aiohttp` with OAuth2 client-credentials flow. No SMTP required.
- **Frontend:** Server-rendered Jinja2 templates + Tailwind (via CDN) + HTMX. Sidebar navigation, mobile-responsive.

## Quick start (Docker)

```bash
git clone <your-fork-url>
cd seap-monitor
cp .env.example .env
# edit .env: set ADMIN_PASSWORD, SESSION_SECRET, ANTHROPIC_API_KEY at minimum
docker-compose up --build
```

Then open <http://localhost:8050> and log in with the credentials from `.env`.

On first start, run the migrations:

```bash
docker-compose exec app alembic upgrade head
```

## Quick start (manual)

**Backend** (Python 3.11+):

```bash
cd seap-monitor
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env

alembic upgrade head
uvicorn app.main:app --reload --port 8050
```

Redis is only needed if you want Celery workers — the in-process scheduler runs without it. Anthropic API key is only needed for the Analyzer + Generator modules; Monitor works standalone.

## Project layout

```
app/
  main.py            FastAPI app + in-process scan scheduler + auth routes
  auth.py            File-based sessions, bcrypt, rate-limited login
  config.py          Pydantic settings (env-driven)
  database.py        SQLAlchemy async engine + session factory
  models.py          ORM: CPVCode, Notice, NoticeDocument, ScanLog,
                     NoticeAnalysis, BidTemplate, CompanyProfile,
                     GeneratedBid, BidPackage
  celery_app.py      Celery broker config

  modules/
    monitor/
      seap_client.py    Async SEAP API client (rate-limited, retry on 429)
      scanner.py        Scan orchestration + CPV match digest email
      routes.py         /monitor/* HTMX views (notices list, settings, detail)
      tasks.py          Celery tasks (download attachments, etc.)

    analyzer/
      extractor.py      PDF/DOCX/ZIP text extraction
      ai_analyzer.py    Claude prompts for structured analysis + scoring
      routes.py         /analyzer/* (analysis view, compare)
      tasks.py          Celery: extract + analyze jobs

    generator/
      template_mgr.py   Upload + AI-detect fields in bid templates
      doc_generator.py  Render bid documents from template + profile
      routes.py         /generator/* (templates, company, generate, preview)
      tasks.py          Celery: bulk-generate documents

  services/
    email_service.py    Microsoft Graph mail send (OAuth2 client credentials)
    file_service.py     Storage helpers

  static/  +  templates/   Tailwind CSS + Jinja2 + HTMX

alembic/             Schema migrations
scripts/init_db.py   First-run DB bootstrap helper
```

## SEAP API notes

The public SEAP API at `e-licitatie.ro/api-pub` is undocumented but stable. The client in [`app/modules/monitor/seap_client.py`](app/modules/monitor/seap_client.py) handles:

- **Two distinct endpoints** — `GetCNoticeList` for open tenders, `GetCANoticeList` for award notices, each returning a different shape
- **Six notice-type IDs for participation** (CN, SCN, RFQ, PC, DC, LR) and six for awards
- **Rate limiting** — one in-flight request at a time via `asyncio.Semaphore(1)` + a configurable inter-request delay (default 2.0s) + exponential backoff on HTTP 429
- **Pagination** — cursor-style, follows `next` until the API stops returning results or the publication-date window is exhausted

CPV codes are matched against the `cpv_code` field on each returned notice. Authorities are tracked by their `seap_id` and matched against the `authority_seap_id` field, since the human-readable name format drifts.

## Pricing snapshot — n/a

Unlike other apps in the portfolio, this one doesn't lock prices into a quote because there are no prices: SEAP notices contain the *estimated value* set by the contracting authority, and bids are submitted as separate documents containing the offered price. The snapshot pattern here applies instead to the **AI analysis JSON**: once Claude has scored a notice, the result is frozen into `NoticeAnalysis.ai_analysis` so the team sees the same eligibility decision the AI made at extraction time, regardless of later prompt changes.

## License

Proprietary. All rights reserved. See [LICENSE](LICENSE).

For commercial licensing, custom development, or any use beyond GitHub-web viewing: **formatia.melodic.fagaras@gmail.com**

## Status

Portfolio-grade demo. Production deployments require:

- PostgreSQL (the project ships with SQLite fallback for local dev)
- A registered Microsoft 365 app with `Mail.Send` application permission (for the digest email)
- An Anthropic API key (for the analysis + generation modules)
- A reverse proxy with TLS (the app expects `secure=True` session cookies)
- Persistent volume for `STORAGE_PATH` (attachments + templates + generated bid ZIPs)
