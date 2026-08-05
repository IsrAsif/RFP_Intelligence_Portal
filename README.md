# RFP Automation Portal

AI-powered RFP (Request for Proposal) analysis platform that ingests solicitation documents and produces a complete bid-preparation workspace: deliverables, evaluation criteria, compliance, risks, timeline, key requirements, a 37-item strategic checklist, Go/No-Go recommendation, requirements extraction & verification, a grounded Q&A copilot, smart content reuse, a Section L–M crosswalk, and amendment/version delta tracking.

> Note: although the codebase organizes itself into "Module 1 … Module 6", every module shares one Flask application (`app.py`), one SQLite database (`analyses.db`), and one set of frontend assets. Modules differ only in their feature scope, not their deployment.

---

## Table of Contents

1. [Feature Overview](#feature-overview)
2. [Module 1 — Document Ingestion & Analysis](#module-1--document-ingestion--analysis)
3. [Module 2 — Strategic Analysis & Compliance](#module-2--strategic-analysis--compliance)
4. [Module 3 — RFP Sentinel Copilot](#module-3--rfp-sentinel-copilot)
5. [Module 4 — Smart Content Reuse Engine](#module-4--smart-content-reuse-engine)
6. [Module 5 — Compliance Shred (Section L–M Crosswalk)](#module-5--compliance-shred-section-lm-crosswalk)
7. [Module 6 — Amendment & Version Delta Tracking](#module-6--amendment--version-delta-tracking)
8. [Cross-Module Features](#cross-module-features)
9. [Database & Storage](#database--storage)
10. [Checklist Evaluation Rules](#checklist-evaluation-rules)
11. [Tech Stack](#tech-stack)
12. [Setup](#setup)
13. [Environment Variables](#environment-variables)
14. [Usage — Routes](#usage--routes)
15. [Project Structure](#project-structure)
16. [Testing](#testing)

---

## Feature Overview

| Area | What it does |
|------|--------------|
| **Upload & parse** | PDF, DOCX, TXT (up to 50 MB) via drag & drop or file picker; `pypdf`/`python-docx` text extraction (image-only PDFs yield no text) |
| **Analysis** | Executive summary + 10-tab results: Deliverables, Evaluation, Compliance, Risks, Timeline, Key Requirements, Checklist, Go/No-Go, Extracted Requirements, Smart Reuse |
| **Compliance** | 37-item bid qualification checklist across Financial (5), Legal (12), Operations (15), Technical (5) |
| **Go/No-Go** | 0–100 score gauge, verdict badge (Go / Caution / No-Go), 4–8 weighted reasons |
| **Sentinel copilot** | Structured-first grounded Q&A per case, Groq fallback, citation tags, notes, actions, bid rehearsal, partner matching |
| **Smart Reuse** | Q&A extraction, tone adaptation, reusable answer library (embedding search) |
| **Compliance Shred** | Maps Section L–M clauses / checklist items onto your proposal outline (BM25 + embeddings) |
| **Amendments** | Tag an upload as an amendment of a prior case; structural diff, severity, Groq summary, Version History UI |
| **Persistence** | All analyses stored in SQLite — real history, compare, view, rename, export |
| **Export** | Full PDF report (complete analysis + requirements verification + compliance-shred crosswalk + amendment delta), deliverables-only PDF, requirements Excel, raw JSON (with delta for amendments) |

---

## Module 1 — Document Ingestion & Analysis

- **Drag & Drop Upload** — PDF, DOCX, TXT up to 50 MB; optional title; Gemini or OpenRouter provider toggle; "Amendment of case" dropdown to link an upload to a prior analysis (Module 6).
- **Document Parsing** — `pypdf` (PDF), `python-docx` (DOCX), raw text (TXT). Text extraction is `pypdf`-only — **no OCR is wired in**, so scanned/image-only PDFs without a text layer produce empty text (see [Limitations](#limitations)).
- **Truncation limits** — Gemini 200K chars, OpenRouter 60K chars, each guarded to stay inside model context windows.
- **Results** — Executive summary + 10 tabs (see overview). Every analysis is persisted to SQLite with a deterministic content hash, word count, provider, and timestamp.
- **Export toolbar** — Results page header has a professional toolbar:
  - **Export dropdown** → Documents: *Full PDF report*, *Deliverables-only PDF*; Data: *Requirements (Excel)*, *Raw JSON*
  - The **Full PDF report** is the complete deliverable: summary, deliverables, evaluation criteria, risks, timeline, key requirements, compliance matrix, 37-item checklist, **extracted requirements with Module 2 verification**, **checklist verification**, the full **Compliance Shred (Module 5)** content (clause-to-outline crosswalk *and* the strategic-checklist "Answered In" mapping), and — for amendment records — the **amendment delta**.
  - **Raw JSON** embeds the amendment `delta` for amendment records too, so every export is complete.
  - **Tools group** → *Extract* (re-run requirements extraction), *Requirements* (view/filter/export requirements), *Smart Reuse*, *Compliance Shred*, and **Sentinel** as the primary action
- **Analyzing Overlay** — Full-screen animated overlay with step-by-step progress during processing.
- **Error handling** — If analysis fails (provider outage, empty/truncated model output), uploads return a styled 502 error page instead of a silent partial result.

---

## Module 2 — Strategic Analysis & Compliance

- **Go / No-Go Analysis** — AI pursuit recommendation with score gauge (0–100), verdict badge (Go / Caution / No-Go), and 4–8 weighted factor breakdown (`factor`, `detail`, `weight`).
- **Bid Qualification Checklist** — Fixed **37 items** across 4 departments:
  - Financial (5), Legal (12), Operations (15), Technical (5).
  - Each item includes: `item`, `status`, `reasoning`, `what_rfp_says` (grounded quotes), `rfp_evidence` (section/clause/page references), `risk_level` (High/Medium/Low), `impact_on_bid_strategy`, and `mitigation_strategy`.
- **Status summary bar** — Color-coded counts for all 6 statuses (Go / No-Go / Escalate / Review / Caution / Not Specified in RFP).
- **Company Profile** (`/company_profile`) — Store company capabilities, certifications, NAICS codes, past performance, capacity, and facts. Used by requirements verification.
- **Requirements Verification** (`/verify_requirements/<id>` or "Verify" button) — Each extracted requirement is checked against the company profile using a **hybrid retrieval** scorer: 0.5 × BM25 (lexical) + 0.5 × embedding cosine (semantic), embeddings via Cloudflare Workers AI `@cf/baai/bge-base-en-v1.5`. Each requirement gets a verdict, matched evidence, and confidence.
- **Departmental Compliance** — Financial, Legal, Operations, and Technical requirements matrix in the Compliance tab.

---

## Module 3 — RFP Sentinel Copilot

- **Structured-first Q&A** (`/sentinel/<case_id>`) — Natural-language questions over existing case analyses. **No vector store, no embeddings, no intent router.**
- **Direct match engine** — Scans every field of the case JSON (`summary`, `deliverables`, `evaluation_criteria`, `compliance`, `risks`, `timeline`, `key_requirements`, `go_nogo.*`, `strategic_checklist.*`, `module2_verification.*`) and scores each via keyword-stem overlap (`jaccard` / `coverage`), positional bigram bonuses, and raw-word substring bonuses. Threshold 0.25; tie-breaker prefers shorter, more specific fields.
- **Groq fallback** — When no direct match reaches threshold, the case JSON (truncated to 12K chars) is sent to Groq `llama-3.3-70b-versatile` with `response_format=json_object` and a constrained "only from this data" system prompt.
- **Response format** — `{answer, cited_fields, confidence: "high"|"low", match_type: "direct"|"llm_fallback"}`.
- **UI** — Message bubbles, confidence-colored citation tags (green = high, amber = low), typing indicator, suggestion buttons, dark-mode compatible.
- **Bid Rehearsal** — Structured mock oral-presentation Q&A against the case.
- **Partner Matching** — Surface candidate partnership areas based on case requirements.
- **Notes & Actions** — Persist human notes and action items per case (API + UI).
- **Integrated access** — Sentinel link is the primary button in the results toolbar and is cross-linked from the Smart Reuse and Compliance Shred pages.

---

## Module 4 — Smart Content Reuse Engine

- **Dedicated Groq account** — Uses `GROQ_MODULE4_API_KEY` (a *different* Groq account from Module 3), guaranteeing independent rate-limit budgets. Controlled by `MODULE4_GROQ_ENABLED` (default `True`).
- **Q&A Extraction** (`POST /api/module4/extract-qa`) — Extracts explicit question–answer pairs from RFP text or a pre-bid Q&A addendum. Fields: `question`, `answer`, `context`. Fallback chain: Cloudflare → OpenRouter → Gemini.
- **Tone Adaptation** (`POST /api/module4/adapt-tone`) — Rewrites draft text to a professional, confident, persuasive proposal tone (active voice, removes hedging like "we believe"). Returns `original` and `adapted`.
- **Answer Library** — Persistent reusable-answer store with embedding search:
  - `/api/module4/library/ingest` — ingest a document, extract Q&As, embed them
  - `/api/module4/library/suggest` — retrieve best-matching library answers for a requirement (embedding + lexical)
  - `/api/module4/library/apply`, `/library/draft`, `/library/save-adapted` — apply, draft, and persist adapted answers
- **UI** — `/module4/<record_id>` page and an embedded Smart Reuse tab in results.

---

## Module 5 — Compliance Shred (Section L–M Crosswalk)

- **Purpose** — Isolate Section L (Instructions, Conditions, Notices) and Section M (Evaluation Factors) language from the source document, then map each clause / checklist item onto your **proposal outline** so proposal teams know which section of the response must address each requirement.
- **Pipeline** (`/module5/<record_id>`, API `/module5/crosswalk/<id>`):
  1. Load the paged source text from disk
  2. Detect Section L/M boundaries
  3. Isolate L/M clauses
  4. Embed each clause (Cloudflare `bge-base-en-v1.5`)
  5. Map to outline sections with hybrid scoring: **0.5 × BM25 + 0.5 × cosine**, using a lexical-stopword pass (`_m5_lexical_clean`) so procedural words ("contract", "bidder") don't dominate; falls back to cosine-only when text is missing
- **Output** — Crosswalk table with per-clause best-matching outline section, similarity score, and pass/fail against a threshold, plus a **Strategic Checklist — Answered In** mapping for every checklist item (37 items). Clauses persist into the case's `results.module5_crosswalk`; checklist mappings persist as `module5_*` fields on each checklist item. **Both parts are included in the full PDF export.**

---

## Module 6 — Amendment & Version Delta Tracking

- **Purpose** — Track what changed between an original RFP and a later amendment (or between successive versions), so the team can focus on new requirements and deadline shifts instead of re-reading everything.
- **Linking** — During upload, pick **"Amendment of case"** to set `case_id` on the new record. Chains never form: `_m6_resolve_case_root` always resolves an amendment back to the original root record.
- **Baseline selection** — `_m6_find_baseline` picks the most recent prior analysis in the case group (root + its amendments).
- **Structural diff** (`_m6_diff`) — Compares baseline vs. new analysis across `timeline`, `requirements`, `deliverables` (including sub-deliverable names), `risks`, `evaluation_criteria`, `compliance`, `strategic_checklist` (per category), `go_nogo` (score/verdict/reasons), and `module2_verification`. Items are matched by normalized fingerprints; each change is classified as **Added / Removed / Modified** with severity **High / Low** (High = material change such as a date, number, name, or requirement text shift).
- **AI summary** — `_m6_generate_summary` condenses High-severity changes into 1–3 sentences via Module 4's Groq account (`llama-3.3-70b-versatile`, `response_format=json_object`); a local rule-based fallback sentence is used if Groq is unavailable. Usage token stats are stored in the delta.
- **Delta record** — Stored in `analyses.delta` JSON: `baseline_id`, `baseline_title`, `baseline_timestamp`, counts, change list, `summary`, `usage`. The delta is included in the amendment's **PDF and JSON exports** so the export tells the full baseline → delta → amendment story.
- **UI** —
  - **Amendment Delta banner** on the amendment's results page (vs. baseline title, high/low chips, compare link).
  - **Version History card** on *every* results page of a case group (baseline and amendments): lists all versions newest-first with Baseline/Amendment badges, provider, timestamp, high/low delta counts, and (for amendments) a truncated delta summary; each row links to `/view/<id>` and the currently-viewed record is highlighted.
- **Compare** — Full side-by-side change list at `/compare` (pick two records) with Added / Removed / Modified grouping.

---

## Cross-Module Features

- **Multi-Provider AI Architecture** — Four providers used across tasks:
  - **Gemini** (`gemini-3.5-flash`) — Primary analysis (Module 1/2). Vision helpers exist but are not wired into extraction
  - **OpenRouter** — Alternative analysis provider (configurable model). Vision OCR helper exists (`google/gemma-4-31b-it:free` default) but is not wired in
  - **Groq** — Two independent accounts: Sentinel Copilot (`GROQ_API_KEY`) and Module 4/6 (`GROQ_MODULE4_API_KEY`), each with its own rate-limit budget
  - **Cloudflare Workers AI** — Requirements extraction (`@cf/meta/llama-3.1-8b-instruct`), embeddings for verification/Module 5 (`@cf/baai/bge-base-en-v1.5`), and verification (`@cf/meta/llama-3.1-8b-instruct`)
- **Retry Logic** — Exponential backoff on Gemini 503/429; OpenRouter retries 429/502/503 with Retry-After support and up to 3 attempts for empty/truncated responses (backoff `3 * attempt` seconds); Groq retries on transient errors; Cloudflare retries on rate limits.
- **Resilient JSON Parsing** — `parse_resilient_json` + `_repair_truncated_json` (stack-based brace/string-aware repair that closes nested arrays/objects in the correct order, handles cut-inside-string and deep nesting) for malformed or truncated AI output from any provider; broken-suffix trimming.
- **`coerce_analysis` normalization** — Promotes free-form model output (e.g. `key_requirements` strings) into structured `REQ-001…` records when a section is missing, and fills defaults.
- **Debug dumps** — Raw provider responses (and empty/error responses) written to `debug_dumps/*.json` for diagnosis.
- **Persistent SQLite history** — All analyses survive restarts; session-based views at `/history` with inline rename.
- **RFP Comparison** — Side-by-side two-record diff at `/compare`.
- **Search & Filter** — Real-time search across all result sections; requirement filter box.
- **Hybrid Tab Bar** — Desktop hover reveals an ultra-thin scrollbar; click-arrow scrolls smoothly; mobile hides it for native swipe.
- **Dark Mode** — Toggle persisted via `localStorage` (`theme.js` + `theme.css`).
- **View Transitions** — Crossfade page transitions via the View Transition API.
- **Zero CDN** — All CSS, JS, SVG icons, and favicon self-hosted in `static/`.

---

## Database & Storage

| Item | Location | Notes |
|------|----------|-------|
| **SQLite DB** | `analyses.db` (repo root, auto-created) | Table `analyses`: `id` (TEXT PK, 8-hex), `title`, `filename`, `provider`, `timestamp` (ISO), `hash` (content hash), `word_count`, `results` (full analysis JSON), `session_id`, `case_id` (root of the case group, added by Module 6), `delta` (amendment diff JSON, added by Module 6) |
| **Uploads** | `uploads/` | Source documents, auto-created |
| **Debug dumps** | `debug_dumps/` | Raw / empty / repaired provider responses as JSON |
| **Module 4 library** | `module4_library*.json` (repo root) | Reusable answer library + embeddings |
| **Company profile** | `company_profile*.json` (repo root) | Profile + facts used by verification |
| **Session secret** | `.secret_key` (repo root, auto-created) | Persistent Flask secret so cookies survive restarts |

### Amendment model

```
0d1c6bbc  (Baseline — original RFP)          case_id = NULL
   └── 85115ead (Amendment)                   case_id = '0d1c6bbc', delta = {baseline_id, ...}
```

Every member of a case group points back at the original root; `_m6_resolve_case_root` prevents chains of amendments. The **Version History** card lists the whole group from any member's results page.

---

## Checklist Evaluation Rules

The AI evaluates each of the 37 checklist items against the RFP:

| Status | Meaning |
|--------|---------|
| **Go** | The RFP requirement is met or the item poses no risk |
| **No-Go** | A critical requirement cannot be met (e.g. insurance > $5M) |
| **Review** | Requires human review before bid decision |
| **Escalate** | Needs approval from higher authority (e.g. payment > NET30) |
| **Caution** | Potential risk identified, proceed with care |
| **Not Specified in RFP** | The RFP does not mention this item |

**Financial-specific rules:**
- **Payment Terms:** NET30 → Go; longer than NET30 (NET45/NET60/NET90) → Escalate
- **Insurance Requirements:** ≤ $5M → Go; greater than $5M → No-Go

---

## Tech Stack

- **Backend:** Python 3, Flask, SQLite
- **AI Providers:**
  - **Google Gemini** (`gemini-3.5-flash`) — Module 1/2 analysis (vision OCR helper present, not wired)
  - **OpenRouter** — alternative analysis (configurable model); OCR vision helper present, not wired
  - **Groq** — Sentinel Copilot (`llama-3.3-70b-versatile` via `GROQ_API_KEY`) + Module 4/6 (`GROQ_MODULE4_API_KEY`, separate account)
  - **Cloudflare Workers AI** — requirements extraction, embeddings (`bge-base-en-v1.5`), verification
- **PDF Export:** xhtml2pdf
- **Excel Export:** openpyxl
- **Document Parsing:** pypdf (PDF), python-docx (DOCX); PyMuPDF/fitz is used by the (unwired) OCR helpers
- **JSON Repair:** built-in truncated-JSON recovery (brace/string-aware depth tracking)
- **Frontend:** vanilla CSS (CSS variables) + vanilla JS — no frameworks

---

## Setup

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd rfp-automation-portal
   ```

2. Create a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create a `.env` file with your API keys (see [Environment Variables](#environment-variables)).

5. Run the application:
   ```bash
   .venv\Scripts\python app.py
   ```

6. Open `http://localhost:5000` in your browser.

---

## Environment Variables

```env
# Required for Module 1/2 analysis
GEMINI_API_KEY=your-gemini-api-key

# Alternative analysis provider (optional)
OPENROUTER_API_KEY=your-openrouter-api-key
OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free

# Required for Module 3 Sentinel Copilot
GROQ_API_KEY=your-groq-api-key

# Module 4/6 — separate Groq account (optional, default enabled)
GROQ_MODULE4_API_KEY=your-groq-module4-api-key

# OCR vision models (present but NOT wired into extraction — unused)
OPENROUTER_OCR_MODEL=google/gemma-4-31b-it:free
GROQ_OCR_MODEL=qwen/qwen3.6-27b

# Cloudflare Workers AI — requirements extraction, embeddings, verification (optional)
CLOUDFLARE_ACCOUNT_ID=your-cloudflare-account-id
CLOUDFLARE_API_TOKEN=your-cloudflare-api-token
CLOUDFLARE_REQ_MODEL=@cf/meta/llama-3.1-8b-instruct
CLOUDFLARE_EMBEDDING_MODEL=@cf/baai/bge-base-en-v1.5
CLOUDFLARE_VERIFICATION_MODEL=@cf/meta/llama-3.1-8b-instruct
```

**Key links:**
- Gemini key — [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
- OpenRouter key — [openrouter.ai/keys](https://openrouter.ai/keys)
- Groq key — [console.groq.com/keys](https://console.groq.com/keys)
- Cloudflare credentials — [dash.cloudflare.com](https://dash.cloudflare.com) (Workers AI)

> Note: Cloudflare Workers AI free tier is 10,000 Neurons/day shared across all models on the account, resetting 00:00 UTC.

---

## Usage — Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Upload page (drag & drop, provider toggle, "Amendment of case" dropdown) |
| `/upload` | POST | Run analysis; stores to DB; builds Module 6 delta when amending |
| `/view/<record_id>` | GET | Results page (10 tabs, export toolbar, Version History) |
| `/requirements/<record_id>` | GET | Dedicated extracted-requirements view |
| `/extract_requirements/<id>` | POST | Re-extract requirements (Cloudflare) |
| `/verify_requirements/<id>` | POST | Verify requirements against company profile (Module 2) |
| `/export_pdf`, `/export_deliverables_pdf` | POST | Full / deliverables-only PDF report |
| `/export_json` | POST | Raw analysis JSON |
| `/export_requirements_xlsx` | POST | Requirements Excel workbook |
| `/history` | GET | Persistent analysis history with inline rename |
| `/rename` | POST | Rename a stored analysis |
| `/compare` | GET | Pick two records for side-by-side diff |
| `/company_profile` | GET | Edit company profile |
| `/api/company_profile` | GET/POST | Load/save profile |
| `/api/company_profile/facts` | GET | Flattened facts used by verification |
| `/sentinel/<case_id>` | GET | Sentinel Copilot chat UI |
| `/api/case/<case_id>/ask` | POST | Ask a grounded question (direct match → Groq fallback) |
| `/api/case/<case_id>/rehearse` | POST | Bid rehearsal Q&A |
| `/api/case/<case_id>/partner-matches` | GET | Partner matching suggestions |
| `/api/case/<case_id>/notes`, `/actions` | GET/POST | Persistent notes & actions |
| `/module4/<record_id>` | GET | Smart Content Reuse UI |
| `/api/module4/extract-qa` | POST | Extract Q&A pairs |
| `/api/module4/adapt-tone` | POST | Adapt draft tone |
| `/api/module4/library/...` | POST/GET/DELETE | Answer library (ingest, suggest, apply, draft, save-adapted, delete) |
| `/module5/<record_id>` | GET | Compliance Shred / Section L–M crosswalk UI |
| `/module5/crosswalk/<id>` | GET | Run and persist the crosswalk |
| `/api/analysis/<id>/export.*` | GET | JSON / PDF / XLSX download endpoints — PDF includes requirements verification + full Compliance Shred; JSON includes the amendment `delta` |

### Typical workflow

1. Upload an RFP (PDF/DOCX/TXT) — optionally set a title, choose provider, or link as an **amendment** of a prior case.
2. Watch the analyzing overlay; review the 10-tab results.
3. Use **Extract** to re-run requirements, then **Verify** against your company profile.
4. Open **Sentinel** to ask questions; use **Smart Reuse** to build reusable answers; run **Compliance Shred** to map L/M clauses to your outline.
5. Export the full PDF report (complete analysis + requirements verification + compliance-shred crosswalk + amendment delta), deliverables-only PDF, requirements Excel, or raw JSON.
6. For amendments, check the **Amendment Delta** banner and the **Version History** card to see exactly what changed.
7. Compare any two records at `/compare`.

---

## Project Structure

```
rfp-automation-portal/
├── app.py                     # Flask app: routes, AI providers, parsers, all 6 modules
├── analyses.db                # SQLite persistent history (auto-created)
├── .env                       # API keys (not committed)
├── .secret_key                # Persistent Flask session secret (auto-created)
├── company_profile*.json      # Company profile + facts (auto-created)
├── module4_library*.json      # Module 4 answer library + embeddings (auto-created)
├── requirements.txt
├── tests/
│   ├── test_module6_delta.py  # Module 6 diff/delta unit tests
│   └── test_upload_cache.py   # Upload / caching tests
├── static/
│   ├── css/theme.css          # CSS variables, base styles, page transitions
│   ├── js/theme.js            # Dark/light mode toggle
│   └── icons/
│       ├── sprite.svg         # SVG icon sprite
│       └── logo-icon.svg      # Brand favicon
├── templates/
│   ├── index.html             # Upload page + analyzing overlay
│   ├── results.html           # 10-tab results, export toolbar, Version History
│   ├── requirements_view.html # Extracted requirements + verify
│   ├── history.html           # Persistent history with inline rename
│   ├── compare.html           # Side-by-side comparison
│   ├── sentinel.html          # RFP Sentinel Copilot chat
│   ├── module4.html           # Smart Content Reuse
│   ├── module5.html           # Compliance Shred / Section L–M crosswalk
│   ├── company_profile.html   # Company profile editor
│   ├── report_template.html   # PDF export template (full)
│   └── deliverables_template.html  # PDF export template (deliverables-only)
├── uploads/                   # Uploaded documents (auto-created)
├── debug_dumps/               # Raw AI responses for debugging (auto-created)
└── README.md
```

---

## Testing

```bash
.venv\Scripts\python -m unittest discover -s tests
```

The suite covers Module 6 delta generation (`test_module6_delta.py`) and upload/caching behavior (`test_upload_cache.py`). Run it after any change to the analysis pipeline, diff engine, or persistence layer.

## Limitations

- **No OCR** — document parsing is `pypdf`-only. Vision OCR helpers exist in `app.py` (`_ocr_pdf_page_gemini/openrouter/groq`) but are **not called** by the extraction pipeline. Scanned/image-only PDFs without a text layer produce empty text, which fails or empties the analysis. Pass a text-layer PDF (or run OCR yourself) for such documents.
- **Session-bound history** — records are tied to a browser `session_id`; the history query falls back to recent global records if the session has none.
- **Single-user** — no authentication or role-based access control; designed for local/private-network use.
- **AI output quality** — free-tier models (e.g. the default OpenRouter model) can return empty or truncated JSON; the resilient parser, retries, and error page mitigate this but cannot guarantee results.
