# RFP Automation Portal

AI-powered RFP (Request for Proposal) analysis tool that automatically extracts deliverables, evaluation criteria, compliance requirements, risks, timelines, Go/No-Go recommendations, and strategic checklist evaluations from uploaded documents.

## Features

- **Dual AI Provider** — Choose Google Gemini (`gemini-3.5-flash`) or Groq (`llama-3.3-70b-versatile`) on upload. Both use plain JSON prompts with resilient parsing for truncated responses
- **Drag & Drop Upload** — Supports PDF, DOCX, and TXT (up to 16MB) with optional title and provider selection
- **Executive Summary** — Auto-generated overview of the RFP
- **8-Tab Results Interface** — Deliverables, Evaluation Criteria, Compliance, Risks, Timeline, Key Requirements, Checklist (with status summary bar + 37-item structured analysis), Go/No-Go
- **Go / No-Go Analysis** — AI-powered pursuit recommendation with score gauge (0-100), verdict badge (Go/Caution/No-Go), and 4-8 weighted factor breakdown
- **Bid Qualification Checklist** — Fixed 37-item checklist across 4 departments with explicit evaluation rules: Financial (5 items), Legal (12 items), Operations (15 items), Technical (5 items). Each item includes: status, reasoning, risk level, RFP evidence (direct quotes + section references), impact on bid strategy, and mitigation strategy
- **Departmental Compliance** — Financial, Legal, Operations, and Technical requirements matrix
- **Risk Assessment** — Identifies risks with category and severity (High/Medium/Low)
- **Timeline & Milestones** — Key dates and deadlines from the document
- **Hybrid Tab Bar** — Desktop hover reveals ultra-thin dark-purple scrollbar; click arrow scrolls tabs smoothly; mobile hides scrollbar for native swipe
- **Analyzing Overlay** — Full-screen animated overlay with step-by-step progress cycling during document processing
- **Analysis History** — Session-based history with inline rename
- **RFP Comparison** — Side-by-side comparison of two analyses at `/compare`
- **Export Options** — Download as PDF (xhtml2pdf) or JSON
- **Search & Filter** — Real-time search across all results sections
- **Dark Mode** — Toggleable theme persisted via localStorage
- **View Transitions** — Crossfade page transitions using the View Transition API
- **Zero CDN** — All assets (CSS, JS, SVG icons, favicon) self-hosted in `static/`
- **Resilient JSON Parsing** — Strings-aware brace tracking, unterminated-string handling, automatic truncation recovery, and broken-suffix trimming for malformed AI responses
- **Retry Logic** — Exponential backoff on Gemini 503/429 errors; Groq retries on 413/rate-limit errors
- **Document Truncation** — Gemini: 200K chars; Groq: 25K chars (fits under 12K TPM limit)

## Checklist Evaluation Rules

The AI evaluates each of the 37 checklist items against the RFP document using these rules:

| Status | Meaning |
|--------|---------|
| **Go** | The RFP requirement is met or the item poses no risk |
| **No-Go** | A critical requirement cannot be met (e.g., insurance >$5M) |
| **Review** | Requires human review before bid decision |
| **Escalate** | Needs approval from higher authority (e.g., payment >NET30) |
| **Caution** | Potential risk identified, proceed with care |
| **Not Specified in RFP** | The RFP does not mention this item |

**Financial-specific rules:**
- **Payment Terms:** NET30 → Go; longer than NET30 (NET45/NET60/NET90) → Escalate
- **Insurance Requirements:** $5M or less → Go; greater than $5M → No-Go

## Tech Stack

- **Backend:** Python, Flask
- **AI:** Google Gemini API (`gemini-3.5-flash`) or Groq (`llama-3.3-70b-versatile`)
- **PDF Export:** xhtml2pdf
- **Document Parsing:** pypdf, python-docx
- **JSON Repair:** Built-in truncated JSON recovery (brace-matching, string-aware depth tracking, suffix trimming)
- **Frontend:** Vanilla CSS (CSS variables), Vanilla JS — no frameworks

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
   pip install flask python-docx pypdf pydantic google-genai xhtml2pdf python-dotenv groq PyMuPDF
   ```

4. Create a `.env` file with your API keys:
   ```env
   GEMINI_API_KEY=your-gemini-api-key
   GROQ_API_KEY=your-groq-api-key
   ```
   - Get a Gemini key at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
   - Get a Groq key at [console.groq.com/keys](https://console.groq.com/keys) (recommended for speed)

5. Run the application:
   ```bash
   .venv\Scripts\python app.py
   ```

6. Open `http://localhost:5000` in your browser.

## Usage

1. Upload an RFP (PDF, DOCX, or TXT) via drag & drop or file picker — optionally set a title and choose Gemini or Groq
2. The analyzing overlay shows progress through extraction → analysis → compliance → finalization
3. Browse results in the 8-tab interface:
   - **Deliverables** — Required tangible items and reports
   - **Evaluation** — Scoring criteria and point allocations
   - **Compliance** — Financial, Legal, Operations, Technical requirements
   - **Risks** — Categorized risks with severity ratings
   - **Timeline** — Important dates and deadlines
   - **Key Req.** — Top prioritized requirements
    - **Checklist** — 37-item structured analysis with status summary bar (color-coded counts for all 6 statuses), RFP evidence per item, risk levels, impact on bid strategy, and mitigation strategies
   - **Go / No-Go** — Score gauge, verdict badge, and weighted factor reasons
4. Use the search bar to filter across all sections
5. Export as PDF or JSON
6. View analysis history at `/history` — rename analyses inline
7. Compare two analyses side by side at `/compare`
8. Toggle dark mode with the theme button (persists across sessions)

## Project Structure

```
rfp-automation-portal/
├── app.py                   # Flask routes, AI analysis, resilient JSON parser, normalizers
├── .env                     # API keys (not committed)
├── static/
│   ├── css/theme.css        # CSS variables, base styles, page transitions
│   ├── js/theme.js          # Dark/light mode toggle
│   └── icons/
│       ├── sprite.svg       # SVG icon sprite
│       └── logo-icon.svg    # Brand favicon
├── templates/
│   ├── index.html           # Upload page with drag-drop, provider toggle, analyzing overlay
│   ├── results.html         # 8-tab results, search, export, dark mode, hybrid tab bar
│   ├── history.html         # Session history with inline rename
│   ├── compare.html         # Side-by-side document comparison
│   └── report_template.html # PDF export template with strategic checklist
├── uploads/                 # Uploaded documents (auto-created)
├── debug_dumps/             # Raw AI responses for debugging (auto-created on success)
├── requirements.txt
├── LICENSE (MIT)
└── README.md
```
