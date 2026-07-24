# Software Requirements Specification

## RFP Automation Portal

**Version:** 1.0  
**Date:** July 2026  
**Prepared for:** Internal Development  
**Prepared by:** Bilal Tech

---

## Table of Contents

1. [Introduction](#1-introduction)  
   1.1 [Purpose](#11-purpose)  
   1.2 [Document Conventions](#12-document-conventions)  
   1.3 [Intended Audience and Reading Suggestions](#13-intended-audience-and-reading-suggestions)  
   1.4 [Product Scope](#14-product-scope)  
   1.5 [References](#15-references)  

2. [Overall Description](#2-overall-description)  
   2.1 [Product Perspective](#21-product-perspective)  
   2.2 [Product Functions](#22-product-functions)  
   2.3 [User Characteristics](#23-user-characteristics)  
   2.4 [Assumptions and Dependencies](#24-assumptions-and-dependencies)  

3. [System Architecture](#3-system-architecture)  
   3.1 [Architectural Overview](#31-architectural-overview)  
   3.2 [Technology Stack](#32-technology-stack)  
   3.3 [Data Flow](#33-data-flow)  

4. [External Interface Requirements](#4-external-interface-requirements)  
   4.1 [User Interfaces](#41-user-interfaces)  
   4.2 [Hardware Interfaces](#42-hardware-interfaces)  
   4.3 [Software Interfaces](#43-software-interfaces)  
   4.4 [Communications Interfaces](#44-communications-interfaces)  

5. [System Features and Functional Requirements](#5-system-features-and-functional-requirements)  
   5.1 [Document Upload and Processing](#51-document-upload-and-processing)  
   5.2 [AI-Powered Analysis](#52-ai-powered-analysis)  
   5.3 [Analysis Results Display](#53-analysis-results-display)  
   5.4 [Strategic Checklist (23-Item Evaluation)](#54-strategic-checklist-23-item-evaluation)  
   5.5 [Go/No-Go Recommendation](#55-go-no-go-recommendation)  
   5.6 [Export Functionality](#56-export-functionality)  
   5.7 [History and Persistence](#57-history-and-persistence)  
   5.8 [Comparison Module](#58-comparison-module)  
   5.9 [Session Management](#59-session-management)  

6. [Data Model and Schema](#6-data-model-and-schema)  
   6.1 [Database Schema](#61-database-schema)  
   6.2 [Pydantic Validation Models](#62-pydantic-validation-models)  

7. [Non-Functional Requirements](#7-non-functional-requirements)  
   7.1 [Performance Requirements](#71-performance-requirements)  
   7.2 [Security Requirements](#72-security-requirements)  
   7.3 [Reliability and Availability](#73-reliability-and-availability)  
   7.4 [API Rate Limit Handling](#74-api-rate-limit-handling)  
   7.5 [Browser Compatibility](#75-browser-compatibility)  
   7.6 [Asset Management](#76-asset-management)  

8. [AI Provider Specifications](#8-ai-provider-specifications)  
   8.1 [Gemini Integration](#81-gemini-integration)  
   8.2 [Groq Integration](#82-groq-integration)  
   8.3 [Retry and Fallback Strategy](#83-retry-and-fallback-strategy)  

9. [Constraints and Limitations](#9-constraints-and-limitations)  
   9.1 [Technical Constraints](#91-technical-constraints)  
   9.2 [API Service Constraints](#92-api-service-constraints)  

10. [Appendices](#10-appendices)  
    10.1 [Glossary](#101-glossary)  
    10.2 [Route Map](#102-route-map)  

---

## 1. Introduction

### 1.1 Purpose

The RFP Automation Portal is a web-based application that automates the analysis of Request for Proposal (RFP) documents. The system ingests uploaded documents in PDF, DOCX, or TXT formats, transmits the extracted text to an artificial intelligence service (Gemini or Groq), and returns a structured, multi-section analysis covering deliverables, compliance requirements, risks, timelines, key requirements, a 23-item strategic checklist, and a Go/No-Go recommendation with scoring.

The primary objective is to reduce manual effort in bid qualification from hours to seconds while maintaining high accuracy through evidence-based, citation-grounded analysis.

### 1.2 Document Conventions

- Requirement identifiers follow the pattern FR-NNN for functional requirements and NFR-NNN for non-functional requirements.
- Priority levels are defined as High (essential for minimum viable product), Medium (important but not critical), and Low (nice-to-have).
- The key words "MUST", "SHALL", "SHOULD", "MAY", and "OPTIONAL" in this document follow RFC 2119 interpretation.

### 1.3 Intended Audience and Reading Suggestions

| Audience | Sections of Interest |
|---|---|
| Development Team | Sections 3, 5, 6, 7, 8, 9 |
| Quality Assurance | Sections 5, 7, 8.3 |
| Project Management | Sections 1, 2, 5 (summary) |
| DevOps / Infrastructure | Sections 3, 7.1, 9 |
| End Users | Section 2.2, Section 4.1 |

### 1.4 Product Scope

The RFP Automation Portal is a single-user Flask application intended for internal use by bid managers and proposal teams. It replaces the manual process of reading, annotating, and summarizing RFP documents with an automated pipeline that produces consistent, structured, and auditable analysis reports.

Key capabilities in scope:

- Document ingestion with drag-drop upload, file-type validation, and size limiting (50 MB maximum).
- AI-driven analysis using Gemini 2.5 Flash or Groq (Llama 3.3 70B) with configurable provider selection.
- Eight-tab results interface: Summary, Deliverables, Evaluation Criteria, Compliance, Risks, Timeline, Key Requirements, Checklist (23-item), and Go/No-Go.
- Persistent analysis history using SQLite with session-independent record access.
- PDF and JSON export with cache-busting filenames and xhtml2pdf-compatible templates.
- Side-by-side comparison of any two past analyses.
- Dark mode toggle with localStorage persistence.

Out of scope:

- Multi-user authentication and role-based access control.
- Real-time collaborative editing.
- Document version comparison or diffing.
- Natural language querying of analysis history.
- Integration with external CRM or ERP systems.

### 1.5 References

| Reference | Source |
|---|---|
| Google Generative AI SDK (google-genai) v2.10.0 | https://ai.google.dev |
| Groq Cloud API | https://console.groq.com/docs |
| xhtml2pdf 0.2.17 | https://github.com/xhtml2pdf/xhtml2pdf |
| Pydantic v2 | https://docs.pydantic.dev |
| Flask 3.x | https://flask.palletsprojects.com |
| PyMuPDF (fitz) | https://pymupdf.readthedocs.io |
| RFP Checklist Source Document | `D:\User\Download\RFP_Checklist.pdf` |

---

## 2. Overall Description

### 2.1 Product Perspective

The RFP Automation Portal is a standalone web application. It does not depend on any external service beyond the two AI providers (Gemini and Groq). All application logic, template rendering, data persistence, and asset serving are handled within a single Flask process.

The system replaces a manual, error-prone process with a consistent, automated pipeline. In the manual workflow, a bid analyst would read a 50-200 page RFP document, take notes, populate spreadsheets, and produce a bid qualification memo. This process requires 2-8 hours per document and produces inconsistent output across analysts. The automated system reduces this to 30-120 seconds per document with structured, reproducible output.

### 2.2 Product Functions

The system provides the following major functions:

1. **Document Ingestion**: Accept PDF, DOCX, and TXT files via drag-drop zone or file browser. Validate file type, size (50 MB max), and extract raw text using pypdf (PDF), python-docx (DOCX), or native file reading (TXT). Limit text extraction to 100 pages maximum.

2. **AI Analysis**: Transmit extracted text (truncated to 5000 characters for Gemini, 1000 characters for Groq) to the selected AI provider. Use structured output schemas (Pydantic models) to enforce response format. Retry on server errors with exponential backoff (1s, 2s, 4s). Recover partial JSON from Groq error responses via the failed_generation field.

3. **Results Display**: Render analysis results in an eight-tabbed interface with search, filter, and expandable detail cards. Each checklist item displays nine fields: item name, status, reasoning, RFP evidence, impact on bid strategy, risk level, mitigation strategy, analysis, and recommendation.

4. **Strategic Checklist**: A 23-item evaluation across four categories (5 Financial, 7 Legal, 6 Operations, 5 Technical). Each item receives a status (Go, No-Go, Escalate, Review, Caution, or Not Specified in RFP) with evidence citation and strategic analysis. A summary status bar displays aggregate counts per status category.

5. **Export**: Generate PDF using xhtml2pdf (table-based layout, no SVG or flexbox) or JSON. Export filenames use the sanitized analysis title with a Unix timestamp to force fresh downloads.

6. **History Management**: Persist all analyses in a SQLite database. View, rename, re-open, and compare past analyses. Session-independent access ensures records survive server restarts and browser cookie changes.

7. **Comparison**: Select any two past analyses for side-by-side viewing, including all analysis sections and the strategic checklist.

### 2.3 User Characteristics

The primary user is a bid manager or proposal analyst with the following characteristics:

- Technical proficiency: Basic computer literacy (file upload, browser navigation, form submission).
- Domain knowledge: Familiarity with RFP processes, bid qualification terminology, and compliance requirements.
- Workflow: Typically analyzes 5-20 RFP documents per week. Requires quick turnaround and consistent output.
- Environment: Desktop or laptop with a modern web browser (Chrome, Firefox, Edge, Safari).

Secondary users include executives who review the Go/No-Go recommendations and compliance checklists for approval decisions.

### 2.4 Assumptions and Dependencies

**Assumptions:**

- The system operates on a local machine or private network with internet access for AI API calls.
- Each user has their own browser instance (single-user design, no concurrent session isolation).
- Uploaded documents are not encrypted, password-protected, or image-only scans. Scanned PDFs without extractable text layers will return empty text and produce an error.
- API keys for Gemini and Groq are configured in the `.env` file before first use.

**Dependencies:**

- `google-genai` SDK version 2.10.0 or higher for Gemini API integration.
- `groq` Python package for Groq API integration.
- `pypdf` for PDF text extraction, `python-docx` for DOCX extraction.
- `xhtml2pdf` version 0.2.17 for PDF export (with known SVG and flexbox limitations).
- `pydantic` version 2.x for response validation.
- `PyMuPDF` (fitz) for future image-based PDF processing.
- Python 3.12+ runtime environment.

---

## 3. System Architecture

### 3.1 Architectural Overview

The application follows a monolithic Flask architecture with the following components:

```
+------------------+       +------------------+       +------------------+
|                  |       |                  |       |                  |
|  Browser Client  | <--> |   Flask Server   | <--> |   SQLite DB      |
|  (HTML/CSS/JS)   |       |   (app.py)       |       |  (analyses.db)   |
|                  |       |                  |       |                  |
+------------------+       +--------+---------+       +------------------+
                                     |
                                     v
                          +------------------+
                          |                  |
                          |   AI Providers   |
                          |  (Gemini/Groq)   |
                          |                  |
                          +------------------+
```

The browser client renders templates server-side (Jinja2) with minimal client-side JavaScript for interactivity (dark mode toggle, tab switching, search filtering, drag-drop upload, inline rename).

### 3.2 Technology Stack

| Layer | Technology | Version |
|---|---|---|
| Runtime | Python | 3.12+ |
| Web Framework | Flask | 3.x |
| Template Engine | Jinja2 | (bundled with Flask) |
| Database | SQLite3 | (built-in) |
| ORM/Validation | Pydantic | 2.x |
| AI - Gemini SDK | google-genai | >=2.10.0 |
| AI - Groq SDK | groq | latest |
| PDF Generation | xhtml2pdf | 0.2.17 |
| PDF Text Extraction | pypdf | latest |
| DOCX Text Extraction | python-docx | latest |
| Image PDF Support | PyMuPDF (fitz) | latest |
| Environment | python-dotenv | latest |
| CSS | Custom (no frameworks) | - |
| Icons | Inline SVG (Feather-style) | - |

### 3.3 Data Flow

1. **Upload Flow:**
   - User selects a file via drag-drop or browser picker.
   - Client-side JavaScript validates file type (pdf/docx/txt) and size (<= 50 MB).
   - Form is submitted to `/upload` as multipart/form-data.
   - Flask saves the file to the `uploads/` directory with a sanitized filename.
   - Text is extracted using the appropriate parser (pypdf, python-docx, or raw read).
   - Text is truncated to the provider-specific character limit.
   - The selected provider function is called with the truncated text.
   - The AI response is validated against the RFPAnalysis Pydantic schema.
   - Validated results are serialized to JSON and stored in the SQLite `analyses` table.
   - The results page is rendered with the analysis data.

2. **View Flow:**
   - User navigates to `/history` to see a list of past analyses.
   - Clicking a record navigates to `/view/<record_id>`.
   - The record is loaded from SQLite, results are deserialized, and the full results page is rendered.

3. **Export Flow:**
   - User clicks Export (PDF or JSON) on the results page.
   - The `results_data` hidden input contains the full analysis as a JSON string.
   - For PDF: Flask renders the `report_template.html` with xhtml2pdf-compatible markup, converts to PDF bytes, and returns as a download with cache-busting headers.
   - For JSON: Flask returns the JSON string as a downloadable file.

---

## 4. External Interface Requirements

### 4.1 User Interfaces

The system provides the following views:

**4.1.1 Upload Page (`/`)**

- Animated gradient orb background (CSS-only, no JavaScript animation).
- Drag-drop zone with visual feedback (border color change, icon update).
- File preview card showing filename, size, and a remove button.
- Provider toggle (Gemini / Groq) as styled radio buttons.
- Analysis title text input with placeholder text.
- Submit button with loading spinner, disabled state until file is selected.
- Analyzing overlay: full-screen with spinning ring, step progress indicator, and AI model name that updates based on provider selection. Step text cycles every 3 seconds.

**4.1.2 Results Page (`/view/<id>` or rendered after upload)**

- Page title displaying the analysis title with inline edit (pencil icon).
- Provider badge showing which AI service was used.
- Tab bar with 8 tabs. Hybrid design: horizontal scroll with hidden scrollbar on desktop, gradient fade indicators, click arrow navigation on non-touch devices (detected via `matchMedia` pointer:coarse). No arrow on touch devices.
- Tab content panels:
  - **Deliverables**: Bulleted list of extracted deliverables from the RFP.
  - **Evaluation Criteria**: Scoring metrics, point allocations, and judgment guidelines.
  - **Compliance**: 4-column matrix (Financial, Legal, Operations, Technical).
  - **Risks**: List with category, description, and severity badge (High=red, Medium=yellow, Low=green).
  - **Timeline**: Milestone entries with date references.
  - **Key Requirements**: Numbered list of top requirements extracted from the RFP.
  - **Checklist**: Status summary bar at top with aggregate counts (Go, No-Go, Review, Escalate, Caution, Not Specified in RFP). Below: 23 expandable cards sorted by category (Financial, Legal, Operations, Technical), each with colored left border (green=Go, red=No-Go, orange=Escalate, blue=Review, amber=Caution, gray=Not Specified). Each card expands to show all 9 fields.
  - **Go/No-Go**: SVG gauge visualization (0-100), verdict badge (Go=green, Caution=amber, No-Go=red), weighted reason cards (4-8 items with factor, detail, and weight).
- Search input: filters all visible checklist items by text match.
- Export buttons: PDF and JSON, each POSTs the results data to the export endpoint.
- "Back to History" link.

**4.1.3 History Page (`/history`)**

- List of past analyses as cards with title (clickable to view), filename, timestamp, word count, deliverable count, and risk count.
- Inline rename via pencil icon click: text becomes an input field, saves via XHR POST to `/rename`.
- "View" button navigates to `/view/<id>`.
- "New" button navigates to `/`.
- Empty state with prompt to upload a document when no records exist.

**4.1.4 Comparison Page (POST to `/compare`)**

- Side-by-side layout with two analysis columns.
- Each column rendered identically to the results page structure.
- Strategic checklist shown for both analyses with status badges.

**4.1.5 Shared UI Elements**

- Dark mode toggle button (fixed position, top-right). Persists in localStorage.
- Theme toggle: switches CSS variables via `data-theme` attribute on `<html>`.
- Page transitions: View Transition API for fade-in on page load and crossfade on link clicks.
- Toast notifications for errors (file too large, invalid type, API errors).
- Footer: "Powered by Bilal Tech".

### 4.2 Hardware Interfaces

No direct hardware interfaces. The system runs on standard x86-64 hardware with a minimum of 4 GB RAM and 500 MB free disk space.

### 4.3 Software Interfaces

| Interface | Protocol | Data Format | Purpose |
|---|---|---|---|
| Gemini API | HTTPS (REST) | JSON (via SDK) | AI document analysis |
| Groq API | HTTPS (REST) | JSON (via SDK) | AI document analysis |
| File System | Local FS | Binary files | Upload storage and text extraction |
| SQLite | Local IPC | SQL | Persistent data storage |

### 4.4 Communications Interfaces

All communications occur over HTTPS for external API calls. The web server listens on `127.0.0.1:5000` by default (development mode). No inter-service communication or message queuing is required.

---

## 5. System Features and Functional Requirements

### 5.1 Document Upload and Processing

**FR-001**: The system SHALL accept file uploads via a drag-drop zone and a standard file browser input.  
**Priority**: High  
**Validation**: Only PDF (.pdf), DOCX (.docx), and TXT (.txt) files SHALL be accepted. Files exceeding 50 MB SHALL be rejected client-side and server-side with a descriptive error message.  
**FR-002**: The system SHALL extract text from PDF files using pypdf, limiting extraction to a maximum of 100 pages.  
**Priority**: High  
**FR-003**: The system SHALL extract text from DOCX files using python-docx by concatenating all paragraph texts.  
**Priority**: High  
**FR-004**: The system SHALL read TXT files as UTF-8 text directly.  
**Priority**: High  
**FR-005**: Uploaded files SHALL be saved with a sanitized filename (via Werkzeug secure_filename) to the `uploads/` directory.  
**Priority**: High  
**FR-006**: The system SHALL compute a SHA-256 hash (first 16 hex characters) of each uploaded file for deduplication.  
**Priority**: Medium  

### 5.2 AI-Powered Analysis

**FR-007**: The system SHALL support two AI providers: Gemini (via google-genai SDK) and Groq (via groq SDK).  
**Priority**: High  
**FR-008**: The user SHALL select the AI provider before upload via a toggle on the upload page. The default provider SHALL be Gemini.  
**Priority**: High  
**FR-009**: Extracted document text SHALL be truncated to 5000 characters for Gemini and 1000 characters for Groq before transmission.  
**Priority**: High  
**Rationale**: Truncation prevents token limit errors on the free tier of both providers.  
**FR-010**: The system SHALL use structured output (response_schema for Gemini, response_format=json_object for Groq) to enforce valid JSON responses.  
**Priority**: High  
**FR-011**: On server errors (5xx), the system SHALL retry up to 3 times with exponential backoff (1s, 2s, 4s).  
**Priority**: High  
**FR-012**: On client errors (4xx), the system SHALL NOT retry except for 429 (rate limit) errors which SHALL retry with backoff.  
**Priority**: High  
**FR-013**: For Groq, if the API response contains a `failed_generation` field, the system SHALL attempt to extract and parse partial JSON from it as a fallback.  
**Priority**: Medium  
**FR-014**: The system SHALL clean AI output by stripping markdown fences, removing control characters, and balancing unmatched trailing quotes before JSON parsing.  
**Priority**: High  

### 5.3 Analysis Results Display

**FR-015**: The results page SHALL display analysis data across 8 tabs, each accessible by clicking the corresponding tab header.  
**Priority**: High  
**FR-016**: The tab bar SHALL use a hybrid scroll design: hidden scrollbar by default, thin dark-purple scrollbar on hover, click arrow navigation on non-touch devices, and no arrow on touch devices.  
**Priority**: Medium  
**FR-017**: Each results section (Deliverables, Evaluation Criteria, Compliance, Risks, Timeline, Key Requirements) SHALL render its data as formatted HTML with appropriate styling (lists, tables, badges).  
**Priority**: High  
**FR-018**: Risk severity SHALL be displayed as color-coded badges (High=red, Medium=yellow/amber, Low=green).  
**Priority**: Medium  

### 5.4 Strategic Checklist (23-Item Evaluation)

**FR-019**: The Checklist tab SHALL display a status summary bar at the top showing aggregate counts for each status: Go, No-Go, Review, Escalate, Caution, and Not Specified in RFP.  
**Priority**: High  
**FR-020**: Below the summary bar, 23 checklist items SHALL be displayed grouped by category (Financial: 5 items, Legal: 7 items, Operations: 6 items, Technical: 5 items), sorted within each category.  
**Priority**: High  
**FR-021**: Each checklist item SHALL display as an expandable card with a colored left border matching its status (green=Go, red=No-Go, orange=Escalate, blue=Review, amber=Caution, gray=Not Specified in RFP).  
**Priority**: High  
**FR-022**: Each expanded card SHALL display all 9 detail fields: Item Name, Status, Reasoning, What RFP Says, RFP Evidence, Impact on Bid Strategy, Risk Level, Mitigation Strategy, Analysis, and Recommendation.  
**Priority**: High  
**FR-023**: A search input SHALL filter visible checklist items by matching text against item names and category headers.  
**Priority**: Medium  

### 5.5 Go/No-Go Recommendation

**FR-024**: The Go/No-Go tab SHALL display a circular gauge visualization showing the score (0-100).  
**Priority**: High  
**FR-025**: A verdict badge SHALL be displayed below the gauge: Go (green, score >= 70), Caution (amber, score 40-69), No-Go (red, score < 40).  
**Priority**: High  
**FR-026**: Each weighted reason SHALL display as a card with three fields: factor, detail, and weight (High/Medium/Low).  
**Priority**: High  

### 5.6 Export Functionality

**FR-027**: The system SHALL provide PDF export using xhtml2pdf 0.2.17. The PDF template SHALL use table-based layout only (no SVG, no flexbox, no CSS `@page` margin-box rules).  
**Priority**: High  
**FR-028**: The PDF export SHALL include all analysis sections: summary, go/nogo, deliverables, evaluation criteria, risks, timeline, key requirements, compliance (4-column matrix), and checklist (status summary bar + 9-field detail table).  
**Priority**: High  
**FR-029**: PDF and JSON export filenames SHALL use the format `RFP_Analysis_<sanitized_title>_<timestamp>.pdf` and `RFP_Analysis_<sanitized_title>.json` respectively.  
**Priority**: High  
**FR-030**: PDF download responses SHALL include cache-control headers (`Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0`) to force fresh downloads.  
**Priority**: Medium  

### 5.7 History and Persistence

**FR-031**: All analyses SHALL be saved to a SQLite database (`analyses.db`) located in the application root directory.  
**Priority**: High  
**FR-032**: The analyses table SHALL contain the following columns: id (UUID, primary key), title, filename, provider, timestamp (ISO 8601), hash (SHA-256 first 16 chars), word_count, results (JSON string), and session_id.  
**Priority**: High  
**FR-033**: The history page (`/history`) SHALL display the 50 most recent analyses matching the current session, falling back to the 20 most recent analyses across all sessions if the session-specific query returns no results.  
**Priority**: High  
**FR-034**: Users SHALL be able to rename analysis titles via inline edit on the history page. Changes SHALL be persisted via a POST to `/rename`.  
**Priority**: Medium  
**FR-035**: Clicking an analysis in history SHALL navigate to `/view/<record_id>` which loads the full analysis from the database and renders the results page.  
**Priority**: High  

### 5.8 Comparison Module

**FR-036**: Users SHALL be able to select any two past analyses and view them side-by-side via a POST to `/compare`.  
**Priority**: Medium  
**FR-037**: The comparison view SHALL include all analysis sections and the strategic checklist for both records, rendered in parallel columns.  
**Priority**: Medium  

### 5.9 Session Management

**FR-038**: The system SHALL use cookie-based sessions. A persistent `secret_key` SHALL be stored in a `.secret_key` file in the application root to ensure session validity across server restarts.  
**Priority**: High  
**FR-039**: A `session_id` SHALL be generated on the first request and stored in the session. This ID SHALL be used as a secondary identifier for database queries.  
**Priority**: High  

---

## 6. Data Model and Schema

### 6.1 Database Schema

```sql
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,                    -- UUID (first 8 characters)
    title TEXT NOT NULL,                    -- User-defined or auto-generated title
    filename TEXT NOT NULL,                 -- Original uploaded filename
    provider TEXT NOT NULL DEFAULT 'gemini', -- 'gemini' or 'groq'
    timestamp TEXT NOT NULL,                -- ISO 8601 timestamp
    hash TEXT NOT NULL,                     -- First 16 chars of SHA-256
    word_count INTEGER DEFAULT 0,           -- Word count of extracted text
    results TEXT NOT NULL,                  -- JSON blob of the full analysis
    session_id TEXT NOT NULL                -- Browser session identifier
);
```

### 6.2 Pydantic Validation Models

The following Pydantic models define the expected structure of AI responses. All responses MUST pass validation against these models before being stored or displayed.

**RFPAnalysis** (top-level container):
- `summary`: string — Executive summary of the RFP (2-3 sentences)
- `deliverables`: List[string] — Tangible items, products, or reports to provide
- `evaluation_criteria`: List[string] — Scoring metrics, point allocations, guidelines
- `compliance`: ComplianceChecklist — 4-category compliance matrix
- `risks`: List[RiskItem] — Identified risks with severity
- `timeline`: List[TimelineMilestone] — Important dates and deadlines
- `key_requirements`: List[string] — Top 5-8 most important requirements
- `go_nogo`: GoNoGo — Score, verdict, summary, and weighted reasons
- `strategic_checklist`: StrategicChecklist — Full 23-item evaluation

**ChecklistItem** (9 fields):
- `item`: string — Exact checklist item name
- `status`: string — One of: Go, No-Go, Escalate, Review, Caution, Not Specified in RFP
- `reasoning`: string — Explanation for the status, citing specific RFP clauses
- `what_rfp_says`: string — What the RFP states about this item
- `rfp_evidence`: string — Direct quotes with quotation marks, section numbers, amounts
- `impact_on_bid_strategy`: string — How this item shapes pricing, teaming, partnerships
- `risk_level`: string — High, Medium, or Low
- `mitigation_strategy`: string — Actionable plan with owner, timeline, contingency
- `analysis`: string — Deep strategic analysis across competitive, resource, financial, timeline, risk, and capability dimensions
- `recommendation`: string — Specific action: what to do, who, when, why

**GoNoGo**:
- `score`: integer (0-100) — Overall pursuit score
- `verdict`: string — Go, Caution, or No-Go
- `summary`: string — One-sentence recommendation
- `reasons`: List[GoNoGoReason] — 4-8 weighted factors

**GoNoGoReason**:
- `factor`: string — Evaluation criterion
- `detail`: string — Why this factor supports or opposes pursuit
- `weight`: string — High, Medium, or Low

**RiskItem**:
- `category`: string — Risk category (e.g., Financial, Timeline, Technical, Legal)
- `description`: string — Description of the specific risk
- `severity`: string — High, Medium, or Low

**TimelineMilestone**:
- `milestone`: string — Milestone or deadline name
- `date_reference`: string — Date or timeframe reference from the document

**ComplianceChecklist**:
- `Financial`: string — Payment terms, financial stability, insurance, profitability, bid bonds
- `Legal`: string — Eligibility, capability, quantum of input, compliance, state registration, e-verify, contractual obligations
- `Operations`: string — Required forms, submission deadlines, document compliance, signatory authority, vendor registration
- `Technical`: string — Scope of services, technical specifications, industry standards, security, integration

**StrategicChecklist**:
- `executive_summary`: string — Overall recommendation summary
- `financial`: List[ChecklistItem] — 5 items
- `legal`: List[ChecklistItem] — 7 items
- `operations`: List[ChecklistItem] — 6 items
- `technical`: List[ChecklistItem] — 5 items

---

## 7. Non-Functional Requirements

### 7.1 Performance Requirements

**NFR-001**: Analysis processing time (upload to results display) SHALL NOT exceed 120 seconds for 95% of requests under normal conditions.  
**Priority**: High  
**NFR-002**: Page load time for the results page SHALL NOT exceed 2 seconds for cached analyses (excluding AI processing time).  
**Priority**: Medium  
**NFR-003**: The SQLite database SHALL support at least 500 analysis records without noticeable performance degradation.  
**Priority**: Low  
**NFR-004**: PDF export SHALL complete within 5 seconds for a standard analysis.  
**Priority**: Medium  

### 7.2 Security Requirements

**NFR-005**: API keys SHALL be stored in a `.env` file excluded from version control via `.gitignore`. The `_get_env()` helper SHALL strip surrounding quotes to prevent authentication failures.  
**Priority**: High  
**NFR-006**: Uploaded filenames SHALL be sanitized using `werkzeug.secure_filename` to prevent path traversal attacks.  
**Priority**: High  
**NFR-007**: The Flask debug mode SHALL be disabled in production deployments.  
**Priority**: High  
**NFR-008**: The server SHALL run on a local interface (127.0.0.1) by default and SHOULD be placed behind a reverse proxy (nginx, Caddy) in production.  
**Priority**: Medium  

### 7.3 Reliability and Availability

**NFR-009**: The system SHALL handle API provider downtime gracefully by displaying descriptive error messages to the user rather than crashing.  
**Priority**: High  
**NFR-010**: Database write operations SHALL use explicit `commit()` calls to ensure data durability.  
**Priority**: High  
**NFR-011**: The application SHALL log errors to stderr for diagnostic purposes but SHALL NOT expose internal error details to the user interface.  
**Priority**: Medium  

### 7.4 API Rate Limit Handling

**NFR-012**: The system SHALL detect 429 (Rate Limit) errors from both providers and display a user-friendly message: "API rate limit exceeded (429). Please wait 30-60 seconds then try again."  
**Priority**: High  
**NFR-013**: For Gemini 503 (Service Unavailable) errors, the system SHALL retry with exponential backoff (3 attempts).  
**Priority**: High  
**NFR-014**: For Groq 413 (Request Too Large) errors related to TPM limits, the system SHALL retry with backoff since the limit resets per minute.  
**Priority**: High  

### 7.5 Browser Compatibility

**NFR-015**: The system SHALL support the latest two major versions of Chrome, Firefox, Safari, and Edge.  
**Priority**: High  
**NFR-016**: The View Transition API SHALL be used as a progressive enhancement. Browsers that do not support it SHALL navigate directly without animation.  
**Priority**: Low  
**NFR-017**: The tab bar scroll-on-hover behavior SHALL be disabled on touch devices, detected via `matchMedia('(pointer: coarse)')`.  
**Priority**: Low  

### 7.6 Asset Management

**NFR-018**: All CSS, JavaScript, fonts, and icons SHALL be self-hosted in the `static/` directory. Zero external CDN calls SHALL be made.  
**Priority**: High  
**NFR-019**: Icons SHALL be rendered as inline SVG (Feather-style) to avoid external icon font dependencies.  
**Priority**: High  

---

## 8. AI Provider Specifications

### 8.1 Gemini Integration

**Endpoint**: `generativelanguage.googleapis.com` (standard AI Studio endpoint, not Vertex AI).  
**SDK**: `google-genai` version >= 2.10.0 with explicit `api_key=` parameter.  
**Model**: `gemini-2.5-flash`.  
**Configuration**:
- `response_mime_type`: `"application/json"`
- `response_schema`: RFPAnalysis Pydantic model (enforces structured output)
- `temperature`: 0.1 (minimal randomness for consistent output)
- `system_instruction`: Detailed prompt covering role, analysis requirements, field descriptions, item names, status rules, and scoring guidelines
- Document text truncated to 5000 characters

**Authentication**: API key (AQ. prefix format). The SDK does not auto-read environment variables in version 2.10.0; the key MUST be passed explicitly.

**Error Handling**:
- `ClientError`: Raised immediately without retry (auth/permission errors).
- `ServerError`: Retry up to 3 times with exponential backoff (1s, 2s, 4s).
- `Exception` (other): Retry up to 3 times with exponential backoff.

### 8.2 Groq Integration

**SDK**: `groq` Python package.  
**Model**: `llama-3.3-70b-versatile` (128K context window).  
**Configuration**:
- `response_format`: `{"type": "json_object"}` (enforces JSON output)
- `temperature`: 0.1
- `max_tokens`: 6000 (keeps total request under 12K TPM free tier limit)
- Document text truncated to 1000 characters

**Error Handling**:
- 400 errors: Raised immediately without retry (JSON validation failures).
- 413 errors: Retry with backoff (TPM rate limit, resets per minute).
- Other errors: Retry up to 3 times with exponential backoff.
- On JSON decode failure: Attempt recovery from `failed_generation` field.

**Post-Processing**:
- `_clean_json()`: Strips markdown fences, removes control characters, balances unmatched trailing quotes.
- `_extract_failed_json()`: Attempts JSON extraction from Groq error response bodies.

### 8.3 Retry and Fallback Strategy

| Error Condition | Action | Rationale |
|---|---|---|
| HTTP 400 (Bad Request) | Raise immediately | Invalid input, cannot succeed on retry |
| HTTP 413 (Payload Too Large) | Retry with backoff | TPM limit, resets per minute |
| HTTP 429 (Rate Limit) | Retry with backoff | Quota resets per minute or per day |
| HTTP 503 (Service Unavailable) | Retry with backoff | Temporary provider overload |
| JSON decode failure | Attempt `failed_generation` recovery, then retry | Partial output may be recoverable |
| Authentication error (401/403) | Raise immediately | Invalid or restricted API key |
| Network timeout | Retry with backoff | Transient connectivity issue |

---

## 9. Constraints and Limitations

### 9.1 Technical Constraints

| Constraint | Impact |
|---|---|
| xhtml2pdf 0.2.17 does not support SVG, flexbox, or CSS `@page` margin-box rules (`@bottom-left`, `@bottom-right`) | PDF templates MUST use table-based layout only. SVGs MUST be replaced with colored text/borders. Complex CSS selectors SHOULD be avoided. |
| Flask development server is single-threaded by default | Concurrent requests may block. Production deployments SHOULD use a WSGI server (gunicorn, waitress). |
| PyMuPDF (fitz) is imported but not used for text extraction | Available for future image-based PDF processing or vision model integration. |

### 9.2 API Service Constraints

| Constraint | Value | Impact |
|---|---|---|
| Gemini 2.5 Flash free tier | 20 requests per day per model | Limits daily analysis volume on free plan. Upgrade to paid tier for higher limits. |
| Groq free tier TPM | 12,000 tokens per minute | Limits per-minute throughput. Groq also limits TPD to 100,000 tokens. |
| Groq free tier TPD | 100,000 tokens per day | Limits daily analysis volume. Upgrade to dev tier for higher limits. |
| Groq model constraints | `openai/gpt-oss-120b` does not support `response_format=json_object` reliably | Model changed to `llama-3.3-70b-versatile` which supports JSON mode. |
| Gemini tokenization | `gemini-2.0-flash` and `gemini-2.0-flash-lite` free tier quota often exhausted | Default model changed to `gemini-2.5-flash` which may have separate quota. |
| Document text extraction | PDFs without extractable text (scanned images) return empty text | Requires OCR or vision-enabled model for image-based PDFs. |

**Configurable Constants:**

| Constant | Value | Location |
|---|---|---|
| `MAX_DOC_CHARS` | 5000 | `app.py` (Gemini truncation) |
| Groq document truncation | 1000 chars | `app.py` line 295 |
| Groq `max_tokens` | 6000 | `app.py` line 352 |
| Max upload file size | 50 MB | `app.py` line 39 and `index.html` line 601 |
| Max PDF pages extracted | 100 | `app.py` line 109 |
| History limit | 50 records (session), 20 records (fallback) | `app.py` lines 530-536 |
| API retry attempts | 3 | `app.py` line 239 (Gemini), line 342 (Groq) |

---

## 10. Appendices

### 10.1 Glossary

| Term | Definition |
|---|---|
| RFP | Request for Proposal — a formal document soliciting bids from potential vendors |
| Bid Qualification | Process of evaluating whether an organization should pursue a given RFP |
| Go/No-Go | Decision point: whether to proceed (Go) or decline (No-Go) a bid opportunity |
| TPM | Tokens Per Minute — API rate limit metric |
| TPD | Tokens Per Day — API rate limit metric |
| NET30 | Payment term: net 30 days from invoice date |
| Bid Bond | A financial guarantee ensuring the bidder will enter into the contract if awarded |
| E-Verify | U.S. electronic employment verification system |
| NIST 800-53 | National Institute of Standards and Technology security controls framework |
| SDG | Sustainable Development Goal (as referenced in some RFP evaluation criteria) |
| Quantum of Input | A measure of the expected scale, scope, or resource commitment required |
| Responsible Person | The individual designated as owner or lead for a specific requirement |
| xhtml2pdf | A Python library for converting HTML/CSS to PDF. Does not support modern CSS features like flexbox or SVG. |
| PyMuPDF (fitz) | A Python library for PDF manipulation, capable of handling both text and image-based PDFs |

### 10.2 Route Map

| Method | Route | Purpose | Template |
|---|---|---|---|
| GET | `/` | Upload page | `index.html` |
| POST | `/upload` | Analyze document | `results.html` |
| GET | `/history` | View analysis history | `history.html` |
| GET | `/view/<id>` | View specific analysis | `results.html` |
| POST | `/compare` | Compare two analyses | `compare.html` |
| POST | `/rename` | Rename analysis | (204 No Content) |
| POST | `/export_pdf` | Download PDF export | `report_template.html` |
| POST | `/export_json` | Download JSON export | (raw JSON) |

---

*Document prepared by Bilal Tech. For questions or corrections, contact the development team.*

*End of Software Requirements Specification*
