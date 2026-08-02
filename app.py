import os
import io
import sys
import re
import json
import uuid
import hashlib
import time
import base64
import random
import sqlite3
import requests as http_requests
from datetime import datetime
from collections import defaultdict
import html

import pypdf
import fitz
import docx
import groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    from openpyxl import Workbook
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

load_dotenv()

def _get_env(key: str) -> str:
    v = os.getenv(key, '')
    return v.strip().strip('"').strip("'") if v else v

from flask import Flask, render_template, request, make_response, jsonify, Response, session, g, url_for
from werkzeug.utils import secure_filename
from typing import List, Union
from google import genai
from google.genai import types
from xhtml2pdf import pisa

# Windows consoles default to cp1252; model output can contain characters it
# cannot encode (e.g. ≤, —). Replace them instead of raising UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(errors='replace')
        except Exception:
            pass

app = Flask(__name__)
# Persistent secret_key — survives restarts so session cookies stay valid
SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
if os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, 'r') as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = os.urandom(24).hex()
    with open(SECRET_KEY_FILE, 'w') as f:
        f.write(app.secret_key)

app.config['TEMPLATES_AUTO_RELOAD'] = True
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MAX_DOC_CHARS = 200000  # Truncation safety limit for LLM context windows
OPENROUTER_MAX_CHARS = 60000
OPENROUTER_DEFAULT_MODEL = os.getenv('OPENROUTER_MODEL', 'nvidia/nemotron-3-super-120b-a12b:free')
OPENROUTER_OCR_MODEL = os.getenv('OPENROUTER_OCR_MODEL', 'google/gemma-4-31b-it:free')
GROQ_OCR_MODEL = os.getenv('GROQ_OCR_MODEL', 'qwen/qwen3.6-27b')  # legacy — slot reserved for Module 4

# Cloudflare Workers AI — sole provider for Module 1 (requirements extraction).
# Free quota: 10,000 Neurons/day shared across all models on the account,
# resetting at 00:00 UTC. Neuron cost varies by model; smaller models stretch
# the budget further across more chunk calls.
CLOUDFLARE_ACCOUNT_ID = _get_env('CLOUDFLARE_ACCOUNT_ID')
CLOUDFLARE_API_TOKEN = _get_env('CLOUDFLARE_API_TOKEN')
CLOUDFLARE_REQ_MODEL = os.getenv('CLOUDFLARE_REQ_MODEL', '@cf/meta/llama-3.1-8b-instruct')
# Using @cf/meta/llama-3.1-8b-instruct — efficient 8B model with good JSON
# instruction following via prefix seeding. Smaller/faster than 70B+ models,
# so daily Neuron budget stretches across more chunk calls.
CLOUDFLARE_EMBEDDING_MODEL = os.getenv('CLOUDFLARE_EMBEDDING_MODEL', '@cf/baai/bge-base-en-v1.5')
# @cf/baai/bge-base-en-v1.5 — 768-dim output, shared vector space for both
# company facts and requirement embeddings in Module 2's hybrid retrieval.
CLOUDFLARE_VERIFICATION_MODEL = os.getenv('CLOUDFLARE_VERIFICATION_MODEL', '@cf/meta/llama-3.1-8b-instruct')
# Using the same 8B instruct model for Module 2 verification — good JSON
# instruction following, same shared Neuron budget as extraction.

# Module 4 — Smart Content Reuse Engine (separate Groq account from Module 3)
# This key belongs to a different Groq account and MUST NOT be shared with or
# substituted for Module 3's key. The isolation ensures independent rate-limit
# budgets for the two modules.
MODULE4_GROQ_ENABLED = True
_MODULE4_GROQ_KEY = _get_env('GROQ_MODULE4_API_KEY')

# --- SQLite Database for persistent history ---
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'analyses.db')

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'gemini',
                timestamp TEXT NOT NULL,
                hash TEXT NOT NULL,
                word_count INTEGER DEFAULT 0,
                results TEXT NOT NULL,
                session_id TEXT NOT NULL
            )
        ''')
        # Module 6 (Amendment / Version Delta Tracking): case_id links an upload
        # to the original analysis it amends; delta stores the structured diff +
        # generated summary produced against the most recent prior case analysis.
        cols = {row[1] for row in conn.execute('PRAGMA table_info(analyses)').fetchall()}
        for col, ddl in (('case_id', 'TEXT'), ('delta', 'TEXT')):
            if col not in cols:
                conn.execute(f'ALTER TABLE analyses ADD COLUMN {col} {ddl}')
        conn.commit()

init_db()

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

class GoNoGoReason(BaseModel):
    factor: str = Field(description="Factor or criterion being evaluated (e.g. Strategic Alignment, Resource Availability, Competitive Position)")
    detail: str = Field(description="Why this factor supports or opposes pursuing the RFP")
    weight: Union[str, int] = Field(description="Weight: High, Medium, or Low (or numeric 10/20/30)")

class GoNoGo(BaseModel):
    score: int = Field(description="Overall pursuit score from 0 to 100, where 0 = definite no-go and 100 = definite go")
    verdict: str = Field(description="One of: Go, Caution, No-Go")
    summary: str = Field(description="One-sentence summary of the go/no-go recommendation")
    reasons: List[GoNoGoReason] = Field(description="List of 4-8 factors that influenced the score")

class ChecklistItem(BaseModel):
    item: str = Field(description="The specific requirement or checklist item evaluated")
    status: str = Field(description="One of: Go, No-Go, Escalate, Review, Caution, Not Specified in RFP")
    reasoning: str = Field(description="Brief clear explanation for the status based on RFP content")
    what_rfp_says: str = Field(description="What the RFP document actually states or implies about this item. Quote key phrases if available. If not mentioned, state 'Not addressed in RFP.'")
    rfp_evidence: str = Field(description="Specific evidence from the RFP document: direct quotes, section numbers, clause references, page numbers, exhibit references, or specific dollar amounts and deadlines mentioned. This must be grounded in the actual document text.")
    impact_on_bid_strategy: str = Field(description="How this checklist item directly impacts our bid approach: pricing strategy, resource allocation, team composition, partnership needs, timeline implications, competitive positioning, or go/no-go decision.")
    risk_level: str = Field(description="Risk level this item poses to bid success: High (could disqualify or cause significant loss), Medium (requires careful management), Low (manageable or standard requirement).")
    mitigation_strategy: str = Field(description="Specific actionable steps to reduce or manage the risk associated with this item. Include who should handle it, what preparation is needed, and contingency plans if applicable.")
    analysis: str = Field(description="Deep analysis of implications, risks, and strategic considerations. Connect this item to overall bid qualification, competitive landscape, resource feasibility, and financial impact.")
    recommendation: str = Field(description="Specific actionable recommendation: what to do next, who to involve, what to prepare, or why to proceed or decline.")

class StrategicChecklist(BaseModel):
    executive_summary: str = Field(description="Overall recommendation summary with Go/No-Go/Escalate verdict and key reasoning")
    financial: List[ChecklistItem] = Field(description="Financial/Accounting checklist evaluation covering payment terms, financial stability, insurance, profitability, bid bond")
    legal: List[ChecklistItem] = Field(description="Legal checklist evaluation covering eligibility, capability, compliance, state registration, e-verify, contractual obligations")
    operations: List[ChecklistItem] = Field(description="Operations checklist evaluation covering required forms, deadlines, document compliance, signatory authority, vendor registration")
    technical: List[ChecklistItem] = Field(description="Technical checklist evaluation covering scope alignment, technical requirements, industry standards, security, integration needs")

class RiskItem(BaseModel):
    category: str = Field(description="Risk category (e.g. Financial, Timeline, Technical, Legal)")
    description: str = Field(description="Description of the specific risk")
    severity: str = Field(description="Severity: High, Medium, or Low")

class TimelineMilestone(BaseModel):
    milestone: str = Field(description="Milestone or deadline mentioned")
    date_reference: str = Field(description="Date or timeframe reference from the document")

class ComplianceChecklist(BaseModel):
    Financial: str = Field(description="Requirements regarding payment terms, financial stability, insurance limits, profitability, or bid bonds.")
    Legal: str = Field(description="Requirements regarding eligibility, capability, quantum of input, compliance, state registration, e-verify, or contractual obligations.")
    Operations: str = Field(description="Requirements regarding required forms, submission deadlines, document compliance, signatory authority, or vendor registration.")
    Technical: str = Field(description="Requirements regarding scope of services, technical specifications, industry standards, security, or integration.")

class SubDeliverable(BaseModel):
    model_config = {'coerce_numbers_to_str': True}
    name: str = Field(description="Name of the sub-deliverable, task, or output")
    reference: str = Field(description="What the RFP document actually says about this sub-deliverable — direct quote, section number, clause reference, or paraphrase. Must be grounded in the document text.")
    page_number: str = Field(default="", description="Exact page number from the document (e.g. '5' or '12-15'). Empty string if not available.")

class DeliverableGroup(BaseModel):
    model_config = {'coerce_numbers_to_str': True}
    deliverable: str = Field(description="Parent deliverable or category name representing a major output or work product")
    sub_deliverables: List[SubDeliverable] = Field(description="Specific sub-items, components, tasks, or outputs under this parent deliverable, each with a name and RFP reference")
    reference: str = Field(description="What the RFP document actually says about this deliverable — direct quote, section number, clause reference, or paraphrase with page/paragraph location. Must be grounded in the document text.")
    page_number: str = Field(default="", description="Exact page number from the document (e.g. '5' or '12-15'). Empty string if not available.")

class RFPAnalysis(BaseModel):
    model_config = {'coerce_numbers_to_str': True, 'extra': 'ignore'}
    summary: str = Field(description="Executive summary of the RFP in 2-3 sentences")
    deliverables: List[DeliverableGroup] = Field(description="Grouped deliverables with parent items and sub-deliverables. Thoroughly extract every deliverable from the RFP.")
    evaluation_criteria: List[str] = Field(description="Summary of scoring metrics, point allocations, or judgment guidelines.")
    compliance: ComplianceChecklist
    risks: List[RiskItem] = Field(description="Key risks identified in the RFP")
    timeline: List[TimelineMilestone] = Field(description="Important dates, milestones, and deadlines")
    key_requirements: List[str] = Field(description="Top 5 most important requirements from the RFP")
    go_nogo: GoNoGo = Field(description="Go/No-Go recommendation with score, verdict, summary, and supporting reasons")
    strategic_checklist: StrategicChecklist = Field(description="Strategic RFP checklist evaluation across Financial, Legal, Operations, and Technical categories")




# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — Knowledge-Base-Grounded Compliance Verifier
# ═══════════════════════════════════════════════════════════════════════════════

COMPANY_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'company_profile.json')
COMPANY_EMBEDDINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'company_profile_embeddings.json')

DEFAULT_COMPANY_PROFILE = {
    "company_name": "",
    "certifications": [
        {"name": "", "number": "", "expiry": "", "scope": ""}
    ],
    "insurance": [
        {"type": "", "coverage_amount": 0, "provider": "", "policy_number": "", "expiry": ""}
    ],
    "payment_terms": {
        "standard_terms": "NET30",
        "accepted_terms": ["NET30"],
        "notes": ""
    },
    "past_performance": [
        {"client": "", "project": "", "value": 0, "start_date": "", "end_date": "", "description": "", "contact": ""}
    ],
    "technical_capabilities": [
        {"name": "", "description": "", "technologies": [], "certifications_required": []}
    ],
    "financial_standing": {
        "annual_revenue": 0,
        "years_in_business": 0,
        "duns_number": "",
        "bonding_capacity": 0,
        "credit_rating": ""
    },
    "personnel": [
        {"name": "", "role": "", "certifications": [], "experience_years": 0}
    ]
}


def _load_company_profile() -> dict:
    if os.path.exists(COMPANY_PROFILE_PATH):
        with open(COMPANY_PROFILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[PROFILE] Loaded profile from {COMPANY_PROFILE_PATH}: company_name={data.get('company_name', '')!r}")
        return data
    print(f"[PROFILE] No profile file at {COMPANY_PROFILE_PATH}, returning defaults")
    return dict(DEFAULT_COMPANY_PROFILE)


def _save_company_profile(profile: dict):
    with open(COMPANY_PROFILE_PATH, 'w', encoding='utf-8') as f:
        json.dump(profile, f, indent=2)
    print(f"[PROFILE] Saved profile to {COMPANY_PROFILE_PATH}: company_name={profile.get('company_name', '')!r}")
    _EMBEDDING_CACHE.clear()
    if os.path.exists(COMPANY_EMBEDDINGS_PATH):
        os.remove(COMPANY_EMBEDDINGS_PATH)


def _flatten_profile_to_facts(profile: dict) -> list[dict]:
    facts = []
    fid = 0

    for cert in profile.get('certifications', []):
        if cert.get('name'):
            text = f"Certification: {cert['name']}"
            if cert.get('number'):
                text += f", Number: {cert['number']}"
            if cert.get('expiry'):
                text += f", Expires: {cert['expiry']}"
            if cert.get('scope'):
                text += f", Scope: {cert['scope']}"
            facts.append({"id": f"cert_{fid}", "text": text, "category": "certifications", "raw": cert})
            fid += 1

    for ins in profile.get('insurance', []):
        if ins.get('type'):
            amt = ins.get('coverage_amount', 0)
            text = f"Insurance: {ins['type']}, Coverage: ${amt:,.0f}"
            if ins.get('provider'):
                text += f", Provider: {ins['provider']}"
            if ins.get('expiry'):
                text += f", Expires: {ins['expiry']}"
            facts.append({"id": f"ins_{fid}", "text": text, "category": "insurance", "raw": ins})
            fid += 1

    pt = profile.get('payment_terms', {})
    if pt.get('standard_terms'):
        text = f"Payment terms: Standard is {pt['standard_terms']}"
        if pt.get('accepted_terms'):
            text += f", Accepted: {', '.join(pt['accepted_terms'])}"
        facts.append({"id": f"pt_{fid}", "text": text, "category": "payment_terms", "raw": pt})
        fid += 1

    for pp in profile.get('past_performance', []):
        if pp.get('project') or pp.get('client'):
            text = f"Past performance: {pp.get('client', 'Unknown')} — {pp.get('project', '')}"
            if pp.get('value'):
                text += f", Value: ${pp['value']:,.0f}"
            if pp.get('description'):
                text += f", {pp['description']}"
            facts.append({"id": f"pp_{fid}", "text": text, "category": "past_performance", "raw": pp})
            fid += 1

    for tc in profile.get('technical_capabilities', []):
        if tc.get('name'):
            text = f"Technical capability: {tc['name']}"
            if tc.get('description'):
                text += f" — {tc['description']}"
            if tc.get('technologies'):
                text += f", Technologies: {', '.join(tc['technologies'])}"
            facts.append({"id": f"tc_{fid}", "text": text, "category": "technical_capabilities", "raw": tc})
            fid += 1

    fs = profile.get('financial_standing', {})
    if fs.get('annual_revenue') or fs.get('years_in_business'):
        text = f"Financial standing: Annual revenue ${fs.get('annual_revenue', 0):,.0f}, {fs.get('years_in_business', 0)} years in business"
        if fs.get('bonding_capacity'):
            text += f", Bonding capacity: ${fs['bonding_capacity']:,.0f}"
        if fs.get('credit_rating'):
            text += f", Credit rating: {fs['credit_rating']}"
        facts.append({"id": f"fs_{fid}", "text": text, "category": "financial_standing", "raw": fs})
        fid += 1

    for p in profile.get('personnel', []):
        if p.get('name'):
            text = f"Personnel: {p['name']}, Role: {p.get('role', '')}"
            if p.get('certifications'):
                text += f", Certifications: {', '.join(p['certifications'])}"
            if p.get('experience_years'):
                text += f", Experience: {p['experience_years']} years"
            facts.append({"id": f"per_{fid}", "text": text, "category": "personnel", "raw": p})
            fid += 1

    return facts


# ── Embedding cache ──
_EMBEDDING_CACHE = {}
_EMBEDDING_CACHE_MODEL = None  # checked on first load to invalidate stale caches


def _embed_cache_key(text: str) -> str:
    """Model-aware cache key: different models produce different vector spaces."""
    return hashlib.md5(f"{CLOUDFLARE_EMBEDDING_MODEL}:{text}".encode()).hexdigest()


def _load_embedding_cache():
    global _EMBEDDING_CACHE, _EMBEDDING_CACHE_MODEL
    if _EMBEDDING_CACHE:
        return
    if os.path.exists(COMPANY_EMBEDDINGS_PATH):
        with open(COMPANY_EMBEDDINGS_PATH, 'r', encoding='utf-8') as f:
            _EMBEDDING_CACHE = json.load(f)
            _EMBEDDING_CACHE_MODEL = _EMBEDDING_CACHE.pop('__model__', None)
        # If model changed (e.g. Gemini → Cloudflare or Cloudflare model change),
        # discard all cached embeddings — different vector space.
        if _EMBEDDING_CACHE_MODEL != CLOUDFLARE_EMBEDDING_MODEL:
            n = len(_EMBEDDING_CACHE)
            _EMBEDDING_CACHE = {}
            _EMBEDDING_CACHE['__model__'] = CLOUDFLARE_EMBEDDING_MODEL
            if n:
                print(f"[EMBED] Model change {_EMBEDDING_CACHE_MODEL!r} → {CLOUDFLARE_EMBEDDING_MODEL!r}, discarded {n} stale embeddings")


def _save_embedding_cache():
    _EMBEDDING_CACHE['__model__'] = CLOUDFLARE_EMBEDDING_MODEL
    with open(COMPANY_EMBEDDINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(_EMBEDDING_CACHE, f)


def _embed_text(text: str) -> list[float]:
    """Embed a single text via Cloudflare Workers AI (uses batch internally)."""
    results = _embed_texts_batch([text])
    return results[0] if results and results[0] else []


def _embed_texts_batch(texts: list[str]) -> list[list[float]]:
    """Embed texts via Cloudflare Workers AI. Falls back to BM25 if Cloudflare is unavailable.

    Both embedding call sites (company facts, requirement texts) go through this
    single function, ensuring they use the same model and vector space.
    """
    _load_embedding_cache()
    results: list[list[float] | None] = []
    to_embed: list[str] = []
    to_embed_indices: list[int] = []
    for i, text in enumerate(texts):
        text_hash = _embed_cache_key(text)
        if text_hash in _EMBEDDING_CACHE:
            results.append(_EMBEDDING_CACHE[text_hash])
        else:
            results.append(None)
            to_embed.append(text)
            to_embed_indices.append(i)
    if not to_embed:
        return results

    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        print("[EMBED] Cloudflare not configured — cannot embed, falling back to BM25 only")
        return results

    model = CLOUDFLARE_EMBEDDING_MODEL
    url = f'https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model}'
    headers = {
        'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
        'Content-Type': 'application/json',
    }
    try:
        resp = http_requests.post(url, headers=headers, json={'text': to_embed}, timeout=120)
        if resp.status_code == 429:
            print(f"[CLOUDFLARE] Embedding rate limited (429). The daily 10,000 Neuron quota may be exhausted — falling back to BM25 only")
            return results
        if resp.status_code != 200:
            print(f"[EMBED] Cloudflare HTTP {resp.status_code} — falling back to BM25 only")
            return results
        body = resp.json()
        if body.get('success') is not True:
            err = body.get('errors', 'unknown')
            print(f"[EMBED] Cloudflare API error: {err} — falling back to BM25 only")
            return results
        data = body.get('result', {}).get('data', [])
        if not data:
            print("[EMBED] Cloudflare returned empty data — falling back to BM25 only")
            return results
        dims = len(data[0]) if data else 0
        print(f"[CLOUDFLARE] Embedded {len(data)} texts via {model} ({dims}-dim) — shared Neuron budget with extraction")
        for j, embedding in enumerate(data):
            if j < len(to_embed_indices):
                text_hash = _embed_cache_key(to_embed[j])
                _EMBEDDING_CACHE[text_hash] = embedding
                results[to_embed_indices[j]] = embedding
        _save_embedding_cache()
    except Exception as e:
        print(f"[EMBED] Cloudflare embedding failed: {e} — falling back to BM25 only")
    return results


# ── Lightweight BM25 (Okapi) ──
import math
import string as _string

_STOP_WORDS = frozenset({'the','a','an','is','are','was','were','be','been','being',
    'have','has','had','do','does','did','will','would','could','should','may',
    'might','shall','can','to','of','in','for','on','with','at','by','from',
    'as','into','through','during','before','after','above','below','between',
    'out','off','over','under','again','further','then','once','here','there',
    'when','where','why','how','all','both','each','few','more','most','other',
    'some','such','no','nor','not','only','own','same','so','than','too','very',
    'just','and','but','or','if','because','about','up','that','this','these',
    'those','it','its','i','me','my','we','our','you','your','he','him','his',
    'she','her','they','them','their','what','which','who','whom'})


def _tokenize(text: str) -> list[str]:
    text = text.lower().translate(str.maketrans('', '', _string.punctuation))
    return [t for t in text.split() if t and t not in _STOP_WORDS and len(t) > 1]


class _BM25:
    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus_tokens = [_tokenize(doc) for doc in corpus]
        self.doc_count = len(corpus)
        self.avg_dl = sum(len(t) for t in self.corpus_tokens) / max(self.doc_count, 1)
        self.df: dict[str, int] = {}
        for tokens in self.corpus_tokens:
            unique = set(tokens)
            for t in unique:
                self.df[t] = self.df.get(t, 0) + 1

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.doc_count - df + 0.5) / (df + 0.5) + 1)

    def score(self, query: str) -> list[float]:
        q_tokens = _tokenize(query)
        scores = []
        for doc_tokens in self.corpus_tokens:
            dl = len(doc_tokens)
            tf_map: dict[str, int] = {}
            for t in doc_tokens:
                tf_map[t] = tf_map.get(t, 0) + 1
            score = 0.0
            for qt in q_tokens:
                tf = tf_map.get(qt, 0)
                idf = self._idf(qt)
                norm = 1 - self.b + self.b * dl / max(self.avg_dl, 1)
                score += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
            scores.append(score)
        return scores


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _hybrid_retrieve(requirement_text: str, facts: list[dict],
                     fact_embeddings: list[list[float]],
                     req_embedding: list[float] | None = None,
                     top_k: int = 5) -> list[dict]:
    if not facts:
        return []
    corpus_texts = [f['text'] for f in facts]
    bm25 = _BM25(corpus_texts)
    bm25_scores = bm25.score(requirement_text)

    # Embedding-based scores from pre-computed vectors (cached or freshly embedded).
    # req_embedding and fact_embeddings are both Cloudflare BGE (same model/vector space).
    if req_embedding is not None and any(fe is not None for fe in fact_embeddings):
        emb_scores = [_cosine_similarity(req_embedding, fe) if fe else 0.0 for fe in fact_embeddings]
    else:
        emb_scores = [0.0] * len(facts)

    max_bm25 = max(bm25_scores) if bm25_scores else 1.0
    max_emb = max(emb_scores) if emb_scores else 1.0
    if max_bm25 == 0:
        max_bm25 = 1.0
    if max_emb == 0:
        max_emb = 1.0

    combined = []
    for i in range(len(facts)):
        norm_bm25 = bm25_scores[i] / max_bm25
        norm_emb = emb_scores[i] / max_emb
        combined_score = 0.5 * norm_bm25 + 0.5 * norm_emb
        combined.append((combined_score, i))

    combined.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, idx in combined[:top_k]:
        entry = dict(facts[idx])
        entry['relevance_score'] = round(score, 4)
        results.append(entry)
    return results


# ── Hard-coded rules ──
def _parse_amount(text: str) -> float | None:
    m = re.search(r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|M|billion|B))?|\d[\d,]*(?:\.\d+)?(?:\s*(?:million|M|billion|B))?', text, re.IGNORECASE)
    if not m:
        return None
    s = m.group(0).replace('$', '').replace(',', '').strip()
    multiplier = 1.0
    if re.search(r'million\b|M\b', s, re.IGNORECASE):
        multiplier = 1_000_000
        s = re.sub(r'(?:million|M)\b', '', s, flags=re.IGNORECASE).strip()
    elif re.search(r'billion\b|B\b', s, re.IGNORECASE):
        multiplier = 1_000_000_000
        s = re.sub(r'(?:billion|B)\b', '', s, flags=re.IGNORECASE).strip()
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def _apply_hardcoded_rules(requirement: dict, company_profile: dict) -> dict | None:
    desc = (requirement.get('description', '') + ' ' + requirement.get('item', '')).lower()
    category = requirement.get('category', '').lower()
    rfp_evidence = requirement.get('rfp_evidence', '') or requirement.get('description', '')

    if ('payment' in desc and 'term' in desc) or (category == 'financial' and 'payment' in desc):
        pt = company_profile.get('payment_terms', {})
        standard = (pt.get('standard_terms', '') or '').upper()
        accepted = [t.upper() for t in (pt.get('accepted_terms', []) or [])]
        if 'NET30' in desc or 'net 30' in desc or 'net30' in desc:
            return {
                'status': 'Go',
                'reasoning': f"RFP requests NET30 payment terms. Company standard is {standard}, which meets the requirement.",
                'company_evidence': f"Company payment terms: {standard}. Accepted: {', '.join(accepted)}.",
                'risk_level': 'Low',
                'mitigation_strategy': 'Standard terms — no mitigation needed.',
                'impact_on_bid_strategy': 'No impact. NET30 aligns with company standard.',
            }
        for term_match in re.finditer(r'net[\s-]?(\d+)', desc):
            days = int(term_match.group(1))
            if days > 30:
                return {
                    'status': 'Escalate',
                    'reasoning': f"RFP requests NET{days} payment terms, which exceeds company standard of {standard}. Requires finance approval.",
                    'company_evidence': f"Company standard: {standard}. RFP requires NET{days}.",
                    'risk_level': 'Medium',
                    'mitigation_strategy': f"Escalate to finance for approval of NET{days} terms. Assess cash flow impact.",
                    'impact_on_bid_strategy': f"NET{days} extends payment cycle by {days - 30} days beyond standard, impacting cash flow projections.",
                }
        return {
            'status': 'Go',
            'reasoning': f"Payment terms alignment: company standard is {standard}.",
            'company_evidence': f"Company payment terms: {standard}.",
            'risk_level': 'Low',
            'mitigation_strategy': 'Confirm terms during contract negotiation.',
            'impact_on_bid_strategy': 'Minimal impact assuming standard terms.',
        }

    if ('insurance' in desc and ('requirement' in desc or 'coverage' in desc or 'liability' in desc)) or \
       (category == 'financial' and 'insurance' in desc) or \
       (category == 'legal' and 'insurance' in desc):
        req_amount = None
        for pattern in [r'(?:coverage|insurance|liability)\s*(?:of|up\s*to|amount)?\s*\$?([\d,]+(?:\.\d+)?)\s*(?:million|M|billion|B)?',
                        r'\$?([\d,]+(?:\.\d+)?)\s*(?:million|M)\b',
                        r'(?:minimum|required|must\s*have)\s*(?:of\s*)?\$?([\d,]+(?:\.\d+)?)']:
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                req_amount = _parse_amount(m.group(0))
                break
        if req_amount is None:
            req_amount = _parse_amount(rfp_evidence)
        if req_amount is not None:
            if req_amount <= 5_000_000:
                return {
                    'status': 'Go',
                    'reasoning': f"Required insurance coverage is ${req_amount:,.0f} (≤ $5M). Company meets this threshold.",
                    'company_evidence': f"Company has insurance coverage that meets or exceeds ${req_amount:,.0f}.",
                    'risk_level': 'Low',
                    'mitigation_strategy': 'Verify specific insurance type matches RFP requirement.',
                    'impact_on_bid_strategy': 'No pricing impact. Standard coverage sufficient.',
                }
            else:
                return {
                    'status': 'No-Go',
                    'reasoning': f"Required insurance coverage is ${req_amount:,.0f} (> $5M). Exceeds standard company coverage.",
                    'company_evidence': f"Company standard insurance coverage is below ${req_amount:,.0f}. Additional coverage would be required.",
                    'risk_level': 'High',
                    'mitigation_strategy': 'Obtain additional insurance quotes or decline to bid. If pursuing, negotiate lower coverage or find partner with higher coverage.',
                    'impact_on_bid_strategy': 'Significant cost increase for additional coverage. May make bid financially unviable.',
                }

    return None


# ── Verification prompt ──
_VERIFICATION_PROMPT = """You are a compliance verifier for an RFP bid. You are given:
1. A requirement from the RFP document
2. Company profile facts retrieved from the company's knowledge base

Your task: Verify whether the company can MEET this requirement based on the company facts provided.

IMPORTANT RULES:
- If no company facts are relevant to this requirement, set status to "Not Specified in RFP"
- Do NOT guess or assume capabilities not supported by the facts
- Be specific about which company fact supports your assessment

Return a JSON object with EXACTLY these fields:
{
  "req_id": "{req_id}",
  "status": "Go|No-Go|Escalate|Review|Caution|Not Specified in RFP",
  "reasoning": "Brief explanation based on company facts",
  "rfp_evidence": "Quote or paraphrase from the RFP requirement",
  "company_evidence": "Which specific company profile fact supports this assessment",
  "risk_level": "High|Medium|Low",
  "mitigation_strategy": "Specific steps to mitigate any identified risks",
  "impact_on_bid_strategy": "How this affects our bid approach"
}

Return ONLY valid JSON. No markdown, no code fences, no explanation."""


def _verify_single_requirement(requirement: dict, retrieved_facts: list[dict],
                               company_profile: dict, provider: str) -> dict:
    req_id = requirement.get('req_id', 'UNKNOWN')
    description = requirement.get('description', '')
    rfp_evidence = requirement.get('rfp_evidence', '') or description
    item_name = requirement.get('item', '')

    hardcoded = _apply_hardcoded_rules(
        {'description': description, 'item': item_name, 'category': requirement.get('category', ''),
         'rfp_evidence': rfp_evidence},
        company_profile
    )
    if hardcoded:
        hardcoded['req_id'] = req_id
        hardcoded['rfp_evidence'] = rfp_evidence
        return hardcoded

    if not retrieved_facts:
        return {
            'req_id': req_id,
            'status': 'Not Specified in RFP',
            'reasoning': 'No matching company profile facts found for this requirement.',
            'rfp_evidence': rfp_evidence,
            'company_evidence': '',
            'risk_level': 'Medium',
            'mitigation_strategy': 'Review company capabilities against this requirement manually.',
            'impact_on_bid_strategy': 'Cannot assess without relevant company data.',
        }

    facts_text = '\n'.join(f"- {f['text']}" for f in retrieved_facts)
    prompt = _VERIFICATION_PROMPT.replace('{req_id}', req_id)
    user_msg = f"Requirement: {item_name or description}\n\nRFP Evidence: {rfp_evidence}\n\nCompany Facts:\n{facts_text}"

    gemini_key = _get_env('GEMINI_API_KEY') or _get_env('GEMINI_FALLBACK_API_KEY')
    openrouter_key = _get_env('OPENROUTER_API_KEY')

    if CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID:
        raw = _call_cloudflare_verification(prompt, user_msg, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID)
    elif provider == 'openrouter' and openrouter_key:
        raw = _call_openrouter_verification(prompt, user_msg, openrouter_key)
    elif gemini_key:
        raw = _call_gemini_verification(prompt, user_msg, gemini_key)
    elif openrouter_key:
        raw = _call_openrouter_verification(prompt, user_msg, openrouter_key)
    else:
        return {
            'req_id': req_id,
            'status': 'Not Specified in RFP',
            'reasoning': 'No AI provider available for verification.',
            'rfp_evidence': rfp_evidence,
            'company_evidence': '',
            'risk_level': 'Medium',
            'mitigation_strategy': 'Manual review required.',
            'impact_on_bid_strategy': 'Cannot assess automatically.',
        }

    parsed = parse_resilient_json(raw)
    if isinstance(parsed, dict):
        parsed.setdefault('req_id', req_id)
        parsed.setdefault('status', 'Review')
        parsed.setdefault('reasoning', '')
        parsed.setdefault('rfp_evidence', rfp_evidence)
        parsed.setdefault('company_evidence', '')
        parsed.setdefault('risk_level', 'Medium')
        parsed.setdefault('mitigation_strategy', '')
        parsed.setdefault('impact_on_bid_strategy', '')
        return parsed

    return {
        'req_id': req_id,
        'status': 'Review',
        'reasoning': 'Verification response could not be parsed.',
        'rfp_evidence': rfp_evidence,
        'company_evidence': '',
        'risk_level': 'Medium',
        'mitigation_strategy': 'Manual review required.',
        'impact_on_bid_strategy': 'Cannot assess automatically.',
    }


def _call_gemini_verification(system_prompt: str, user_msg: str, api_key: str) -> str:
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=f"{system_prompt}\n\n{user_msg}",
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=4096),
        )
        raw = response.text or "{}"
        if raw is None and response.candidates:
            for c in response.candidates:
                if c.content and c.content.parts:
                    raw = ''.join(getattr(p, 'text', '') or '' for p in c.content.parts)
                    if raw:
                        break
        return raw or "{}"
    except Exception as e:
        print(f"[MODULE2-GEMINI] Failed: {e}")
        return "{}"


def _call_openrouter_verification(system_prompt: str, user_msg: str, api_key: str) -> str:
    url = 'https://openrouter.ai/api/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'http://localhost:5000',
        'X-OpenRouter-Title': 'RFP Automation Portal',
    }
    payload = {
        'model': OPENROUTER_DEFAULT_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_msg},
        ],
        'response_format': {'type': 'json_object'},
        'temperature': 0.1,
        'max_tokens': 4096,
    }
    max_retries = 4
    for attempt in range(max_retries + 1):
        try:
            resp = http_requests.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 429:
                body = resp.json()
                err_msg = (body.get('error', {}) or {}).get('message', '') or ''
                # Hard daily limit — retrying won't help, fail immediately
                if 'free-models-per-day' in err_msg or 'credits' in err_msg.lower():
                    print(f'[MODULE2-OR] Free model daily limit exhausted — stopping further verification calls')
                    return '{}'
                if attempt < max_retries:
                    wait = (2 ** attempt) + random.random()
                    print(f'[MODULE2-OR] 429 rate limit (attempt {attempt+1}/{max_retries+1}), retrying in {wait:.1f}s')
                    time.sleep(wait)
                    continue
            if resp.status_code != 200:
                print(f'[MODULE2-OR] HTTP {resp.status_code}: {resp.text[:200]}')
                return '{}'
            body = resp.json()
            return (body.get('choices', [{}])[0].get('message', {}).get('content') or '{}')
        except Exception as e:
            if attempt < max_retries:
                wait = (2 ** attempt) + random.random()
                print(f'[MODULE2-OR] Failed (attempt {attempt+1}/{max_retries+1}): {e}, retrying in {wait:.1f}s')
                time.sleep(wait)
                continue
            print(f'[MODULE2-OR] Failed after {max_retries+1} attempts: {e}')
            return '{}'
    return '{}'


def _verify_all_requirements(requirements: list[dict], company_profile: dict, provider: str) -> dict:
    facts = _flatten_profile_to_facts(company_profile)
    fact_texts = [f['text'] for f in facts]

    fact_embeddings = _embed_texts_batch(fact_texts) if fact_texts else []

    # Batch-embed all requirement search texts once
    req_texts = []
    for req in requirements:
        description = req.get('description', '')
        item_name = req.get('item', '')
        req_texts.append(f"{item_name} {description}".strip())
    req_embeddings = _embed_texts_batch(req_texts) if req_texts else []

    results = {}
    for i, req in enumerate(requirements):
        req_id = req.get('req_id', f'REQ-{i+1:03d}')
        search_text = req_texts[i]

        print(f"[MODULE2] Verifying {req_id}: {search_text[:80]}...")
        retrieved = _hybrid_retrieve(
            search_text, facts, fact_embeddings,
            req_embedding=req_embeddings[i] if i < len(req_embeddings) else None,
            top_k=5,
        )
        verification = _verify_single_requirement(req, retrieved, company_profile, provider)
        results[req_id] = verification

    print(f"[MODULE2] Verified {len(results)} requirements")
    return results


# ── Verify checklist items specifically (uses the 37-item strategic_checklist) ──
def _verify_checklist_items(checklist: dict, company_profile: dict, provider: str) -> dict:
    all_items = []
    for cat in ('financial', 'legal', 'operations', 'technical'):
        for item in checklist.get(cat, []):
            if isinstance(item, dict) and item.get('item'):
                all_items.append({
                    'req_id': f"CHK-{cat[:3].upper()}-{len(all_items)+1:03d}",
                    'item': item['item'],
                    'description': item.get('reasoning', '') or item.get('item', ''),
                    'category': cat.capitalize(),
                    'rfp_evidence': item.get('rfp_evidence', ''),
                })

    if not all_items:
        return {}

    facts = _flatten_profile_to_facts(company_profile)
    fact_texts = [f['text'] for f in facts]

    fact_embeddings = _embed_texts_batch(fact_texts) if fact_texts else []

    # Batch-embed all checklist search texts once
    item_texts = [f"{item['item']} {item['description']}" for item in all_items]
    item_embeddings = _embed_texts_batch(item_texts) if item_texts else []

    results = {}
    for i, item in enumerate(all_items):
        req_id = item['req_id']
        search_text = item_texts[i]
        retrieved = _hybrid_retrieve(
            search_text, facts, fact_embeddings,
            req_embedding=item_embeddings[i] if i < len(item_embeddings) else None,
            top_k=5,
        )
        verification = _verify_single_requirement(item, retrieved, company_profile, provider)
        results[req_id] = verification

    return results


def _ocr_pdf_page(file_path: str, page_num: int, provider: str = 'gemini') -> str:
    """Render a single PDF page to image via PyMuPDF, then OCR via Groq Llama vision only."""
    groq_key = _get_env('GROQ_MODULE4_API_KEY')
    if groq_key:
        result = _ocr_pdf_page_groq(file_path, page_num)
        if result:
            return result
    return ""


def _ocr_pdf_page_gemini(file_path: str, page_num: int) -> str:
    """Render a single PDF page to image via PyMuPDF, then OCR via Gemini vision."""
    gemini_key = _get_env('GEMINI_API_KEY')
    if not gemini_key:
        return ""
    try:
        doc = fitz.open(file_path)
        if page_num >= len(doc):
            doc.close()
            return ""
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=72)
        img_bytes = pix.tobytes('png')
        doc.close()

        client = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=[{
                'inline_data': {'mime_type': 'image/png', 'data': base64.b64encode(img_bytes).decode()}
            }, 'Extract ALL text from this image exactly as it appears. Return only the text, no commentary.'],
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=8192),
        )
        return (response.text or "").strip()
    except Exception as e:
        print(f"[OCR] Gemini vision failed on page {page_num}: {e}")
        return ""


def _ocr_pdf_page_openrouter(file_path: str, page_num: int) -> str:
    """Render a single PDF page to image via PyMuPDF, then OCR via OpenRouter vision model."""
    api_key = _get_env('OPENROUTER_API_KEY')
    if not api_key:
        return ""
    try:
        doc = fitz.open(file_path)
        if page_num >= len(doc):
            doc.close()
            return ""
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=72)
        img_bytes = pix.tobytes('png')
        doc.close()

        import requests as http_requests
        b64_image = base64.b64encode(img_bytes).decode()
        resp = http_requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'http://localhost:5000',
                'X-OpenRouter-Title': 'RFP Automation Portal',
            },
            json={
                'model': OPENROUTER_OCR_MODEL,
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image_url',
                                'image_url': {'url': f'data:image/png;base64,{b64_image}'},
                            },
                            {
                                'type': 'text',
                                'text': 'Extract ALL text from this image exactly as it appears. Return only the text, no commentary.',
                            },
                        ],
                    }
                ],
                'temperature': 0.0,
                'max_tokens': 8192,
            },
            timeout=120,
        )
        if resp.status_code != 200:
            print(f"[OCR] OpenRouter vision failed on page {page_num}: HTTP {resp.status_code}")
            return ""
        body = resp.json()
        return (body.get('choices', [{}])[0].get('message', {}).get('content') or "").strip()
    except Exception as e:
        print(f"[OCR] OpenRouter vision failed on page {page_num}: {e}")
        return ""


def _ocr_pdf_page_groq(file_path: str, page_num: int) -> str:
    """Render a single PDF page to image via PyMuPDF, then OCR via Module 4's Groq (Llama vision)."""
    api_key = _get_env('GROQ_MODULE4_API_KEY')
    if not api_key:
        return ""
    try:
        doc = fitz.open(file_path)
        if page_num >= len(doc):
            doc.close()
            return ""
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=72)
        img_bytes = pix.tobytes('png')
        doc.close()

        from PIL import Image as PILImage
        import io
        pil_img = PILImage.open(io.BytesIO(img_bytes))
        max_dim = 800
        w, h = pil_img.size
        if w > max_dim or h > max_dim:
            ratio = min(max_dim / w, max_dim / h)
            pil_img = pil_img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
        buf = io.BytesIO()
        pil_img.save(buf, format='PNG', optimize=True)
        img_bytes = buf.getvalue()

        b64_image = base64.b64encode(img_bytes).decode()
        client = groq.Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=GROQ_OCR_MODEL,
            messages=[
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'image_url',
                            'image_url': {'url': f'data:image/png;base64,{b64_image}'},
                        },
                        {
                            'type': 'text',
                            'text': 'Extract ALL text from this image exactly as it appears. Return only the raw text content, no commentary or description.',
                        },
                    ],
                }
            ],
            temperature=0.0,
            max_completion_tokens=8192,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[OCR] Groq Llama vision failed on page {page_num}: {e}")
        return ""


def extract_text_from_pdf(file_path: str, provider: str = 'gemini') -> str:
    """Extract text from PDF via pypdf (no OCR)."""
    text = ""
    with open(file_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
    return text

def extract_text_from_docx(file_path: str, provider: str = 'gemini') -> str:
    doc = docx.Document(file_path)
    return "\n".join([paragraph.text for paragraph in doc.paragraphs])

def get_document_text(file_path: str, provider: str = 'gemini') -> str:
    if file_path.endswith('.pdf'):
        return extract_text_from_pdf(file_path, provider=provider)
    elif file_path.endswith('.docx'):
        return extract_text_from_docx(file_path, provider=provider)
    else:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

def compute_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


# ── Heading-based semantic chunking for requirement extraction ──

# Heuristic: detect section headings by matching lines that start with
# numbered patterns like "Section 3", "3.1.2", "ARTICLE IV", or lines that
# are short, all-uppercase, and followed by a blank line — these are typical
# RFP section/division headers.
_HEADING_RE = re.compile(
    r'^(?:'
    r'(?:Section|SECTION|Section)\s+[\dIVX]+'          # "Section 3" / "SECTION IV"
    r'|(?:Article|ARTICLE)\s+[\dIVX]+'                  # "Article IV"
    r'|(?:Part|PART)\s+[\dIVX]+'                        # "Part 2"
    r'|\d+(?:\.\d+){0,4}\s+[A-Z]'                      # "3.1.2 Title" / "4 Scope"
    r'|(?:[A-Z][A-Za-z]*\s*){1,6}$'                    # short all-caps line (e.g. "SCOPE OF WORK")
    r')', re.MULTILINE
)


def _detect_headings(text: str) -> list[tuple[int, str]]:
    """Return list of (char_offset, heading_text) for detected section headings."""
    headings = []
    lines = text.split('\n')
    offset = 0
    for line in lines:
        stripped = line.strip()
        if stripped and _HEADING_RE.match(stripped) and len(stripped) < 120:
            headings.append((offset, stripped))
        offset += len(line) + 1
    return headings


def _chunk_by_headings(text: str, max_chunk_chars: int = 8000) -> list[dict]:
    """Split document into chunks at heading boundaries. No chunk cuts a requirement mid-sentence."""
    headings = _detect_headings(text)
    if not headings:
        # No headings found — return the whole document as one chunk
        return [{'heading': 'Full Document', 'text': text[:max_chunk_chars]}]

    chunks = []
    for i, (start, heading) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        chunk_text = text[start:end].strip()
        if not chunk_text:
            continue
        # If a single chunk exceeds max, split at paragraph boundaries
        if len(chunk_text) > max_chunk_chars:
            paragraphs = chunk_text.split('\n\n')
            current = ''
            for para in paragraphs:
                if len(current) + len(para) > max_chunk_chars and current:
                    chunks.append({'heading': heading, 'text': current.strip()})
                    current = para
                else:
                    current = current + '\n\n' + para if current else para
            if current.strip():
                chunks.append({'heading': heading, 'text': current.strip()})
        else:
            chunks.append({'heading': heading, 'text': chunk_text})
    return chunks


# ── Requirement extraction prompt (used per-chunk by the AI wrapper) ──

_REQUIREMENTS_EXTRACTION_PROMPT = """\
You are an RFP compliance analyst. Extract the most important requirements from the \
document chunk below. A requirement is any mandatory obligation, specification, \
constraint, deadline, certification, or deliverable the RFP imposes on bidders.

Extract between 5 and 20 requirements. Prioritize the most material requirements \
that would have the biggest impact on bid compliance and success.

For each requirement found, return an object with:
  - req_id: string, a stable unique identifier (use the format "REQ-<NNN>" with sequential numbering)
  - category: string, one of "Financial", "Legal", "Technical", "Operations"
  - description: string, the specific requirement text (not the whole clause)
  - section_ref: string, the heading/section this came from (e.g. "Section 3.1.2")
  - page_num: integer or null — only if the page is clearly determinable from context; use null when uncertain
  - initial_status: string, exactly one of "Yes", "No", "Partial"
    - "Yes" = requirement is clearly stated and directly addressable
    - "No"  = requirement is explicitly contradicted or clearly not addressed
    - "Partial" = requirement is ambiguous or only partially addressed

Rules:
- Extract at least 5 and at most 20 requirements.
- Do NOT fabricate a page_num you cannot support — null is correct when uncertain.
- Do NOT include evaluation criteria, scoring rubrics, or procedural instructions — \
only substantive requirements the bidder must meet.
 - Return a JSON object with key "requirements" containing the array of requirement objects. If no requirements found, return {"requirements": []}.
 - Return ONLY valid JSON. No markdown, no code fences, no explanation."""


def _extract_requirements_single(text: str, provider: str = 'gemini',
                                 filenames: list[str] | None = None) -> list[dict]:
    """Extract requirements from the full document using Cloudflare Workers AI only.
    The `provider` parameter is ignored — Cloudflare is the sole provider for Module 1."""
    prompt = _REQUIREMENTS_EXTRACTION_PROMPT + "\n\nDocument to analyze (full RFP):"
    print(f"[REQ] Single-call extraction ({len(text)} chars) with provider=cloudflare")

    raw_requirements = _call_ai_for_requirements(text, prompt, 'cloudflare')
    normalized = []
    for i, req in enumerate(raw_requirements):
        if isinstance(req, dict):
            if not req.get('req_id'):
                req['req_id'] = f"REQ-{i+1:03d}"
            normalized.append(req)
    # Cap at 20 requirements max
    normalized = normalized[:20]
    print(f"[REQ] Extracted {len(normalized)} requirements in 1 call")
    return normalized


def _extract_requirements_chunked(text: str, provider: str = 'gemini',
                                  filenames: list[str] | None = None) -> list[dict]:
    """Split document into heading-based chunks, extract requirements from each, merge."""
    global _GEMINI_RATE_LIMITED, _OPENROUTER_RATE_LIMITED
    _GEMINI_RATE_LIMITED = False
    _OPENROUTER_RATE_LIMITED = False

    chunks = _chunk_by_headings(text, max_chunk_chars=8000)
    all_requirements = []
    req_counter = 0

    print(f"[REQ] Processing {len(chunks)} chunks with provider={provider}")

    for i, chunk in enumerate(chunks[:8]):
        chunk_text = chunk['text'][:6000]
        heading = chunk['heading']
        prompt = _REQUIREMENTS_EXTRACTION_PROMPT + f"\n\nCurrent section heading: {heading}"
        print(f"[REQ] Chunk {i+1}/{min(len(chunks), 8)}: '{heading}' ({len(chunk_text)} chars)")

        try:
            raw_requirements = _call_ai_for_requirements(chunk_text, prompt, provider)
            for req in raw_requirements:
                if isinstance(req, dict):
                    req_counter += 1
                    if not req.get('req_id'):
                        req['req_id'] = f"REQ-{req_counter:03d}"
                    if not req.get('section_ref'):
                        req['section_ref'] = heading
                    all_requirements.append(req)
            print(f"[REQ] Chunk '{heading}': got {len(raw_requirements)} requirements")
        except Exception as e:
            print(f"[REQ] Error extracting from chunk '{heading}': {e}")
            continue
        if i < min(len(chunks), 8) - 1:
            time.sleep(3)

    print(f"[REQ] Total extracted: {len(all_requirements)} requirements")
    return all_requirements


def _call_ai_for_requirements(document_text: str, system_prompt: str, provider: str) -> list[dict]:
    """Call AI to extract requirements. Cloudflare Workers AI is the sole provider for Module 1."""
    if provider == 'cloudflare':
        if CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID:
            return _call_cloudflare_requirements(document_text, system_prompt, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID)
        raise ValueError("CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID not set")
    # Legacy providers — kept for other call sites (Module 2 etc.)
    gemini_key = _get_env('GEMINI_API_KEY')
    openrouter_key = _get_env('OPENROUTER_API_KEY')

    if provider == 'openrouter' and openrouter_key:
        return _call_openrouter_requirements(document_text, system_prompt, openrouter_key)
    elif provider == 'gemini':
        if gemini_key:
            return _call_gemini_requirements(document_text, system_prompt, gemini_key)
        raise ValueError("GEMINI_API_KEY not set")
    elif openrouter_key:
        return _call_openrouter_requirements(document_text, system_prompt, openrouter_key)
    elif gemini_key:
        return _call_gemini_requirements(document_text, system_prompt, gemini_key)
    raise ValueError("No API key available")


def _call_cloudflare_requirements(document_text: str, system_prompt: str, api_token: str, account_id: str) -> list[dict]:
    """Cloudflare Workers AI adapter for requirements extraction."""
    model = CLOUDFLARE_REQ_MODEL
    url = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}'
    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json',
    }
    doc_slice = document_text[:20000] if isinstance(document_text, str) else str(document_text)[:20000]
    resp = http_requests.post(url, headers=headers, json={
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': f'RFP Document Chunk:\n\n{doc_slice}'},
            {'role': 'assistant', 'content': '{"requirements": ['},
        ],
        'max_tokens': 30000,
    }, timeout=300)

    if resp.status_code == 429:
        raise RuntimeError("Cloudflare Workers AI rate limited (429). The daily 10,000 Neuron quota may be exhausted.")
    if resp.status_code != 200:
        resp_text = str(resp.text)[:300]
        raise RuntimeError(f"Cloudflare Workers AI HTTP {resp.status_code}: {resp_text}")

    body = resp.json()
    if body.get('success') is not True:
        errors = body.get('errors', [])
        err_msg = errors[0].get('message', str(errors)) if errors else 'unknown error'
        result_meta = body.get('result', {})
        if isinstance(result_meta, dict) and 'usage' in result_meta:
            print(f"[CLOUDFLARE] Neuron usage: {result_meta['usage']}")
        raise RuntimeError(f"Cloudflare Workers AI error: {err_msg}")

    result = body.get('result', {})
    raw = ''
    if isinstance(result, dict):
        # Cloudflare returns response in two forms:
        #   result.response           → already-parsed dict (e.g. {"requirements": [...]})
        #   result.choices[].message.content → raw JSON string
        # Handle both — use the raw string when available, short-circuit on already-parsed dict.
        choices = result.get('choices', [])
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get('message', {})
            raw = msg.get('content', '') or ''
        if not raw and 'response' in result:
            r = result['response']
            if isinstance(r, str):
                raw = r
            elif isinstance(r, dict):
                return r.get('requirements', [])
            elif isinstance(r, list):
                return r
    elif isinstance(result, str):
        raw = result
    raw = raw or '{"requirements": []}'

    if isinstance(result, dict) and 'usage' in result:
        print(f"[CLOUDFLARE] Daily neuron usage: {result['usage']}")

    parsed = parse_resilient_json(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return parsed.get('requirements', [])
    return []


def _call_cloudflare_verification(system_prompt: str, user_msg: str, api_token: str, account_id: str) -> str:
    """Cloudflare Workers AI adapter for Module 2 verification. Returns raw JSON string."""
    model = CLOUDFLARE_VERIFICATION_MODEL
    url = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}'
    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json',
    }
    resp = http_requests.post(url, headers=headers, json={
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_msg},
        ],
        'max_tokens': 4096,
    }, timeout=120)

    if resp.status_code == 429:
        print("[CLOUDFLARE] Verification rate limited (429). The daily 10,000 Neuron quota may be exhausted.")
        return '{}'
    if resp.status_code != 200:
        print(f"[CLOUDFLARE] Verification HTTP {resp.status_code}")
        return '{}'

    body = resp.json()
    if body.get('success') is not True:
        errors = body.get('errors', [])
        err_msg = errors[0].get('message', str(errors)) if errors else 'unknown error'
        print(f"[CLOUDFLARE] Verification API error: {err_msg}")
        return '{}'

    result = body.get('result', {})
    raw = ''
    if isinstance(result, dict):
        choices = result.get('choices', [])
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get('message', {})
            raw = msg.get('content', '') or ''
        if not raw and 'response' in result:
            r = result['response']
            if isinstance(r, str):
                raw = r
    elif isinstance(result, str):
        raw = result
    raw = raw or '{}'

    if isinstance(result, dict) and 'usage' in result:
        print(f"[CLOUDFLARE] Daily neuron usage (verification): {result['usage']}")

    return raw


def _call_gemini_requirements(document_text: str, system_prompt: str, api_key: str) -> list[dict]:
    """Single-attempt Gemini requirements extraction."""
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{system_prompt}\n\nRFP Document Chunk:\n\n{document_text}",
        config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=16384),
    )
    raw = response.text or "[]"
    if raw is None and response.candidates:
        for c in response.candidates:
            if c.content and c.content.parts:
                raw = ''.join(getattr(p, 'text', '') or '' for p in c.content.parts)
                if raw:
                    break
    raw = raw or "[]"
    parsed = parse_resilient_json(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return parsed.get('requirements', [])
    return []


def _call_openrouter_requirements(document_text: str, system_prompt: str, api_key: str) -> list[dict]:
    """Single-attempt OpenRouter requirements extraction. Uses prefix to force JSON output."""
    model = OPENROUTER_DEFAULT_MODEL
    url = 'https://openrouter.ai/api/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'http://localhost:5000',
        'X-OpenRouter-Title': 'RFP Automation Portal',
    }
    resp = http_requests.post(url, headers=headers, json={
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': f'RFP Document Chunk:\n\n{document_text[:35000]}'},
            {'role': 'assistant', 'content': '{"requirements": ['},
        ],
        'temperature': 0.1,
        'max_tokens': 16384,
    }, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    raw = (body.get('choices', [{}])[0].get('message', {}).get('content') or '{"requirements": []}')
    print(f"[REQ-OPENROUTER] raw response ({len(raw)} chars): {raw[:500]}")
    parsed = parse_resilient_json(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return parsed.get('requirements', [])
    return []


# Reserved for the RFP Sentinel copilot module (Module 3) —
# not currently called by the main analysis pipeline as of this change.
# The groq client import, _call_groq_requirements, and analyze_rfp_groq
# below are reserved for Module 3's low-latency conversational copilot.


def _call_groq_requirements(document_text: str, system_prompt: str, api_key: str) -> list[dict]:
    """Single-attempt Groq requirements extraction."""
    client = groq.Groq(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"RFP Document Chunk:\n\n{document_text[:25000]}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=8000,
        )
        raw = response.choices[0].message.content or "[]"
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return parsed.get('requirements', [])
        return []
    except Exception as e:
        print(f"[REQ-GROQ] Failed: {e}")
        return []

SECTION_PROMPTS = {
    'summary': "summary (string): 2-3 sentence executive summary of the RFP.",
    'deliverables': """deliverables (array of objects): CRITICAL — EXTRACT ABSOLUTELY EVERY DELIVERABLE WITH ZERO OMISSIONS. Create a SEPARATE parent group for EACH distinct deliverable category/section in the RFP. Do NOT merge categories. Each parent object has:
  - parent_number (string, sequential e.g. "1", "2", "3"...)
  - parent_title (string, exact name of the deliverable category/section from the RFP)
  - parent_summary (string, what this parent category covers)
  - children (array of objects, EXTRACT EVERY CHILD ITEM — each with:
      - child_number (string, e.g. "1.1", "1.2", "1.3"...)
      - title (string, exact deliverable name)
      - description (string, detailed description)
      - reference (string, section/page e.g. "Section IV, Page 12")
    )
MAXIMUM EXTRACTION MANDATORY: You MUST extract EVERY SINGLE line item, deliverable, product, service, report, document, form, software module, hardware item, study, plan, schedule, or work product mentioned ANYWHERE in the document. Create as many parent groups as needed — if there are 30 distinct categories, create 30 parent groups. Never skip, merge, truncate, or summarize. Extract ALL children under each parent. If the document has 200 deliverables, output all 200.""",
    'evaluation_criteria': "evaluation_criteria (array of strings): List scoring criteria, point allocations, and evaluation factors mentioned in the RFP.",
    'compliance': "compliance (array of strings): List compliance requirements across Financial, Legal, Operations, and Technical categories.",
    'risks': "risks (array of strings): List key risks mentioned in the RFP (performance, technical, financial, schedule).",
    'timeline': "timeline (array of strings): List dates, deadlines, milestones, and time-sensitive requirements mentioned in the RFP.",
    'key_requirements': "key_requirements (array of strings): List important requirements from the RFP (technical, functional, business, staffing, security).",
    'go_nogo': "go_nogo (object): A JSON object with: score (0-100 integer), verdict (string: 'Go'/'No-Go'/'Review'), summary (string), reasons (array of objects each with factor, weight, detail). Provide thorough strategic assessment covering win probability, competitive landscape, resource fit, financial viability, and key risk factors.",
    'strategic_checklist': """strategic_checklist (object): A JSON object matching the EXACT schema below.

CRITICAL EVALUATION RULES:
- For "Payment Terms": If the RFP specifies NET30, set status to "Go". If it specifies a payment period longer than NET30 (e.g., NET45, NET60, NET90), set status to "Escalate".
- For "Insurance Requirements": If the required insurance coverage is $5M or less, set status to "Go". If strictly greater than $5M, set status to "No-Go".
- For all other items use: "Go", "No-Go", "Review", "Escalate", "Caution", or "Not Specified in RFP".
- Include EVERY item below. If not mentioned in the RFP, set status to "Not Specified in RFP".
- Include rfp_evidence with direct quotes and section references from the RFP for every item.

Schema:
{
  "executive_summary": "Concise overview of findings, risks, and bid viability.",
  "financial": [
    {"item": "Payment Terms", "status": "Go", "reasoning": "...", "risk_level": "Low/Medium/High", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Financial Stability Requirements", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Insurance Requirements", "status": "Go", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Profitability Analysis", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Bid Bond", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."}
  ],
  "legal": [
    {"item": "Eligibility Criteria / Relevant Experience", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Registration Requirement", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Financial Statement of Previous Year", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Capability / Qualified Personnel", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Technical Knowhow", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Quantum of Input / Expected Revenue Generation", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Period of Implementation", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Insurance Coverage", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Compliance of Law", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "State Registration", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "E-Verify", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Contractual Obligations", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."}
  ],
  "operations": [
    {"item": "Required Forms", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Insurance Requirement", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Information Form (Tax ID, Owner Name, % ownership)", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Small Business (MD)", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "MBE (specify type)", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Workers Comp Insurance", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Business with Iran", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Submission Deadlines", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Document Compliance", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Signatory Authority", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Checklist of Required Documents", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Responsible Person / RFP Owner/Lead", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Meeting with Ops", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Vendor Registration / Specific Info", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Vendor Registration / Responsible Person", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."}
  ],
  "technical": [
    {"item": "Scope of Services/Products", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Technical Requirements", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Compliance with Industry Standards", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Security Considerations", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."},
    {"item": "Integration Needs", "status": "...", "reasoning": "...", "risk_level": "...", "rfp_evidence": "...", "impact_on_bid_strategy": "...", "mitigation_strategy": "..."}
  ]
}
Return ONLY valid JSON. No introductory or concluding text.""",
}
ALL_SECTIONS = list(SECTION_PROMPTS.keys())


# ── Shared analysis pipeline ──

_TEMPLATE_KEYS = ['summary', 'deliverables', 'evaluation_criteria', 'compliance', 'risks', 'timeline', 'key_requirements', 'go_nogo', 'strategic_checklist', 'requirements', 'sections_analyzed']

_DEFAULTS = {
    'summary': 'No summary provided.',
    'deliverables': [],
    'evaluation_criteria': [],
    'compliance': {'Financial': '', 'Legal': '', 'Operations': '', 'Technical': ''},
    'risks': [],
    'timeline': [],
    'key_requirements': [],
    'go_nogo': {'score': 0, 'verdict': 'Not Analyzed', 'summary': '', 'reasons': []},
    'strategic_checklist': {'executive_summary': '', 'financial': [], 'legal': [], 'operations': [], 'technical': []},
    'requirements': [],
    'sections_analyzed': [],
}


def _normalize_deliverables(val) -> list:
    raw_list = val if isinstance(val, list) else []
    result = []
    for item in raw_list:
        if isinstance(item, dict):
            parent_num = str(item.get('parent_number', '')).strip()
            parent_title = str(item.get('parent_title', '')).strip()
            parent_summary = str(item.get('parent_summary', '')).strip()
            has_children = 'children' in item
            children_raw = item.get('children', []) if has_children else item.get('sub_deliverables', [])

            if parent_num or parent_title or has_children:
                if not isinstance(children_raw, list):
                    children_raw = []

                # Template adds outer_idx. prefix — strip any leading number from the LLM's title
                if parent_title:
                    cleaned = re.sub(r'^\d+(\.\d+)*\s*[.)]?\s*', '', parent_title).strip()
                    deliverable = cleaned if cleaned and cleaned != parent_title else parent_title
                else:
                    deliverable = str(item.get('deliverable', item.get('name', '')))

                reference = parent_summary if parent_summary else str(item.get('reference', ''))

                subs = []
                for child in children_raw:
                    if isinstance(child, dict):
                        child_title = str(child.get('title', '')).strip()
                        child_desc = str(child.get('description', '')).strip()
                        child_ref = str(child.get('reference', '')).strip()

                        # Template adds outer_idx.loop_index prefix
                        if child_title:
                            cleaned = re.sub(r'^\d+(\.\d+)*\s*[.)]?\s*', '', child_title).strip()
                            name = cleaned if cleaned and cleaned != child_title else child_title
                        else:
                            name = str(child.get('name', ''))

                        cref_parts = []
                        if child_desc:
                            cref_parts.append(child_desc)
                        if child_ref:
                            cref_parts.append(f"({child_ref})")
                        cref = ' '.join(cref_parts) if cref_parts else ''

                        subs.append({'name': name, 'reference': cref, 'page_number': str(child.get('page_number', ''))})
                    elif isinstance(child, str):
                        subs.append({'name': child, 'reference': '', 'page_number': ''})

                result.append({'deliverable': deliverable, 'reference': reference, 'page_number': str(item.get('page_number', '')), 'sub_deliverables': subs})
            else:
                d = {
                    'deliverable': str(item.get('deliverable', item.get('name', ''))),
                    'reference': str(item.get('reference', '')),
                    'page_number': str(item.get('page_number', '')),
                    'sub_deliverables': [],
                }
                subs = item.get('sub_deliverables', [])
                if isinstance(subs, list):
                    d['sub_deliverables'] = [
                        {'name': str(s.get('name', '')), 'reference': str(s.get('reference', '')), 'page_number': str(s.get('page_number', ''))}
                        for s in subs if isinstance(s, dict)
                    ]
                result.append(d)
        elif isinstance(item, str):
            result.append({'deliverable': item, 'reference': '', 'page_number': '', 'sub_deliverables': []})
    return result


def _normalize_compliance(val) -> dict:
    if isinstance(val, dict):
        return {k: str(val.get(k, '')) for k in ('Financial', 'Legal', 'Operations', 'Technical')}
    parts = [str(v) for v in (val if isinstance(val, list) else []) if isinstance(v, str)]
    labels = ['Financial', 'Legal', 'Operations', 'Technical']
    result = {}
    for i, k in enumerate(labels):
        result[k] = parts[i] if i < len(parts) else ''
    return result


def _normalize_risks(val) -> list:
    raw_list = val if isinstance(val, list) else []
    result = []
    for item in raw_list:
        if isinstance(item, dict):
            result.append({
                'category': str(item.get('category', 'General')),
                'description': str(item.get('description', '')),
                'severity': str(item.get('severity', 'Medium')),
            })
        elif isinstance(item, str):
            result.append({'category': 'General', 'description': item, 'severity': 'Medium'})
    return result


def _normalize_timeline(val) -> list:
    raw_list = val if isinstance(val, list) else []
    result = []
    for item in raw_list:
        if isinstance(item, dict):
            result.append({
                'milestone': str(item.get('milestone', '')),
                'date_reference': str(item.get('date_reference', '')),
            })
        elif isinstance(item, str):
            result.append({'milestone': item, 'date_reference': ''})
    return result


def _normalize_gonogo(val):
    if isinstance(val, dict):
        reasons = []
        for r in (val.get('reasons') or []):
            if isinstance(r, dict):
                reasons.append({k: str(v) for k, v in r.items()})
            elif isinstance(r, str):
                reasons.append({'factor': r, 'weight': '', 'detail': ''})
        return {
            'score': int(val.get('score', 0)),
            'verdict': str(val.get('verdict', 'Not Analyzed')),
            'summary': str(val.get('summary', '')),
            'reasons': reasons,
        }
    return {'score': 0, 'verdict': 'Not Analyzed', 'summary': str(val) if val else '', 'reasons': []}


def _normalize_strategic_checklist(val) -> dict:
    if isinstance(val, dict):
        result = {'executive_summary': str(val.get('executive_summary', '')), 'financial': [], 'legal': [], 'operations': [], 'technical': []}
        for key in ('financial', 'legal', 'operations', 'technical'):
            items = val.get(key, [])
            if isinstance(items, list):
                result[key] = [
                    {
                        'item': str(i.get('item', '') if isinstance(i, dict) else i),
                        'status': str(i.get('status', 'Review') if isinstance(i, dict) else 'Review'),
                        'reasoning': str(i.get('reasoning', '') if isinstance(i, dict) else ''),
                        'risk_level': str(i.get('risk_level', 'Medium') if isinstance(i, dict) else 'Medium'),
                        'rfp_evidence': str(i.get('rfp_evidence', '') if isinstance(i, dict) else ''),
                        'impact_on_bid_strategy': str(i.get('impact_on_bid_strategy', '') if isinstance(i, dict) else ''),
                        'mitigation_strategy': str(i.get('mitigation_strategy', '') if isinstance(i, dict) else ''),
                    }
                    for i in items if isinstance(i, (dict, str))
                ]
        return result
    raw_list = val if isinstance(val, list) else []
    return {'executive_summary': '', 'financial': [], 'legal': [], 'operations': [], 'technical': [
        {'item': str(v), 'status': 'Review', 'reasoning': '', 'risk_level': 'Medium'} for v in raw_list if isinstance(v, str)
    ]}


def _normalize_requirements(val) -> list:
    """Normalize the structured requirements list from chunked extraction."""
    raw_list = val if isinstance(val, list) else []
    result = []
    for item in raw_list:
        if isinstance(item, dict):
            result.append({
                'req_id': str(item.get('req_id', '')),
                'category': str(item.get('category', 'General')),
                'description': str(item.get('description', '')),
                'section_ref': str(item.get('section_ref', '')),
                'page_num': item.get('page_num'),
                'initial_status': str(item.get('initial_status', 'Partial')),
            })
    return result


def coerce_analysis(parsed: dict, filenames: list[str] | None = None, sections: list[str] | None = None) -> dict:
    if isinstance(parsed, list):
        parsed = parsed[0] if len(parsed) > 0 else {}
    if not isinstance(parsed, dict):
        parsed = {}

    if 'score' in parsed or 'verdict' in parsed:
        gn = parsed.get('go_nogo')
        if not isinstance(gn, dict):
            gn = {}
        if 'score' in parsed:
            gn['score'] = int(parsed.pop('score'))
        if 'verdict' in parsed:
            gn['verdict'] = str(parsed.pop('verdict'))
        parsed['go_nogo'] = gn

    result = {}
    for key in _TEMPLATE_KEYS:
        raw = parsed.get(key)
        if key == 'summary':
            result[key] = str(raw) if raw else 'No summary provided.'
        elif key == 'deliverables':
            result[key] = _normalize_deliverables(raw)
        elif key == 'compliance':
            result[key] = _normalize_compliance(raw)
        elif key == 'risks':
            result[key] = _normalize_risks(raw)
        elif key == 'timeline':
            result[key] = _normalize_timeline(raw)
        elif key == 'go_nogo':
            result[key] = _normalize_gonogo(raw)
        elif key == 'strategic_checklist':
            result[key] = _normalize_strategic_checklist(raw)
        elif key == 'requirements':
            result[key] = _normalize_requirements(raw)
        elif key == 'sections_analyzed':
            result[key] = sections or filenames or []
        else:
            if isinstance(raw, list):
                result[key] = [str(v) for v in raw if isinstance(v, (str, int, float))]
            elif raw:
                result[key] = [str(raw)]
            else:
                result[key] = []

    # Robustness fallback: some models put the detailed requirement items under
    # key_requirements and leave requirements empty. Promote them so the
    # Requirements tab and Modules 2/5/6 still have data to work with.
    if not result.get('requirements'):
        kr_raw = parsed.get('key_requirements')
        kr_items = kr_raw if isinstance(kr_raw, list) and any(isinstance(x, dict) for x in kr_raw) \
            else result.get('key_requirements', [])
        promoted = []
        for i, kr in enumerate(kr_items, 1):
            if isinstance(kr, dict):
                desc = kr.get('description') or kr.get('text') or ''
                if not desc:
                    desc = ' '.join(str(v) for v in kr.values() if v is not None)
                promoted.append({
                    'req_id': str(kr.get('req_id') or f'REQ-{i:03d}'),
                    'category': str(kr.get('category') or 'General'),
                    'description': desc,
                    'section_ref': str(kr.get('section_ref') or ''),
                    'page_num': kr.get('page_num'),
                    'initial_status': str(kr.get('initial_status') or 'Partial'),
                })
            elif isinstance(kr, str):
                promoted.append({
                    'req_id': f'REQ-{i:03d}',
                    'category': 'General',
                    'description': kr,
                    'section_ref': '',
                    'page_num': None,
                    'initial_status': 'Partial',
                })
        if promoted:
            result['requirements'] = promoted
            print(f"[COERCE] Promoted {len(promoted)} key_requirements into requirements (requirements was empty)")

    return result


@app.template_filter('ensure_str')
def ensure_str(value) -> str:
    if value is None:
        return ''
    return str(value)


def _load_results(json_str: str) -> dict:
    data = parse_resilient_json(json_str)
    if isinstance(data, list):
        return data[0] if len(data) > 0 else {}
    if isinstance(data, dict):
        return data
    return {}


def _build_prompt(selected_keys: list[str]) -> str:
    keys_str = ', '.join(selected_keys)
    lines = [
        "You are an RFP analyst. Return a SINGLE JSON object with ALL of the following keys:",
        f"  {keys_str}",
        "",
        "Every key MUST be present. Use an empty array [] for any key where no data is found.",
        "",
    ]
    for s in selected_keys:
        if s in SECTION_PROMPTS:
            lines.append("- " + SECTION_PROMPTS[s])
    lines.append("")
    lines.append("Return ONLY the JSON object. No markdown, no code fences, no explanation.")
    return "\n".join(lines)


def _sanitize_text(text: str) -> str:
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202f\ufffe\uffff]', '', text)


def _repair_truncated_json(s: str):
    """Recover a JSON string that was cut off mid-way (e.g. the provider hit its
    output token cap). Scans with a delimiter stack, records every completed-value
    boundary, then tries closing remaining brackets IN ORDER (innermost first) from
    the longest prefix backwards. Returns the parsed value, or None if nothing
    repairs cleanly."""
    closer = {'{': '}', '[': ']'}
    stack = []
    in_str = False
    esc = False
    str_open_stack = None
    boundaries = []
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            if not in_str:
                str_open_stack = list(stack)
            in_str = not in_str
            if not in_str:
                boundaries.append((i + 1, list(stack)))
            continue
        if in_str:
            continue
        if ch in '{[':
            stack.append(ch)
        elif ch in '}]':
            if stack and closer.get(stack[-1]) == ch:
                stack.pop()
                boundaries.append((i + 1, list(stack)))
            else:
                break
        elif ch in ',:' or ch in ' \t\r\n':
            boundaries.append((i, list(stack)))
    if in_str and str_open_stack is not None:
        boundaries.append((len(s), str_open_stack))
    elif not in_str and not esc and s and s[-1] not in ' \t\r\n,:{[':
        boundaries.append((len(s), list(stack)))
    tried = set()
    for b, st in sorted(boundaries, key=lambda x: -x[0]):
        if b in tried:
            continue
        tried.add(b)
        prefix = s[:b]
        if in_str and b == len(s):
            prefix += '"'
        prefix += ''.join(closer[c] for c in reversed(st))
        try:
            return json.loads(prefix)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_resilient_json(raw: str):
    import re as _re
    s = raw.strip()
    s = _re.sub(r'^```(?:json)?\s*', '', s)
    s = _re.sub(r'\s*```$', '', s)
    s = s.strip()
    # Try direct parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Strings-aware depth tracking to find outermost balanced brace
    def _find_outer(s_, op_, cl_):
        start = s_.find(op_)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s_)):
            ch = s_[i]
            if esc:
                esc = False
                continue
            if ch == '\\':
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if not in_str:
                if ch == op_:
                    depth += 1
                elif ch == cl_:
                    depth -= 1
                    if depth == 0:
                        return s_[start:i+1]
        return None
    # Try outermost { ... } object (strings-aware)
    outer = _find_outer(s, '{', '}')
    if outer is not None:
        try:
            return json.loads(outer)
        except json.JSONDecodeError:
            pass
    # Try truncated JSON recovery: the provider hit its output cap, so close
    # whatever was left open — nested brackets closed in correct order.
    repaired = _repair_truncated_json(s)
    if repaired is not None:
        return repaired
    # Try extracting first [ ... ] block (last resort)
    outer = _find_outer(s, '[', ']')
    if outer is not None:
        try:
            return json.loads(outer)
        except json.JSONDecodeError:
            pass
    # Last-resort repair: try unescaping common issues
    try:
        fixed = _re.sub(r'\\(?!["\\/bfnrtu])', '\\\\', s)
        return json.loads(fixed)
    except Exception:
        pass
    return {}


def analyze_rfp(file_path: str, text_override: str | None = None, sections: list[str] | None = None, filenames: list[str] | None = None) -> dict:
    gemini_key = _get_env('GEMINI_API_KEY')
    gemini_fb_key = _get_env('GEMINI_FALLBACK_API_KEY')
    if not gemini_key and not gemini_fb_key:
        raise ValueError("GEMINI_API_KEY is not set in .env file.")

    document_text = _sanitize_text((text_override or get_document_text(file_path))[:MAX_DOC_CHARS])
    print(f"[GEMINI] Input text length: {len(document_text)} chars")

    selected_keys = [s for s in (sections or ALL_SECTIONS[:]) if s in SECTION_PROMPTS]
    prompt = _build_prompt(selected_keys)
    full_contents = f"RFP Document:\n\n{document_text}\n\n{prompt}"

    def _run_gemini(api_key: str, label: str):
        client = genai.Client(api_key=api_key)
        print(f"[{label}] Single call ({len(full_contents)} chars)")
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=full_contents,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=65536,
                ),
            )
            raw = response.text
            if raw is None and response.candidates:
                for c in response.candidates:
                    if c.content and c.content.parts:
                        raw = ''.join(getattr(p, 'text', '') or '' for p in c.content.parts)
                        if raw:
                            break
            raw = raw or "{}"
            print(f"[{label}] Raw response: {len(raw)} chars")
            parsed = parse_resilient_json(raw)
            result = coerce_analysis(parsed, filenames or [file_path], sections=selected_keys)
            print(f"[{label}] Success. Keys: {list(result.keys())}")
            os.makedirs('debug_dumps', exist_ok=True)
            ts = time.strftime('%Y%m%d_%H%M%S')
            with open(f'debug_dumps/{label.lower()}_raw_{ts}.json', 'w', encoding='utf-8') as df:
                df.write(raw)
            with open(f'debug_dumps/{label.lower()}_result_{ts}.json', 'w', encoding='utf-8') as df:
                json.dump(result, df, indent=2, default=str)
            return result
        except Exception as e:
            print(f"[{label}] Failed: {e}")
            raise

    # Try primary key first, then fallback on429/rate-limit exhaustion
    try:
        return _run_gemini(gemini_key or gemini_fb_key, 'GEMINI')
    except Exception as e:
        err_str = str(e)
        if gemini_fb_key and ('429' in err_str or 'RESOURCE_EXHAUSTED' in err_str or 'rate limit' in err_str.lower()):
            print(f"[GEMINI] Primary key exhausted, trying fallback key...")
            return _run_gemini(gemini_fb_key, 'GEMINI-FALLBACK')
        raise


def analyze_rfp_groq(file_path: str, text_override: str | None = None, sections: list[str] | None = None, filenames: list[str] | None = None) -> dict:
    api_key = _get_env('GROQ_API_KEY')
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set in .env file.")

    GROQ_MAX_CHARS = 18000  # conservative limit: system prompt ~6K tokens + doc ~5K tokens must fit in Groq context
    document_text = _sanitize_text((text_override or get_document_text(file_path))[:GROQ_MAX_CHARS])
    print(f"[GROQ] Input text length: {len(document_text)} chars")

    client = groq.Groq(api_key=api_key)
    selected_keys = [s for s in (sections or ALL_SECTIONS[:]) if s in SECTION_PROMPTS]
    system_prompt = _build_prompt(selected_keys)

    try:
        response = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"RFP Document Text:\n\n{document_text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=8192,
        )
        raw = response.choices[0].message.content or "{}"
        print(f"[GROQ] Raw response: {len(raw)} chars")
        parsed = parse_resilient_json(raw)
        result = coerce_analysis(parsed, filenames or [file_path], sections=selected_keys)
        print(f"[GROQ] Success. Keys: {list(result.keys())}")
        os.makedirs('debug_dumps', exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        with open(f'debug_dumps/groq_raw_{ts}.json', 'w', encoding='utf-8') as df:
            df.write(raw)
        with open(f'debug_dumps/groq_result_{ts}.json', 'w', encoding='utf-8') as df:
            json.dump(result, df, indent=2, default=str)
        return result
    except Exception as e:
        print(f"[GROQ] Failed: {type(e).__name__}: {str(e)[:300]}")
        raise


# ── Reserved for the RFP Sentinel copilot module (Module 3) ──
# The groq client import and analyze_rfp_groq above are reserved for Module 3
# (RFP Sentinel copilot) which requires low-latency conversational responses.
# The main analysis pipeline uses analyze_rfp_openrouter as of this change.


def _call_openrouter_single_section(document_text: str, section_key: str, section_prompt: str, api_key: str, model: str) -> dict:
    return {}


def analyze_rfp_openrouter(file_path: str, text_override: str | None = None, sections: list[str] | None = None, filenames: list[str] | None = None) -> dict:
    api_key = _get_env('OPENROUTER_API_KEY')
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not set in .env file.")

    model = OPENROUTER_DEFAULT_MODEL
    document_text = _sanitize_text((text_override or get_document_text(file_path))[:OPENROUTER_MAX_CHARS])
    print(f"[OPENROUTER] Input text length: {len(document_text)} chars, model: {model}")

    selected_keys = [s for s in (sections or ALL_SECTIONS[:]) if s in SECTION_PROMPTS]
    system_prompt = _build_prompt(selected_keys)
    OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'http://localhost:5000',
        'X-OpenRouter-Title': 'RFP Automation Portal',
    }

    print(f'[OPENROUTER] Single call for {len(selected_keys)} sections')
    os.makedirs('debug_dumps', exist_ok=True)
    raw = ''
    body = {}
    last_status = None
    for attempt in range(1, 4):
        try:
            resp = http_requests.post(OPENROUTER_URL, headers=headers, json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'RFP Document Text:\n\n{document_text}'},
                    {'role': 'assistant', 'content': '{'},
                ],
                'temperature': 0.2,
                'max_tokens': 32768,
            }, timeout=300)
            last_status = resp.status_code
            if resp.status_code != 200:
                raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            raw = (body.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()
            if raw and raw.strip() not in ('', '{}'):
                break
            finish = ''
            try:
                finish = body.get('choices', [{}])[0].get('finish_reason') or ''
            except Exception:
                pass
            print(f'[OPENROUTER] Empty response (attempt {attempt}/3, finish_reason={finish or "n/a"}); retrying...')
            with open(f'debug_dumps/openrouter_empty_{time.strftime("%Y%m%d_%H%M%S")}.json', 'w', encoding='utf-8') as df:
                json.dump(body, df, indent=2, default=str)
            if attempt < 3:
                time.sleep(3 * attempt)
        except Exception as e:
            print(f'[OPENROUTER] Attempt {attempt}/3 failed: {type(e).__name__}: {str(e)[:200]}')
            if attempt < 3:
                time.sleep(3 * attempt)
            else:
                raise
    if not raw or raw.strip() in ('', '{}'):
        raise RuntimeError(
            f"OpenRouter returned empty content after 3 attempts (HTTP {last_status}). "
            f"The free model '{model}' may be overloaded. Try again later or switch to Gemini/Groq.")
    print(f'[OPENROUTER] Raw response: {len(raw)} chars')
    parsed = parse_resilient_json(raw)
    result = coerce_analysis(parsed, filenames or [file_path], sections=selected_keys)
    print(f'[OPENROUTER] Done. Keys: {list(result.keys())}')
    ts = time.strftime('%Y%m%d_%H%M%S')
    with open(f'debug_dumps/openrouter_raw_{ts}.json', 'w', encoding='utf-8') as df:
        df.write(raw)
    with open(f'debug_dumps/openrouter_result_{ts}.json', 'w', encoding='utf-8') as df:
        json.dump(result, df, indent=2, default=str)
    return result

@app.before_request
def ensure_session():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())


def _upload_error_page(err: Exception, filenames: str):
    msg = html.escape(str(err) or type(err).__name__)
    fnames = html.escape(filenames or '')
    page = f'''<!DOCTYPE html><html lang="en" data-theme="light"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Analysis Failed</title>
<link rel="stylesheet" href="/static/css/theme.css"><style>
body{{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;background:var(--bg)}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:32px;max-width:600px;box-shadow:var(--shadow-lg)}}
.card h1{{font-size:20px;margin:0 0 12px;color:var(--text)}}
.card p{{font-size:14px;color:var(--text-secondary);line-height:1.6;word-break:break-word;margin:0 0 10px}}
.card code{{background:var(--bg);padding:2px 6px;border-radius:4px;font-size:13px}}
.card .btn{{margin-top:16px}}
</style></head><body><div class="card">
<h1>Analysis could not be completed</h1>
<p><strong>Files:</strong> {fnames}</p>
<p><code>{msg}</code></p>
<p>Tip: the free OpenRouter models can be overloaded and return empty output. Wait a few minutes and try again, or pick Gemini/Groq in the provider list.</p>
<a class="btn btn-primary" href="/">&larr; Back to upload</a>
</div></body></html>'''
    return make_response(page, 502)

@app.route('/')
def index() -> str:
    db = get_db()
    rows = db.execute(
        'SELECT id, title, filename, provider, timestamp FROM analyses '
        'ORDER BY timestamp DESC LIMIT 50'
    ).fetchall()
    return render_template('index.html', cases=[dict(r) for r in rows])

@app.route('/upload', methods=['POST'])
def upload_file() -> Union[Response, str]:
    if 'rfp_file' not in request.files:
        return make_response("No file part in the request", 400)

    files = request.files.getlist('rfp_file')
    files = [f for f in files if f and f.filename and f.filename.strip()]
    if not files:
        return make_response("No file selected", 400)

    saved_filenames = []
    combined_parts = []
    file_digests = []
    provider = request.form.get('provider', 'gemini')
    for f in files:
        fname = secure_filename(str(f.filename))
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        raw = f.read()
        with open(fpath, 'wb') as out:
            out.write(raw)
        saved_filenames.append(fname)
        file_digests.append(hashlib.sha256(raw).digest())
        doc_text = get_document_text(fpath, provider=provider)
        combined_parts.append(f"[DOCUMENT: {fname}]\n{doc_text}")

    combined_text = "\n\n---NEXT DOCUMENT---\n\n".join(combined_parts)
    display_filenames = ', '.join(saved_filenames)

    # Content hash of the uploaded files' actual bytes (SHA-256), in upload order.
    # Not derived from filenames or titles — identical bytes => identical hash.
    content_hash = hashlib.sha256(b'\x00'.join(file_digests)).hexdigest()
    word_count = len(combined_text.split())

    force_reanalyze = request.form.get('force_reanalyze', '') in ('1', 'true', 'on', 'True', 'yes')

    try:
        sections_raw = request.form.get('sections', '')
        sections = [s.strip() for s in sections_raw.split(',') if s.strip()] if sections_raw else None
        SHORT_KEY_MAP = {
            'evaluation': 'evaluation_criteria',
            'requirements': 'key_requirements',
            'gonogo': 'go_nogo',
            'strategic': 'strategic_checklist',
            'evaluation_criteria': 'evaluation_criteria',
            'key_requirements': 'key_requirements',
            'go_nogo': 'go_nogo',
            'strategic_checklist': 'strategic_checklist',
        }
        if sections:
            sections = [SHORT_KEY_MAP.get(s, s) for s in sections]

        used_sections = [s for s in (sections or ALL_SECTIONS[:]) if s in SECTION_PROMPTS]

        # ── Exact whole-document content cache ──
        # Only a plain repeat upload (same bytes, same provider, same selected
        # sections) is served from cache with zero AI calls. Selecting a different
        # provider, changing the section set, or ticking "force re-analyze" all
        # bypass the cache.
        cached_case = None
        if not force_reanalyze:
            db = get_db()
            hit = db.execute(
                'SELECT * FROM analyses WHERE hash = ? AND provider = ? '
                'ORDER BY timestamp DESC LIMIT 1',
                (content_hash, provider)
            ).fetchone()
            if hit:
                try:
                    hit_results = _load_results(hit['results'])
                except Exception:
                    hit_results = {}
                if isinstance(hit_results, dict) and set(hit_results.get('sections_analyzed', []) or []) == set(used_sections):
                    cached_case = dict(hit)
        if cached_case is not None:
            cached_results = _load_results(cached_case['results'])
            print(f"[CACHE] Analysis served from cache: identical document already analyzed as {cached_case['id']} "
                  f"(provider={provider}, {len(used_sections)} sections, zero AI calls)")
            analysis_title = request.form.get('analysis_title', '').strip() or saved_filenames[0].rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ')
            sid = session['session_id']
            record_id = str(uuid.uuid4())[:8]
            amends_case = request.form.get('amends_case', '').strip()
            resolved_case, delta = _m6_build_delta(amends_case, cached_results, current_id=record_id)
            db = get_db()
            db.execute(
                'INSERT INTO analyses (id, title, filename, provider, timestamp, hash, word_count, results, session_id, case_id, delta) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (record_id, analysis_title, display_filenames, provider, datetime.now().isoformat(),
                 content_hash, word_count, json.dumps(cached_results), sid, resolved_case,
                 json.dumps(delta) if delta else None)
            )
            db.commit()
            return render_template('results.html', results=cached_results, filename=display_filenames,
                                   record_id=record_id, record_title=analysis_title, provider=provider,
                                   delta=delta, is_amendment=bool(amends_case),
                                   versions=_get_versions(db, {'id': record_id, 'case_id': resolved_case}))

        try:
            if provider == 'openrouter':
                analysis_results = analyze_rfp_openrouter('', text_override=combined_text, sections=sections, filenames=saved_filenames)
            else:
                analysis_results = analyze_rfp('', text_override=combined_text, sections=sections, filenames=saved_filenames)
        except Exception as e:
            print(f"[UPLOAD] Analysis failed: {type(e).__name__}: {str(e)[:300]}")
            return _upload_error_page(e, display_filenames)

        print(f"[UPLOAD] analysis_results type={type(analysis_results).__name__}")
        print(f"[UPLOAD] analysis_results keys: {list(analysis_results.keys()) if isinstance(analysis_results, dict) else 'N/A'}")
        if isinstance(analysis_results, dict):
            sa = analysis_results.get('sections_analyzed', [])
            print(f"[UPLOAD] sections_analyzed ({len(sa)}): {sa}")
            for k in ['deliverables','evaluation_criteria','compliance','risks','timeline','key_requirements','go_nogo','strategic_checklist']:
                v = analysis_results.get(k, 'MISSING')
                print(f"[UPLOAD]   {k}: type={type(v).__name__} len={len(v) if isinstance(v, (list, dict)) else 'N/A'}")
            # Safety: ensure sections_analyzed is populated
            if not sa:
                used_sections = sections or ALL_SECTIONS[:]
                analysis_results['sections_analyzed'] = [s for s in used_sections if s in SECTION_PROMPTS]
                print(f"[UPLOAD] FIXED empty sections_analyzed -> {analysis_results['sections_analyzed']}")

        analysis_title = request.form.get('analysis_title', '').strip() or saved_filenames[0].rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ')

        sid = session['session_id']
        record_id = str(uuid.uuid4())[:8]
        amends_case = request.form.get('amends_case', '').strip()
        resolved_case, delta = _m6_build_delta(amends_case, analysis_results, current_id=record_id)
        db = get_db()
        db.execute(
            'INSERT INTO analyses (id, title, filename, provider, timestamp, hash, word_count, results, session_id, case_id, delta) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (record_id, analysis_title, display_filenames, provider, datetime.now().isoformat(),
             content_hash, word_count, json.dumps(analysis_results), sid, resolved_case,
             json.dumps(delta) if delta else None)
        )
        db.commit()

        return render_template('results.html', results=analysis_results, filename=display_filenames, record_id=record_id, record_title=analysis_title, provider=provider, delta=delta, is_amendment=bool(amends_case), versions=_get_versions(db, {'id': record_id, 'case_id': resolved_case}))
    except Exception as e:
        error_msg = str(e)
        if '503' in error_msg or 'UNAVAILABLE' in error_msg or 'ServiceUnavailable' in error_msg:
            return make_response("The AI service is temporarily overloaded (503). Please wait a few seconds and try again.", 503)
        if '413' in error_msg or 'Payload Too Large' in error_msg or 'content size limit' in error_msg.lower():
            return make_response("Document too large for the API. Try a shorter document or switch to Gemini.", 413)
        if '429' in error_msg or 'rate_limit' in error_msg.lower() or 'Too Many Requests' in error_msg:
            return make_response("API rate limit exceeded (429). Please wait 30-60 seconds then try again.", 429)
        return make_response(f"An error occurred during AI analysis: {error_msg}", 500)

@app.route('/export_pdf', methods=['POST'])
def export_pdf():
    results_raw = request.form.get('results_data', '{}')
    filename = request.form.get('filename', 'document')
    results_dict = _load_results(results_raw)

    html = render_template('report_template.html', results=results_dict, filename=filename)
    buf = io.BytesIO()
    status = pisa.CreatePDF(html, dest=buf)
    if getattr(status, 'err', False):
        buf.close()
        print('PDF generation error - status.err is True')
        return make_response("Error creating PDF", 500)
    pdf_bytes = buf.getvalue()
    buf.close()
    safe_name = re.sub(r'[^\w\s-]', '', filename).strip().replace(' ', '_') or 'analysis'
    import time
    ts = str(int(time.time()))
    resp = make_response(pdf_bytes)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename="RFP_Analysis_{safe_name}_{ts}.pdf"'
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/export_deliverables_pdf', methods=['POST'])
def export_deliverables_pdf():
    results_raw = request.form.get('results_data', '{}')
    filename = request.form.get('filename', 'document')
    results_dict = _load_results(results_raw)

    from datetime import date
    html = render_template('deliverables_template.html', results=results_dict, filename=filename, date=date.today().isoformat())
    buf = io.BytesIO()
    status = pisa.CreatePDF(html, dest=buf)
    if getattr(status, 'err', False):
        buf.close()
        print('PDF generation error - status.err is True')
        return make_response("Error creating deliverables PDF", 500)
    pdf_bytes = buf.getvalue()
    buf.close()
    safe_name = re.sub(r'[^\w\s-]', '', filename).strip().replace(' ', '_') or 'deliverables'
    ts = str(int(time.time()))
    resp = make_response(pdf_bytes)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename="Deliverables_{safe_name}_{ts}.pdf"'
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/export_json', methods=['POST'])
def export_json():
    results_raw = request.form.get('results_data', '{}')
    filename = request.form.get('filename', 'document')
    results_dict = _load_results(results_raw)

    safe_name = re.sub(r'[^\w\s-]', '', filename).strip().replace(' ', '_') or 'analysis'
    clean_name = f"RFP_Analysis_{safe_name}.json"
    response = make_response(json.dumps(results_dict, indent=2))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Content-Disposition'] = f'attachment; filename="{clean_name}"'
    return response

# Unauthenticated by design for this demo — do not expose in production without adding
# access control, since analysis IDs may be guessable and results contain compliance/pricing detail.
@app.route('/api/analysis/<analysis_id>/export.json')
def api_export_json(analysis_id):
    db = get_db()
    row = db.execute(
        'SELECT results FROM analyses WHERE id = ?', (analysis_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'analysis not found'}), 404
    return jsonify(_load_results(row['results']))

# Unauthenticated by design for this demo — do not expose in production without adding
# access control, since analysis IDs may be guessable and results contain compliance/pricing detail.
@app.route('/api/analysis/<analysis_id>/export.pdf')
def api_export_pdf(analysis_id):
    db = get_db()
    row = db.execute(
        'SELECT title, filename, results FROM analyses WHERE id = ?', (analysis_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'analysis not found'}), 404
    results_dict = _load_results(row['results'])
    html = render_template('report_template.html', results=results_dict, filename=row['filename'])
    buf = io.BytesIO()
    status = pisa.CreatePDF(html, dest=buf)
    if getattr(status, 'err', False):
        buf.close()
        return make_response('Error creating PDF', 500)
    pdf_bytes = buf.getvalue()
    buf.close()
    resp = make_response(pdf_bytes)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename="{analysis_id}.pdf"'
    return resp


@app.route('/export_requirements_xlsx', methods=['POST'])
def export_requirements_xlsx():
    """Export requirements as Excel (.xlsx) via form POST (UI button)."""
    if not HAS_OPENPYXL:
        return make_response("openpyxl not installed — cannot export Excel", 500)
    results_raw = request.form.get('results_data', '{}')
    filename = request.form.get('filename', 'document')
    results_dict = _load_results(results_raw)
    requirements = results_dict.get('requirements', [])
    verification = results_dict.get('module2_verification', {})
    return _build_requirements_xlsx(requirements, filename, verification)


@app.route('/api/analysis/<analysis_id>/export.xlsx')
def api_export_xlsx(analysis_id):
    """REST endpoint: export requirements as Excel (.xlsx) by analysis ID."""
    if not HAS_OPENPYXL:
        return jsonify({'error': 'openpyxl not installed'}), 500
    db = get_db()
    row = db.execute(
        'SELECT filename, results FROM analyses WHERE id = ?', (analysis_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'analysis not found'}), 404
    results_dict = _load_results(row['results'])
    requirements = results_dict.get('requirements', [])
    verification = results_dict.get('module2_verification', {})
    return _build_requirements_xlsx(requirements, row['filename'], verification)


def _build_requirements_xlsx(requirements: list, filename: str, verification: dict | None = None):
    """Build an Excel workbook with one row per requirement."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Requirements'
    headers = ['req_id', 'category', 'description', 'section_ref', 'page_num', 'initial_status', 'verified_status']
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = cell.font.copy(bold=True)
    for req in requirements:
        if isinstance(req, dict):
            req_id = req.get('req_id', '')
            verified = ''
            if verification:
                v = verification.get(req_id, {})
                if isinstance(v, dict):
                    verified = v.get('status', '')
            ws.append([
                req_id,
                req.get('category', ''),
                req.get('description', ''),
                req.get('section_ref', ''),
                req.get('page_num', ''),
                req.get('initial_status', ''),
                verified,
            ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = re.sub(r'[^\w\s-]', '', filename).strip().replace(' ', '_') or 'requirements'
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    resp.headers['Content-Disposition'] = f'attachment; filename="{safe_name}_requirements.xlsx"'
    return resp


@app.route('/extract_requirements/<analysis_id>', methods=['POST'])
def extract_requirements(analysis_id):
    """On-demand requirement extraction. Reads stored document text, extracts, saves back."""
    db = get_db()
    row = db.execute(
        'SELECT filename, provider, results FROM analyses WHERE id = ?', (analysis_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'analysis not found'}), 404

    results_dict = _load_results(row['results'])
    provider = row['provider'] or 'gemini'

    # Re-extract text from stored file
    upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), UPLOAD_FOLDER)
    filenames = [f.strip() for f in row['filename'].split(',') if f.strip()]
    combined_parts = []
    for fname in filenames:
        fpath = os.path.join(upload_dir, fname)
        if os.path.exists(fpath):
            doc_text = get_document_text(fpath, provider=provider)
            combined_parts.append(f"[DOCUMENT: {fname}]\n{doc_text}")
    if not combined_parts:
        return jsonify({'error': 'source files not found on disk'}), 404

    combined_text = "\n\n---NEXT DOCUMENT---\n\n".join(combined_parts)
    try:
        print(f"[REQ] On-demand extraction for {analysis_id} ({len(combined_text)} chars)")
    except OSError:
        pass

    try:
        extracted_requirements = _extract_requirements_single(combined_text, provider=provider)
    except Exception as e:
        err_msg = str(e)
        print(f"[REQ] Extraction failed for {analysis_id}: {err_msg}")
        return jsonify({'error': err_msg}), 500

    normalized = _normalize_requirements(extracted_requirements)
    results_dict['requirements'] = normalized

    # Save back to database
    db.execute(
        'UPDATE analyses SET results = ? WHERE id = ?',
        (json.dumps(results_dict), analysis_id)
    )
    db.commit()
    _sentinel_check_deltas(analysis_id, results_dict)
    print(f"[REQ] Saved {len(normalized)} requirements for {analysis_id}")

    return jsonify({'count': len(normalized), 'requirements': normalized})


@app.route('/history')
def history():
    records = []
    db = get_db()
    rows = db.execute(
        'SELECT id, title, filename, provider, timestamp, word_count, results FROM analyses '
        'ORDER BY timestamp DESC LIMIT 50'
    ).fetchall()
    for r in rows:
        rec = dict(r)
        try:
            parsed = _load_results(rec['results'])
            rec['results'] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            rec['results'] = {}
        records.append(rec)
    return render_template('history.html', records=records)

@app.route('/compare', methods=['POST'])
def compare():
    record_id_1 = request.form.get('record_1')
    record_id_2 = request.form.get('record_2')

    db = get_db()
    rows = db.execute(
        'SELECT * FROM analyses WHERE id IN (?, ?)',
        (record_id_1, record_id_2)
    ).fetchall()

    records = {r['id']: r for r in rows}
    r1 = records.get(record_id_1)
    r2 = records.get(record_id_2)

    if not r1 or not r2:
        return make_response("One or both records not found", 404)

    # Parse JSON results
    r1 = dict(r1)
    r2 = dict(r2)
    r1['results'] = _load_results(r1['results'])
    r2['results'] = _load_results(r2['results'])

    # Module 6: surface the stored delta summary when this pair is a
    # baseline/amendment pair (one record's delta references the other).
    def _parse_delta(row: dict):
        try:
            d = json.loads(row.get('delta') or 'null')
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    delta = None
    d1, d2 = _parse_delta(r1), _parse_delta(r2)
    if d2 and d2.get('baseline_id') == r1['id']:
        delta = d2
    elif d1 and d1.get('baseline_id') == r2['id']:
        delta = d1

    return render_template('compare.html', r1=r1, r2=r2, delta=delta)

@app.route('/view/<record_id>')
def view_analysis(record_id: str):
    db = get_db()
    row = db.execute(
        'SELECT * FROM analyses WHERE id = ?',
        (record_id,)
    ).fetchone()
    if not row:
        return make_response("Analysis not found", 404)
    rec = dict(row)
    try:
        results = _load_results(rec['results'])
    except Exception:
        results = {}
    try:
        delta = json.loads(rec.get('delta') or 'null') if rec.get('delta') else None
    except Exception:
        delta = None
    return render_template('results.html',
        results=results,
        filename=rec['filename'],
        record_id=rec['id'],
        record_title=rec['title'],
        provider=rec['provider'],
        delta=delta,
        is_amendment=bool(rec.get('case_id')),
        versions=_get_versions(db, rec)
    )


def _get_versions(db, rec: dict) -> list[dict]:
    """All records in this case group (baseline + amendments), newest first,
    so the results page can show a version-history / Amendments UI."""
    root_id = rec.get('case_id') or rec.get('id')
    if not root_id:
        return []
    rows = db.execute(
        'SELECT * FROM analyses WHERE id = ? OR case_id = ? ORDER BY timestamp DESC',
        (root_id, root_id)
    ).fetchall()
    versions = []
    for gr in rows:
        d = dict(gr)
        dd = None
        if d.get('delta'):
            try:
                dd = json.loads(d['delta'])
            except Exception:
                dd = None
        versions.append({
            'id': d['id'],
            'title': d.get('title') or d.get('filename'),
            'filename': d.get('filename'),
            'timestamp': str(d.get('timestamp') or ''),
            'is_baseline': d['id'] == root_id,
            'is_current': d['id'] == rec['id'],
            'provider': d.get('provider'),
            'high_count': (dd or {}).get('high_count'),
            'low_count': (dd or {}).get('low_count'),
            'summary': (dd or {}).get('summary') if dd else None,
            'baseline_id': (dd or {}).get('baseline_id') if dd else None,
        })
    return versions


@app.route('/requirements/<record_id>')
def view_requirements(record_id: str):
    db = get_db()
    row = db.execute(
        'SELECT * FROM analyses WHERE id = ?',
        (record_id,)
    ).fetchone()
    if not row:
        return make_response("Analysis not found", 404)
    rec = dict(row)
    try:
        results = _load_results(rec['results'])
    except Exception:
        results = {}
    reqs = results.get('requirements', [])
    verification = results.get('module2_verification', {})
    return render_template('requirements_view.html',
        requirements=reqs,
        verification=verification,
        analysis_title=rec['title'],
        filename=rec['filename'],
        record_id=rec['id']
    )


@app.route('/rename', methods=['POST'])
def rename():
    record_id = request.form.get('record_id')
    new_title = request.form.get('title', '').strip()
    if not record_id or not new_title:
        return make_response("Missing parameters", 400)
    db = get_db()
    cur = db.execute(
        'UPDATE analyses SET title = ? WHERE id = ?',
        (new_title, record_id)
    )
    db.commit()
    if cur.rowcount == 0:
        return make_response("Record not found", 404)
    return '', 204


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/company_profile')
def company_profile_page():
    profile = _load_company_profile()
    return render_template('company_profile.html', profile=profile)


@app.route('/api/company_profile', methods=['GET'])
def api_get_company_profile():
    return jsonify(_load_company_profile())


@app.route('/api/company_profile', methods=['POST'])
def api_save_company_profile():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400
    _save_company_profile(data)
    return jsonify({'ok': True, 'fact_count': len(_flatten_profile_to_facts(data))})


@app.route('/verify_requirements/<analysis_id>', methods=['POST'])
def verify_requirements(analysis_id):
    db = get_db()
    row = db.execute(
        'SELECT provider, results FROM analyses WHERE id = ?', (analysis_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'analysis not found'}), 404

    results_dict = _load_results(row['results'])
    provider = row['provider'] or 'openrouter'
    company_profile = _load_company_profile()

    requirements = results_dict.get('requirements', [])
    if not requirements:
        return jsonify({'error': 'No requirements found. Extract requirements first.'}), 400

    facts = _flatten_profile_to_facts(company_profile)
    if not facts or all(not f.get('text', '').strip() for f in facts):
        return jsonify({'error': 'Company profile is empty. Populate it first.'}), 400

    verification_results = _verify_all_requirements(requirements, company_profile, provider)
    results_dict['module2_verification'] = verification_results

    checklist = results_dict.get('strategic_checklist', {})
    checklist_verification = _verify_checklist_items(checklist, company_profile, provider)
    results_dict['module2_checklist_verification'] = checklist_verification

    for cat in ('financial', 'legal', 'operations', 'technical'):
        for item in checklist.get(cat, []):
            if isinstance(item, dict) and item.get('item'):
                matching = [v for v in checklist_verification.values()
                           if v.get('reasoning', '').startswith(f"chk_{cat[:3].upper()}") or
                           item['item'] in v.get('company_evidence', '')]
                if not matching:
                    for v in checklist_verification.values():
                        if item['item'] in str(v.get('rfp_evidence', '')):
                            matching.append(v)
                            break
                if matching:
                    item['company_evidence'] = matching[0].get('company_evidence', '')
                    item['verification_status'] = matching[0].get('status', '')

    db.execute(
        'UPDATE analyses SET results = ? WHERE id = ?',
        (json.dumps(results_dict), analysis_id)
    )
    db.commit()
    _sentinel_check_deltas(analysis_id, results_dict)

    return jsonify({
        'ok': True,
        'requirement_count': len(verification_results),
        'checklist_count': len(checklist_verification),
        'requirements': verification_results,
        'checklist': checklist_verification,
    })


@app.route('/api/company_profile/facts', methods=['GET'])
def api_company_facts():
    profile = _load_company_profile()
    facts = _flatten_profile_to_facts(profile)
    return jsonify({'facts': facts, 'count': len(facts)})


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — RFP Sentinel Copilot (Grounded Core, Phase 1)
# ═══════════════════════════════════════════════════════════════════════════════
# Groq-only Q&A over structured case JSON. Two paths:
#   1. Direct structured-field match (short-circuit, no LLM call)
#   2. LLM fallback via Groq (constrained to answer only from case JSON)
# No vector search, no embeddings, no intent router in this phase.

_SENTINEL_STOP_WORDS = frozenset({
    'what', 'is', 'are', 'the', 'a', 'an', 'in', 'of', 'to', 'for', 'and', 'or',
    'on', 'at', 'by', 'with', 'from', 'do', 'does', 'did', 'was', 'were', 'be',
    'been', 'have', 'has', 'had', 'can', 'will', 'would', 'could', 'should',
    'may', 'might', 'shall', 'about', 'into', 'through', 'during', 'before',
    'after', 'above', 'below', 'between', 'out', 'off', 'over', 'under',
    'again', 'further', 'then', 'once', 'here', 'there', 'when', 'where',
    'why', 'how', 'all', 'each', 'every', 'both', 'few', 'more', 'most',
    'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so',
    'than', 'too', 'very', 'just', 'because', 'as', 'until', 'while', 'it',
    'its', 'this', 'that', 'these', 'those', 'which', 'who', 'whom', 'whose',
    'tell', 'give', 'list', 'show', 'find', 'need', 'know', 'like', 'get',
})

_SENTINEL_DIRECT_THRESHOLD = 0.25

# Aggregate/overview questions ("what are the key requirements?", "list the risks") are
# answered better by the LLM fallback than by a single best-matching field.
_SENTINEL_AGGREGATE_RE = re.compile(
    r'\b(key )?(requirements|deliverables|evaluation criteria|risks|summary)\b',
    re.IGNORECASE,
)
_SENTINEL_AGGREGATE_ASK_RE = re.compile(r'^\s*(what|list|show|tell|give|enumerate)\b', re.IGNORECASE)

_SENTINEL_SYSTEM_PROMPT = """\
You are a precise RFP data assistant. You have been given the structured analysis \
data for a specific RFP case. Answer the user's question using ONLY the information \
present in this data.

Rules:
- Answer ONLY from the provided case JSON. Do NOT use any external knowledge.
- If the case JSON does not contain the information needed, say exactly: \
"I don't have that information in this case's data."
- Be concise. Answer in 1-3 sentences unless the question requires more detail.
- Cite which specific fields from the case JSON you used by their field paths.
- Return your response as a JSON object with these keys:
  - "answer": string (your answer text)
  - "cited_fields": array of strings (the case JSON field paths you used)
  - "confidence": "low" (always "low" for LLM fallback responses)
"""


def _sentinel_stem(word: str) -> str:
    w = word
    if w.endswith('ing'):
        w = w[:-3]
    elif w.endswith('tion'):
        w = w[:-4]
    elif w.endswith('ment'):
        w = w[:-4]
    elif w.endswith('ed'):
        w = w[:-2]
    if w.endswith('ss'):
        pass
    elif w.endswith('s') and len(w) > 3:
        w = w[:-1]
    return w

def _sentinel_keywords(text: str) -> set[str]:
    words = re.findall(r'\w+', text.lower())
    result = set()
    for w in words:
        if w not in _SENTINEL_STOP_WORDS and len(w) > 2:
            result.add(w)
            result.add(_sentinel_stem(w))
    return result


def _sentinel_match_score_idf(question: str, field_text: str, idf: dict, weight_total: float) -> float:
    """IDF-weighted keyword score. Rare question tokens (EFT, 100000) count far more
    than generic ones (bids, accepted) that appear across many fields."""
    if not field_text or not question:
        return 0.0
    t_keys = _sentinel_keywords(field_text)
    if not t_keys:
        return 0.0
    matched = sum(idf.get(qk, 0.0) for qk in _sentinel_keywords(question) if qk in t_keys)
    if matched <= 0 or weight_total <= 0:
        return 0.0
    score = matched / weight_total
    # Bonus for bigram/phrase match — earlier bigrams get higher weight
    q_words = [w for w in re.findall(r'\w+', question.lower())
               if w not in _SENTINEL_STOP_WORDS and len(w) > 2]
    t_lower = str(field_text).lower()
    bigram_bonus = 0.0
    for i in range(len(q_words) - 1):
        bigram = q_words[i] + ' ' + q_words[i+1]
        if bigram in t_lower:
            bonus = max(0.25 - i * 0.08, 0.08)
            bigram_bonus = max(bigram_bonus, bonus)
    if bigram_bonus:
        score = min(score + bigram_bonus * score, 0.85)
    # Small bonus for each raw question word present as substring of any target word
    raw_q = set(w for w in re.findall(r'\w+', question.lower())
                if w not in _SENTINEL_STOP_WORDS and len(w) > 2)
    raw_t = set(re.findall(r'\w+', t_lower))
    sub_count = sum(1 for qw in raw_q if any(qw in tw for tw in raw_t))
    if sub_count:
        score = min(score + sub_count * 0.04 * score, 0.85)
    return score


def _sentinel_direct_match(question: str, case: dict) -> dict | None:
    candidates = []

    def add(text, field_path, page=None):
        if text and str(text).strip():
            candidates.append((str(text), field_path, page))

    # Top-level fields
    add(case.get('summary'), 'summary')
    for i, kr in enumerate(case.get('key_requirements', [])):
        add(kr, f'key_requirements[{i}]')
    for i, ec in enumerate(case.get('evaluation_criteria', [])):
        add(ec, f'evaluation_criteria[{i}]')
    for key in ('Financial', 'Legal', 'Operations', 'Technical'):
        add(case.get('compliance', {}).get(key), f'compliance.{key}')

    # Deliverables (composite entries only — name + reference/description)
    for di, dg in enumerate(case.get('deliverables', [])):
        dg_name = (dg.get('deliverable') or '').strip()
        dg_page = dg.get('page_number')
        if dg_name:
            dg_ref = (dg.get('reference') or '').strip()
            composite = dg_name
            if dg_ref:
                composite += f" — {dg_ref}"
            add(composite, f'deliverables[{di}]', dg_page)
        for ci, child in enumerate(dg.get('sub_deliverables', [])):
            child_name = (child.get('name') or '').strip()
            child_desc = (child.get('description') or '').strip()
            child_page = child.get('page_number')
            if child_name:
                composite = child_name
                if child_desc:
                    composite += f": {child_desc}"
                add(composite, f'deliverables[{di}].sub_deliverables[{ci}]', child_page)

    # Risks
    for ri, risk in enumerate(case.get('risks', [])):
        add(risk.get('category'), f'risks[{ri}].category')
        add(risk.get('description'), f'risks[{ri}].description')

    # Timeline
    for ti, tm in enumerate(case.get('timeline', [])):
        add(tm.get('milestone'), f'timeline[{ti}].milestone')
        add(tm.get('date_reference'), f'timeline[{ti}].date_reference')

    # Go/No-Go (composite entries only)
    gn = case.get('go_nogo', {})
    gn_verdict = (gn.get('verdict') or '').strip()
    gn_score = gn.get('score')
    gn_summary = (gn.get('summary') or '').strip()
    if gn_verdict and gn_score is not None:
        add(f"Verdict: {gn_verdict} (Score: {gn_score}). {gn_summary}", 'go_nogo')
    for ri, reason in enumerate(gn.get('reasons', [])):
        factor = (reason.get('factor') or '').strip()
        detail = (reason.get('detail') or '').strip()
        if factor:
            composite = factor
            if detail:
                composite += f": {detail}"
            add(composite, f'go_nogo.reasons[{ri}]')

    # Strategic checklist — use composite entries only (item + status + reasoning)
    sc = case.get('strategic_checklist', {})
    add(sc.get('executive_summary'), 'strategic_checklist.executive_summary')
    for cat in ('financial', 'legal', 'operations', 'technical'):
        for ii, item in enumerate(sc.get(cat, [])):
            item_name = (item.get('item') or '').strip()
            item_status = (item.get('status') or '').strip()
            item_reasoning = (item.get('reasoning') or '').strip()
            item_evidence = (item.get('rfp_evidence') or '').strip()
            if item_name:
                composite = item_name
                if item_status:
                    composite += f" — Status: {item_status}"
                if item_reasoning:
                    composite += f". {item_reasoning}"
                if item_evidence:
                    composite += f" Evidence: {item_evidence[:300]}"
                add(composite, f'strategic_checklist.{cat}[{ii}]')

    # Extracted requirements (composite entries only)
    for ri, req in enumerate(case.get('requirements', [])):
        req_desc = (req.get('description') or '').strip()
        req_ref = (req.get('section_ref') or '').strip()
        req_cat = (req.get('category') or '').strip()
        req_page = req.get('page_num')
        if req_desc:
            composite = req_desc
            if req_cat:
                composite = f"[{req_cat}] {composite}"
            if req_ref:
                composite += f" ({req_ref})"
            if req_page:
                composite += f" [p. {req_page}]"
            add(composite, f'requirements[{ri}]', req_page)

    # Module 2 verification results (composite entries only)
    for req_id, v in case.get('module2_verification', {}).items():
        v_status = (v.get('status') or '').strip()
        v_reasoning = (v.get('reasoning') or '').strip()
        if v_status and v_reasoning:
            add(f"{req_id} | Status: {v_status} — {v_reasoning}", f'module2_verification.{req_id}')
        elif v_status:
            add(f"{req_id} | {v_status}", f'module2_verification.{req_id}')

    # Module 2 checklist verification results (composite entries only)
    for req_id, v in case.get('module2_checklist_verification', {}).items():
        v_status = (v.get('status') or '').strip()
        v_reasoning = (v.get('reasoning') or '').strip()
        if v_status and v_reasoning:
            add(f"{req_id} | Status: {v_status} — {v_reasoning}", f'module2_checklist_verification.{req_id}')
        elif v_status:
            add(f"{req_id} | {v_status}", f'module2_checklist_verification.{req_id}')

    if not candidates:
        return None

    if _SENTINEL_AGGREGATE_ASK_RE.search(question) and _SENTINEL_AGGREGATE_RE.search(question):
        return None

    q_lower = question.lower().strip()
    # Fast path: exact phrase is embedded in a field — unambiguous
    if len(q_lower) > 6:
        for text, field_path, page in candidates:
            if q_lower in text.lower():
                citation = field_path
                if page:
                    citation += f" (p. {page})"
                return {
                    "answer": text,
                    "cited_fields": [citation],
                    "confidence": "high",
                    "match_type": "direct",
                }

    q_keys = _sentinel_keywords(question)
    if not q_keys:
        return None

    # IDF weighting across candidate fields: a token found in few fields
    # (eft, 100000) discriminates far better than one in nearly all (bids, via).
    n = max(len(candidates), 1)
    df = dict.fromkeys(q_keys, 0)
    for text, _, _ in candidates:
        t_keys = _sentinel_keywords(text)
        for qk in q_keys:
            if qk in t_keys:
                df[qk] += 1
    idf = {qk: 1.0 + n / (df[qk] + 1) for qk in q_keys}
    weight_total = sum(idf.values())

    scored = []
    for text, field_path, page in candidates:
        score = _sentinel_match_score_idf(question, text, idf, weight_total)
        if score > 0:
            citation = field_path
            if page:
                citation += f" (p. {page})"
            scored.append((score, text, [citation]))
    if not scored:
        return None
    # Highest score wins; ties broken by more specific (longer) keyword set
    scored.sort(key=lambda x: (x[0], len(_sentinel_keywords(x[1]))), reverse=True)
    best = scored[0]
    if best[0] < _SENTINEL_DIRECT_THRESHOLD:
        return None
    return {
        "answer": best[1],
        "cited_fields": best[2],
        "confidence": "high",
        "match_type": "direct",
    }


def _sentinel_build_llm_context(case: dict, limit: int = 24000) -> str:
    """Compact case context for LLM fallback. Module 2 verification data is placed
    first so it is never lost to truncation."""
    lines = []
    for req_id, v in case.get('module2_verification', {}).items():
        lines.append(f"REQ {req_id}: {v.get('status','')} | risk={v.get('risk_level','')} | {(v.get('reasoning') or '')[:140]}")
    for req_id, v in case.get('module2_checklist_verification', {}).items():
        lines.append(f"CHK {req_id}: {v.get('status','')} | risk={v.get('risk_level','')} | {(v.get('reasoning') or '')[:140]}")
    for req in case.get('requirements', []):
        lines.append(f"REQ {req.get('req_id','')} [{req.get('category','')}]: {(req.get('description') or '')[:160]}")
    gn = case.get('go_nogo', {})
    if gn.get('verdict'):
        lines.append(f"VERDICT: {gn.get('verdict')} (score {gn.get('score')}). {(gn.get('summary') or '')[:300]}")
    if case.get('summary'):
        lines.append(f"SUMMARY: {case['summary'][:400]}")
    for it in case.get('key_requirements', []):
        lines.append(f"KEY REQ: {it[:160]}")
    for it in case.get('evaluation_criteria', []):
        lines.append(f"CRITERIA: {it[:160]}")
    for k in ('Financial', 'Legal', 'Operations', 'Technical'):
        v = case.get('compliance', {}).get(k)
        if v:
            lines.append(f"{k}: {v[:160]}")
    for d in case.get('deliverables', []):
        lines.append(f"DELIVERABLE: {(d.get('deliverable') or '')[:120]}")
    for t in case.get('timeline', []):
        lines.append(f"TIMELINE: {(t.get('milestone') or '')[:120]}")
    for r in case.get('risks', []):
        lines.append(f"RISK: {(r.get('category') or '')} - {(r.get('description') or '')[:120]}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n... [truncated]"
    return text


def _sentinel_fallback_groq(question: str, case: dict) -> dict:
    api_key = _get_env('GROQ_API_KEY')
    if not api_key:
        return {
            "answer": "Groq API key is not configured. Cannot answer this question.",
            "cited_fields": [],
            "confidence": "low",
            "match_type": "llm_fallback",
        }
    case_json_str = _sentinel_build_llm_context(case)
    client = groq.Groq(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {"role": "system", "content": _SENTINEL_SYSTEM_PROMPT},
                {"role": "user", "content": f"Case JSON:\n\n{case_json_str}\n\nQuestion: {question}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2048,
        )
        raw = response.choices[0].message.content or '{}'
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, dict):
            return {
                "answer": parsed.get("answer", "I don't have that information in this case's data."),
                "cited_fields": parsed.get("cited_fields", []),
                "confidence": "low",
                "match_type": "llm_fallback",
            }
        return {
            "answer": "I don't have that information in this case's data.",
            "cited_fields": [],
            "confidence": "low",
            "match_type": "llm_fallback",
        }
    except Exception as e:
        print(f"[SENTINEL] Groq fallback failed: {e}")
        return {
            "answer": f"I encountered an error processing your question.",
            "cited_fields": [],
            "confidence": "low",
            "match_type": "llm_fallback",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — Phase 2: Proactive (Action Cards + Sentinel Notes)
# ═══════════════════════════════════════════════════════════════════════════════

_SENTINEL_ALLOWED_ACTIONS = frozenset({'reassign', 'mark_reviewed', 'draft_email'})

_SENTINEL_ACTION_PATTERN = re.compile(
    r'\b(reassign|assign|transfer|move\s+to|mark\s+(as\s+)?(review(ed)?|complete|done)|'
    r'draft\s+(an?\s+)?(email|message)|send\s+(an?\s+)?email|compose\s+(an?\s+)?email)\b',
    re.IGNORECASE
)


# --- File-based storage helpers ---

def _sentinel_load_json(path: str) -> dict:
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _sentinel_save_json(path: str, data: dict):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def _sentinel_audit_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sentinel_audit.json')

def _sentinel_notes_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sentinel_notes.json')

def _sentinel_snapshots_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sentinel_snapshots.json')


# --- Audit Log ---

def _sentinel_store_action(case_id: str, action: dict, status: str = 'proposed') -> int:
    path = _sentinel_audit_path()
    data = _sentinel_load_json(path)
    if case_id not in data:
        data[case_id] = []
    entry = {**action, 'status': status, 'timestamp': datetime.now().isoformat()}
    data[case_id].append(entry)
    _sentinel_save_json(path, data)
    return len(data[case_id]) - 1

def _sentinel_update_action_status(case_id: str, idx: int, status: str):
    data = _sentinel_load_json(_sentinel_audit_path())
    if case_id in data and 0 <= idx < len(data[case_id]):
        data[case_id][idx]['status'] = status
        _sentinel_save_json(_sentinel_audit_path(), data)

def _sentinel_get_actions(case_id: str) -> list:
    data = _sentinel_load_json(_sentinel_audit_path())
    return data.get(case_id, [])


# --- Intent Classification ---

def _sentinel_is_action_request(message: str) -> bool:
    return bool(_SENTINEL_ACTION_PATTERN.search(message))


# --- Action Generator ---

_ACTION_SYSTEM_PROMPT = """\
You are a precise RFP action assistant. Given the user's request and the case data, \
determine the appropriate action. Return a JSON object with exactly these keys:
  - "action_type": string — one of "reassign", "mark_reviewed", "draft_email"
  - "target_id": string or null — for reassign, the person's name; for others, null
  - "payload": object or null — for draft_email, {"subject":"...","body":"..."}; for others, null
  - "reason": string — why this action should be taken
  - "explanation": string — what will happen in simple terms
  - "confidence": "high", "medium", or "low"

Rules:
- Only use action_type from: reassign, mark_reviewed, draft_email
- reassign: target_id is a person's name; payload is null
- mark_reviewed: target_id and payload are null
- draft_email: target_id is null; payload has "subject" and "body"
- If the request does not match any allowed action, return action_type "none" with reason explaining why
"""

def _sentinel_generate_action(question: str, case: dict) -> dict:
    api_key = _get_env('GROQ_API_KEY')
    if not api_key:
        return {
            "action_type": None, "reason": "Groq API key not configured",
            "explanation": "Cannot process action requests without LLM access.", "confidence": "low",
        }
    case_json_str = json.dumps(case, indent=2, default=str)
    if len(case_json_str) > 8000:
        case_json_str = case_json_str[:8000] + "\n... [truncated]"
    client = groq.Groq(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {"role": "system", "content": _ACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Case JSON:\n\n{case_json_str}\n\nRequest: {question}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1024,
        )
        raw = response.choices[0].message.content or '{}'
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"action_type": None, "reason": "Failed to parse LLM response", "explanation": "", "confidence": "low"}
    except Exception as e:
        print(f"[SENTINEL] Action generation failed: {e}")
        return {"action_type": None, "reason": f"LLM error: {e}", "explanation": "", "confidence": "low"}


# --- Action Executor ---

def _sentinel_execute_action(case: dict, action: dict) -> dict:
    action_type = action.get('action_type')
    if action_type not in _SENTINEL_ALLOWED_ACTIONS:
        return {"ok": False, "error": f"Action '{action_type}' is not allowed"}
    if action_type == 'reassign':
        target = action.get('target_id')
        if not target:
            return {"ok": False, "error": "reassign requires a target_id (assignee name)"}
        case['sentinel_assigned_to'] = target
        return {"ok": True, "message": f"Case reassigned to {target}"}
    elif action_type == 'mark_reviewed':
        case['sentinel_review_status'] = 'reviewed'
        return {"ok": True, "message": "Case marked as reviewed"}
    elif action_type == 'draft_email':
        payload = action.get('payload') or {}
        if not isinstance(payload, dict):
            payload = {}
        subject = payload.get('subject', '')
        body = payload.get('body', '')
        if not subject or not body:
            return {"ok": False, "error": "draft_email requires payload with subject and body"}
        case['sentinel_draft_email'] = {"subject": subject, "body": body}
        return {"ok": True, "message": "Email draft created", "subject": subject, "body": body}
    return {"ok": False, "error": f"Unknown action type: {action_type}"}


# --- Delta-based Sentinel Notes ---

def _sentinel_extract_watched_values(case: dict) -> dict:
    snapshot = {}
    gn = case.get('go_nogo', {})
    if 'score' in gn:
        snapshot['go_nogo.score'] = gn['score']
    if 'timeline' in case:
        snapshot['timeline'] = case['timeline']
    if 'strategic_checklist' in case:
        sc = case['strategic_checklist']
        no_go_count = 0
        for cat in ('financial', 'legal', 'operations', 'technical'):
            for item in sc.get(cat, []):
                if isinstance(item, dict) and item.get('status') == 'No-Go':
                    no_go_count += 1
        snapshot['strategic_checklist.no_go_count'] = no_go_count
    return snapshot

def _sentinel_check_deltas(case_id: str, new_case: dict):
    snap_path = _sentinel_snapshots_path()
    notes_path = _sentinel_notes_path()
    all_snaps = _sentinel_load_json(snap_path)
    prev_snapshot = all_snaps.get(case_id, {})
    new_values = _sentinel_extract_watched_values(new_case)
    all_snaps[case_id] = new_values
    _sentinel_save_json(snap_path, all_snaps)
    if not prev_snapshot:
        return
    notes = []
    old_score = prev_snapshot.get('go_nogo.score')
    new_score = new_values.get('go_nogo.score')
    if old_score is not None and new_score is not None and old_score != new_score:
        drop = old_score - new_score
        if drop >= 10:
            notes.append(f"Fit score dropped from {old_score} to {new_score} (Δ -{drop}). Review recommended.")
        elif new_score > old_score:
            notes.append(f"Fit score improved from {old_score} to {new_score}.")
    old_timeline = prev_snapshot.get('timeline', [])
    new_timeline = new_values.get('timeline', [])
    if json.dumps(old_timeline) != json.dumps(new_timeline):
        old_dates = json.dumps([t.get('date_reference', '') for t in old_timeline if isinstance(t, dict)])
        new_dates = json.dumps([t.get('date_reference', '') for t in new_timeline if isinstance(t, dict)])
        if old_dates != new_dates:
            notes.append("Timeline dates have changed. Verify your schedule.")
    old_nogo = prev_snapshot.get('strategic_checklist.no_go_count', 0)
    new_nogo = new_values.get('strategic_checklist.no_go_count', 0)
    if new_nogo > old_nogo:
        notes.append(f"{new_nogo - old_nogo} new No-Go items found (total: {new_nogo}). Review required.")
    if not notes:
        return
    all_notes = _sentinel_load_json(notes_path)
    if case_id not in all_notes:
        all_notes[case_id] = []
    ts = datetime.now().isoformat()
    for msg in notes:
        all_notes[case_id].append({"message": msg, "timestamp": ts})
    all_notes[case_id] = all_notes[case_id][-50:]
    _sentinel_save_json(notes_path, all_notes)

def _sentinel_get_notes(case_id: str) -> list:
    data = _sentinel_load_json(_sentinel_notes_path())
    return data.get(case_id, [])


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — Phase 3: Rehearsal + Partner Matching (read-only conversational)
# ═══════════════════════════════════════════════════════════════════════════════

_SENTINEL_REHEARSAL_PROMPT = """\
You are a skeptical procurement evaluator reviewing an RFP response. \
Your role is to pressure-test the bid by raising ONLY objections that are \
grounded in the case's actual Module 2 verification findings listed below. \
Do NOT invent new compliance problems that Module 2 did not already flag.

Module 2 Non-Go findings for this case:
{non_go_items}

Rules:
- Base your objections EXCLUSIVELY on the Non-Go items listed above.
- For each objection, cite the specific req_id, the status, and the stated \
reasoning from Module 2.
- Do NOT invent requirements, risks, or compliance gaps not listed.
- If the user asks you to take an action (reassign, mark_reviewed, draft_email, \
etc.), explain that Rehearsal Mode is read-only and cannot perform actions.
- Be concise. Answer in 1-3 paragraphs.
- Return your response as a JSON object with these keys:
  - "answer": string (your response text)
  - "cited_items": array of strings (the req_ids you referenced)
"""


# --- Partner Directory ---

PARTNER_DIRECTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'partner_directory.json')

_DEFAULT_PARTNERS = {
    "partners": [
        {
            "partner_name": "CloudTech Solutions",
            "capabilities": [
                "Cloud infrastructure migration and management",
                "AWS, Azure, and GCP managed services",
                "DevOps pipeline setup and CI/CD automation",
                "Kubernetes orchestration and containerization",
                "24/7 cloud operations monitoring",
            ],
            "contact_hint": "Maria Sanchez, Partner Relations Director",
        },
        {
            "partner_name": "SecureNet Defense",
            "capabilities": [
                "Cybersecurity assessment and penetration testing",
                "SOC 2 and ISO 27001 compliance consulting",
                "Network security architecture and firewall management",
                "Incident response and forensic analysis",
                "Security awareness training programs",
            ],
            "contact_hint": "James Chen, Cybersecurity Practice Lead",
        },
        {
            "partner_name": "DataBridge Analytics",
            "capabilities": [
                "Data engineering and ETL pipeline development",
                "Business intelligence dashboard implementation",
                "Machine learning model deployment and MLOps",
                "Data lake and warehouse architecture",
                "Real-time data streaming solutions",
            ],
            "contact_hint": "Dr. Priya Patel, Data Solutions Director",
        },
        {
            "partner_name": "FieldOps Pro",
            "capabilities": [
                "Field service management software implementation",
                "Mobile workforce enablement and scheduling",
                "IoT sensor integration for remote monitoring",
                "Geographic information system (GIS) integration",
                "Asset tracking and inventory management systems",
            ],
            "contact_hint": "Tom Williams, Operations Technology Director",
        },
    ]
}


def _load_partner_directory() -> dict:
    if os.path.exists(PARTNER_DIRECTORY_PATH):
        with open(PARTNER_DIRECTORY_PATH, 'r') as f:
            return json.load(f)
    _sentinel_save_json(PARTNER_DIRECTORY_PATH, _DEFAULT_PARTNERS)
    return dict(_DEFAULT_PARTNERS)


# --- Gap Detection (capability only = Technical/Operations, never Financial/Legal) ---

# Any status other than a clean Go signals a capability concern worth a partner match.
_GAP_TRIGGER_STATUSES = frozenset({'No-Go', 'Escalate', 'Caution', 'Review'})

def _sentinel_find_gap_items(case: dict) -> list[dict]:
    """
    Scan Module 2 verification results for capability gaps.
    Trigger: status No-Go/Escalate/Caution/Review AND category Technical or Operations.
    Financial and Legal items are explicitly excluded — no partner resolves
    a coverage-amount or debarment issue.
    """
    gaps = []
    verification = case.get('module2_verification', {})
    for i, req in enumerate(case.get('requirements', [])):
        req_id = req.get('req_id', f'REQ-{i+1:03d}')
        v = verification.get(req_id, {})
        if not v:
            continue
        status = v.get('status', '')
        category = req.get('category', '')
        # Hard category gate: only Technical/Operations are capability gaps
        if status in _GAP_TRIGGER_STATUSES and category in ('Technical', 'Operations'):
            gaps.append({
                'source': 'requirement',
                'req_id': req_id,
                'description': req.get('description', ''),
                'category': category,
                'status': status,
                'reasoning': v.get('reasoning', ''),
                'risk_level': v.get('risk_level', ''),
                'mitigation': v.get('mitigation_strategy', ''),
            })
    checklist_verification = case.get('module2_checklist_verification', {})
    cat_map = {'TEC': 'Technical', 'OPE': 'Operations', 'FIN': 'Financial', 'LEG': 'Legal'}
    for req_id, v in checklist_verification.items():
        status = v.get('status', '')
        parts = req_id.split('-')
        cat_prefix = parts[1] if len(parts) > 1 else ''
        category = cat_map.get(cat_prefix, '')
        if status in _GAP_TRIGGER_STATUSES and category in ('Technical', 'Operations'):
            gaps.append({
                'source': 'checklist',
                'req_id': req_id,
                'description': v.get('rfp_evidence', '') or v.get('reasoning', ''),
                'category': category,
                'status': status,
                'reasoning': v.get('reasoning', ''),
                'risk_level': v.get('risk_level', ''),
                'mitigation': v.get('mitigation_strategy', ''),
            })
    return gaps


# --- Partner Matching via Cloudflare embeddings ---

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _sentinel_match_partner(gap_description: str) -> dict | None:
    """Embed gap description and match against partner capabilities."""
    directory = _load_partner_directory()
    partners = directory.get('partners', [])
    if not partners:
        return None
    cap_texts = [', '.join(p.get('capabilities', []) or []) for p in partners]
    all_texts = [gap_description] + cap_texts
    embeddings = _embed_texts_batch(all_texts)
    if not embeddings or len(embeddings) != len(all_texts) or not embeddings[0]:
        return None
    gap_emb = embeddings[0]
    best_idx = -1
    best_score = 0.0
    for i, cap_emb in enumerate(embeddings[1:], 0):
        if not cap_emb:
            continue
        score = _cosine_similarity(gap_emb, cap_emb)
        if score > best_score:
            best_score = score
            best_idx = i
    SIMILARITY_THRESHOLD = 0.30
    if best_idx < 0 or best_score < SIMILARITY_THRESHOLD:
        return None
    partner = partners[best_idx]
    return {
        "partner_name": partner['partner_name'],
        "contact_hint": partner.get('contact_hint', ''),
        "similarity": round(best_score, 3),
    }


def _sentinel_check_partner_matches(case_id: str, case: dict) -> list[dict]:
    """
    Find capability gaps and match to partners.
    Produces draft_email action cards through Phase 2's existing action path.
    Returns list of action card response dicts (stored in audit log if new).
    Re-uses existing proposed actions from audit log to avoid duplicate cards.
    """
    gaps = _sentinel_find_gap_items(case)
    existing = _sentinel_get_actions(case_id)
    cards = []
    existing_gap_actions = {}
    for i, a in enumerate(existing):
        reason = a.get('reason') or ''
        idx = reason.find('gap:')
        if idx >= 0 and a.get('status') == 'proposed':
            gid = reason[idx + 4:].split()[0] if idx + 4 < len(reason) else ''
            if gid:
                existing_gap_actions[gid] = a
                a['action_idx'] = i
                cards.append({"response_type": "action_card", "action": a, "match_type": "partner_match"})
    for gap in gaps:
        gap_key = gap['req_id']
        if gap_key in existing_gap_actions:
            continue
        if any((c['action'].get('reason') or '').find(f'gap:{gap_key}') >= 0 for c in cards):
            continue
        match = _sentinel_match_partner(gap['description'])
        if not match:
            continue
        subject = f"Partner opportunity: {gap['category']} gap — {gap['description'][:80]}"
        body = (
            f"Hi {match['contact_hint']},\n\n"
            f"We are reviewing an RFP opportunity and identified a {gap['category']} capability gap "
            f"that aligns with your expertise.\n\n"
            f"Gap details:\n"
            f"- Requirement: {gap['description']}\n"
            f"- Module 2 status: {gap['status']}\n"
            f"- Risk level: {gap['risk_level']}\n"
            f"- Reasoning: {gap['reasoning']}\n"
            f"- Mitigation suggested: {gap['mitigation']}\n\n"
            f"Best match: {match['partner_name']}\n\n"
            f"Would you be available to discuss potential collaboration on this?\n\n"
            f"Best regards"
        )
        action = {
            "action_type": "draft_email",
            "target_id": match['contact_hint'],
            "payload": {"subject": subject, "body": body},
            "reason": f"Partner match for gap:{gap_key} — {gap['description'][:100]}",
            "explanation": f"Best match: {match['partner_name']} (similarity: {match['similarity']}). Draft email to {match['contact_hint']}.",
            "confidence": "high",
        }
        action['action_idx'] = _sentinel_store_action(case_id, action, 'proposed')
        cards.append({"response_type": "action_card", "action": action, "match_type": "partner_match"})
    return cards


# --- Rehearsal Mode handler ---

_NON_GO_STATUSES = frozenset({'No-Go', 'Escalate', 'Caution'})

def _sentinel_build_rehearsal_context(case: dict) -> str | None:
    items = []
    verification = case.get('module2_verification', {})
    for req_id, v in verification.items():
        if v.get('status') in _NON_GO_STATUSES:
            items.append(f"- {req_id}: status={v.get('status','')}, risk_level={v.get('risk_level','')}, reasoning={v.get('reasoning','')}")
    checklist_verif = case.get('module2_checklist_verification', {})
    for req_id, v in checklist_verif.items():
        if v.get('status') in _NON_GO_STATUSES:
            items.append(f"- {req_id}: status={v.get('status','')}, risk_level={v.get('risk_level','')}, reasoning={v.get('reasoning','')}")
    if not items:
        return None
    return "\n".join(items)


def _sentinel_rehearse(question: str, case: dict) -> dict:
    context = _sentinel_build_rehearsal_context(case)
    if context is None:
        return {
            "answer": "This case has no Module 2 findings flagged as No-Go, Escalate, or Caution. There are no objections to rehearse against.",
            "cited_items": [], "confidence": "high",
        }
    api_key = _get_env('GROQ_API_KEY')
    if not api_key:
        return {
            "answer": "Groq API key not configured. Cannot run rehearsal mode.",
            "cited_items": [], "confidence": "low",
        }
    prompt = _SENTINEL_REHEARSAL_PROMPT.format(non_go_items=context)
    client = groq.Groq(api_key=api_key)
    case_json_str = json.dumps(case, indent=2, default=str)
    if len(case_json_str) > 10000:
        case_json_str = case_json_str[:10000] + "\n... [truncated]"
    try:
        response = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Case JSON:\n\n{case_json_str}\n\nQuestion: {question}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2048,
        )
        raw = response.choices[0].message.content or '{}'
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, dict):
            return {
                "answer": parsed.get("answer", "No response generated."),
                "cited_items": parsed.get("cited_items", []),
                "confidence": "medium",
            }
        return {"answer": "I couldn't generate a rehearsal response.", "cited_items": [], "confidence": "low"}
    except Exception as e:
        print(f"[SENTINEL] Rehearsal failed: {e}")
        return {"answer": "Rehearsal mode encountered an error.", "cited_items": [], "confidence": "low"}


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — Smart Content Reuse Engine
# ═══════════════════════════════════════════════════════════════════════════════
# Uses its own Groq account (GROQ_MODULE4_API_KEY) — fully independent from
# Module 3's Groq client and rate-limit budget. Falls back to the main provider
# chain (Cloudflare → OpenRouter → Gemini) when Groq is unavailable.

_MODULE4_GROQ_MODEL = 'llama-3.3-70b-versatile'

_MODULE4_QA_PROMPT = """\
You are a precise Q&A extraction engine. Given the document text below, extract \
every explicit question-answer pair found in the content. Return a JSON array of \
objects, each with:
  - "question": string (the question as stated)
  - "answer": string (the answer as stated)
  - "context": string (surrounding section heading or context)
Only extract pairs where both a clear question and a clear answer are present.
Return [] if no Q&A pairs are found.
"""

_MODULE4_TONE_PROMPT = """\
You are a tone adaptation engine. Given the draft text below, rewrite it to match \
a professional, confident, and persuasive proposal tone. Rules:
- Keep all factual content intact.
- Use active voice.
- Be concise but authoritative.
- Remove hedging language ("we believe", "we think", "we hope").
- Return only the rewritten text, no commentary.
"""


def _call_module4_groq(system_prompt: str, user_msg: str, force_json: bool = True) -> str | None:
    """Call Module 4's dedicated Groq client. Returns raw text or None on failure.

    Groq rejects response_format json_object unless the prompt mentions "json", so
    callers that want plain text output (e.g. tone adaptation) pass force_json=False.
    """
    if not MODULE4_GROQ_ENABLED or not _MODULE4_GROQ_KEY:
        print("[MODULE4-GROQ] Skipped — disabled or no key")
        return None
    client = groq.Groq(api_key=_MODULE4_GROQ_KEY)
    try:
        kwargs: dict = {
            "model": _MODULE4_GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content or ''
        print("[MODULE4-GROQ] Success")
        return raw
    except Exception as e:
        print(f"[MODULE4-GROQ] Failed: {type(e).__name__}: {str(e)[:200]}")
        return None


def _call_module4_fallback(system_prompt: str, user_msg: str) -> str | None:
    """Fallback chain for Module 4: Cloudflare → OpenRouter → Gemini."""
    if CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID:
        try:
            raw = _call_cloudflare_verification(system_prompt, user_msg, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID)
            if raw:
                print("[MODULE4-FALLBACK] Served by Cloudflare")
                return raw
        except Exception as e:
            print(f"[MODULE4-FALLBACK] Cloudflare failed: {e}")
    openrouter_key = _get_env('OPENROUTER_API_KEY')
    if openrouter_key:
        try:
            raw = _call_openrouter_verification(system_prompt, user_msg, openrouter_key)
            if raw:
                print("[MODULE4-FALLBACK] Served by OpenRouter")
                return raw
        except Exception as e:
            print(f"[MODULE4-FALLBACK] OpenRouter failed: {e}")
    gemini_key = _get_env('GEMINI_API_KEY') or _get_env('GEMINI_FALLBACK_API_KEY')
    if gemini_key:
        try:
            raw = _call_gemini_verification(system_prompt, user_msg, gemini_key)
            if raw:
                print("[MODULE4-FALLBACK] Served by Gemini")
                return raw
        except Exception as e:
            print(f"[MODULE4-FALLBACK] Gemini failed: {e}")
    print("[MODULE4-FALLBACK] All providers exhausted")
    return None


# --- Module 4 Phase 1: Q&A Extraction ---

def _module4_extract_qa(document_text: str) -> list[dict]:
    """Extract question-answer pairs from document text."""
    user_msg = f"Document text:\n\n{document_text[:15000]}"
    raw = _call_module4_groq(_MODULE4_QA_PROMPT, user_msg)
    if raw is not None:
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, list):
            print(f"[MODULE4-QA] Extracted {len(parsed)} Q&A pairs via Groq")
            return parsed
        if isinstance(parsed, dict):
            qa = parsed.get('qa_pairs', []) or parsed.get('questions', [])
            if qa:
                print(f"[MODULE4-QA] Extracted {len(qa)} Q&A pairs via Groq")
                return qa
    raw = _call_module4_fallback(_MODULE4_QA_PROMPT, user_msg)
    if raw:
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, list):
            print(f"[MODULE4-QA] Extracted {len(parsed)} Q&A pairs via fallback")
            return parsed
        if isinstance(parsed, dict):
            qa = parsed.get('qa_pairs', []) or parsed.get('questions', [])
            if qa:
                print(f"[MODULE4-QA] Extracted {len(qa)} Q&A pairs via fallback")
                return qa
    print("[MODULE4-QA] No Q&A pairs extracted")
    return []


# --- Module 4 Phase 3: Tone Adaptation ---

def _module4_adapt_tone(draft_text: str) -> str:
    """Adapt draft text to professional proposal tone."""
    user_msg = f"Draft text to adapt:\n\n{draft_text[:8000]}"
    raw = _call_module4_groq(_MODULE4_TONE_PROMPT, user_msg, force_json=False)
    if raw is not None:
        stripped = raw.strip().strip('"').strip('```').strip()
        if stripped:
            print("[MODULE4-TONE] Adapted via Groq")
            return stripped
    raw = _call_module4_fallback(_MODULE4_TONE_PROMPT, user_msg)
    if raw:
        stripped = raw.strip().strip('"').strip('```').strip()
        if stripped:
            print("[MODULE4-TONE] Adapted via fallback")
            return stripped
    print("[MODULE4-TONE] Returning original text (no provider succeeded)")
    return draft_text


# ===== ROUTES =====


@app.route('/api/case/<case_id>/ask', methods=['POST'])
def sentinel_ask(case_id):
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'Missing question'}), 400
    db = get_db()
    row = db.execute(
        'SELECT results FROM analyses WHERE id = ?', (case_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'Case not found'}), 404
    case = _load_results(row['results'])
    if not case:
        return jsonify({'error': 'Case data could not be loaded'}), 500
    if _sentinel_is_action_request(question):
        action = _sentinel_generate_action(question, case)
        at = action.get('action_type')
        if at and at in _SENTINEL_ALLOWED_ACTIONS:
            action['action_idx'] = _sentinel_store_action(case_id, action, 'proposed')
            return jsonify({"response_type": "action_card", "action": action, "match_type": "action_proposal"})
        elif at is None or str(at).strip().lower() in ('none', 'null'):
            return jsonify({
                "response_type": "text",
                "answer": action.get('reason', 'I could not process this as a valid action request.'),
                "cited_fields": [], "confidence": action.get('confidence', 'low'), "match_type": "action_failed",
            })
    result = _sentinel_direct_match(question, case)
    if result is None:
        result = _sentinel_fallback_groq(question, case)
    result['response_type'] = 'text'
    return jsonify(result)


@app.route('/sentinel/<case_id>')
def sentinel_page(case_id):
    db = get_db()
    row = db.execute(
        'SELECT title, filename, provider, results FROM analyses WHERE id = ?', (case_id,)
    ).fetchone()
    if not row:
        return make_response("Case not found", 404)
    rec = dict(row)
    return render_template('sentinel.html',
        case_id=case_id,
        title=rec['title'],
        filename=rec['filename'],
        provider=rec['provider'],
    )


@app.route('/api/case/<case_id>/action', methods=['POST'])
def sentinel_apply_action(case_id):
    data = request.get_json(silent=True) or {}
    action_idx = data.get('action_idx')
    verdict = data.get('verdict', 'apply')
    if action_idx is None or not isinstance(action_idx, int) or action_idx < 0:
        return jsonify({'error': 'Invalid action_idx'}), 400
    actions = _sentinel_get_actions(case_id)
    if action_idx >= len(actions):
        return jsonify({'error': 'Action not found'}), 404
    if actions[action_idx].get('status') != 'proposed':
        return jsonify({'error': 'Action already processed'}), 400
    if verdict == 'discard':
        _sentinel_update_action_status(case_id, action_idx, 'discarded')
        return jsonify({'ok': True, 'message': 'Action discarded'})
    db = get_db()
    row = db.execute('SELECT results FROM analyses WHERE id = ?', (case_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Case not found'}), 404
    case = _load_results(row['results'])
    if not case:
        return jsonify({'error': 'Case data could not be loaded'}), 500
    result = _sentinel_execute_action(case, actions[action_idx])
    if result['ok']:
        db.execute('UPDATE analyses SET results = ? WHERE id = ?', (json.dumps(case), case_id))
        db.commit()
        _sentinel_update_action_status(case_id, action_idx, 'applied')
        _sentinel_check_deltas(case_id, case)
    return jsonify(result)


@app.route('/api/case/<case_id>/notes', methods=['GET'])
def sentinel_get_notes(case_id):
    return jsonify({'notes': _sentinel_get_notes(case_id)})


@app.route('/api/case/<case_id>/actions', methods=['GET'])
def sentinel_get_actions(case_id):
    return jsonify({'actions': _sentinel_get_actions(case_id)})


# ===== MODULE 3 — Phase 3 Routes =====


@app.route('/api/case/<case_id>/rehearse', methods=['POST'])
def sentinel_rehearse(case_id):
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'Missing question'}), 400
    if _sentinel_is_action_request(question):
        return jsonify({
            "answer": "Rehearsal Mode is read-only and cannot perform actions. Exit Rehearsal Mode if you need to apply an action.",
            "cited_items": [], "confidence": "high", "response_type": "rehearsal",
        })
    db = get_db()
    row = db.execute('SELECT results FROM analyses WHERE id = ?', (case_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Case not found'}), 404
    case = _load_results(row['results'])
    if not case:
        return jsonify({'error': 'Case data could not be loaded'}), 500
    result = _sentinel_rehearse(question, case)
    result['response_type'] = 'rehearsal'
    return jsonify(result)


@app.route('/api/case/<case_id>/partner-matches', methods=['GET'])
def sentinel_partner_matches(case_id):
    db = get_db()
    row = db.execute('SELECT results FROM analyses WHERE id = ?', (case_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Case not found'}), 404
    case = _load_results(row['results'])
    if not case:
        return jsonify({'error': 'Case data could not be loaded'}), 500
    cards = _sentinel_check_partner_matches(case_id, case)
    return jsonify({'cards': cards})


# ===== MODULE 4 — Routes =====


_MODULE4_TEXT_CACHE: dict = {}


def _get_case_document_text(record_id: str) -> str | None:
    """Return the source RFP document text for a case (cached in-memory)."""
    if record_id in _MODULE4_TEXT_CACHE:
        return _MODULE4_TEXT_CACHE[record_id]
    db = get_db()
    row = db.execute('SELECT filename FROM analyses WHERE id = ?', (record_id,)).fetchone()
    if not row:
        return None
    fname = row['filename'].split(',')[0].strip()
    fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
    if not os.path.exists(fpath):
        return None
    try:
        text = get_document_text(fpath, provider='gemini')
    except Exception as e:
        print(f"[MODULE4] Text extraction failed for {fname}: {e}")
        return None
    _MODULE4_TEXT_CACHE[record_id] = text
    return text


@app.route('/api/case/<case_id>/module4/source', methods=['GET'])
def module4_source(case_id):
    text = _get_case_document_text(case_id)
    if text is None:
        return jsonify({'error': 'Source document not found'}), 404
    return jsonify({'text': text, 'chars': len(text)})


@app.route('/module4/<record_id>')
def module4_page(record_id):
    db = get_db()
    row = db.execute('SELECT * FROM analyses WHERE id = ?', (record_id,)).fetchone()
    if not row:
        return make_response('Analysis not found', 404)
    rec = dict(row)
    results = _load_results(rec['results'])
    sample_draft = results.get('summary', '') or ''
    return render_template('module4.html',
        record_id=rec['id'],
        title=rec['title'],
        filename=rec['filename'],
        provider=rec['provider'],
        sample_draft=sample_draft,
    )


@app.route('/api/module4/extract-qa', methods=['POST'])
def module4_extract_qa():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    qa_pairs = _module4_extract_qa(text)
    return jsonify({'qa_pairs': qa_pairs, 'count': len(qa_pairs)})


@app.route('/api/module4/adapt-tone', methods=['POST'])
def module4_adapt_tone():
    data = request.get_json(silent=True) or {}
    draft = (data.get('draft') or '').strip()
    if not draft:
        return jsonify({'error': 'Missing draft text'}), 400
    adapted = _module4_adapt_tone(draft)
    return jsonify({'original': draft, 'adapted': adapted})


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — Response Library (Smart Content Reuse Engine)
# ═══════════════════════════════════════════════════════════════════════════════
# A searchable library of past proposal Q&A pairs. Three phases:
#   Phase 1 (ingest): past proposal/Q&A documents (PDF/DOCX/TXT) are parsed and
#     split into reusable question-answer pairs via the main provider chain
#     (Cloudflare → OpenRouter → Gemini). No Groq — reserved for Module 3.
#   Phase 2 (retrieval): an incoming RFP requirement is embedded with the same
#     Module 2 Cloudflare model / content-hash cache (shared vector space) and
#     matched against stored questions by cosine similarity. A match is returned
#     only above a 0.85 threshold, with optional industry/tech-stack filtering.
#   Phase 3 (adaptation): the matched past answer is rewritten for the specific
#     case (agency/client, dates, scope), preserving core technical claims, then
#     stored as the case's in-progress answer for that requirement. The adapted
#     version can optionally be saved back into the library.

RESPONSE_LIBRARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'response_library.json')
RESPONSE_LIBRARY_EMBEDDINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'response_library_embeddings.json')

_MODULE4_LIBRARY_THRESHOLD = 0.85  # semantic similarity cutoff for a "match"
_MODULE4_LIBRARY_TOP_K = 1
_MODULE4_LIBRARY_MAX_DOC_CHARS = 60000  # slice past documents before extraction
_MODULE4_LIBRARY_CACHE: dict | None = None  # in-memory copy of the loaded library


_MODULE4_LIBRARY_EXTRACT_PROMPT = """\
You are a proposal response librarian. Given text from a past proposal or Q&A \
document, extract reusable question-answer pairs. Return ONLY a JSON object with a \
"qa_pairs" array. Each item must have:
  - "question": the question as stated (string)
  - "answer": the answer as stated (string)
  - "industry": a short industry tag if discernible (e.g. "healthcare", "defense", \
"construction"), otherwise ""
  - "tech_stack": a small array of technologies/platforms mentioned (e.g. ["AWS", \
"React"]), empty array if none
Only include pairs where both a clear question and a clear answer are present. Return \
{"qa_pairs": []} if nothing useful is found.
"""


_MODULE4_LIBRARY_ADAPT_PROMPT = """\
You are a proposal tone adaptation engine. Rewrite the PAST ANSWER below so it \
responds to the INCOMING RFP REQUIREMENT, adapted to this specific case. Rules:
- PRESERVE all core technical claims, capabilities, and factual content in substance.
- Substitute placeholders / old project details with the CASE-SPECIFIC FIELDS provided \
(agency/client name, dates, scope). If a field is empty, keep the wording generic.
- Do NOT invent capabilities the company does not state.
- Do NOT mention the old client, agency, or project name.
- Use a professional, confident, persuasive proposal tone with active voice.
Return ONLY a JSON object: {"adapted": "..."}
"""


def _module4_lib_normalize_q(q: str) -> str:
    return re.sub(r'\s+', ' ', (q or '').strip().lower())


def _module4_library_load() -> dict:
    global _MODULE4_LIBRARY_CACHE
    if _MODULE4_LIBRARY_CACHE is not None:
        return _MODULE4_LIBRARY_CACHE
    data = {'answers': []}
    try:
        if os.path.exists(RESPONSE_LIBRARY_PATH):
            with open(RESPONSE_LIBRARY_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get('answers'), list):
            data = {'answers': []}
    except Exception as e:
        print(f"[MODULE4-LIB] Failed to load {RESPONSE_LIBRARY_PATH}: {e}")
        data = {'answers': []}
    _MODULE4_LIBRARY_CACHE = data
    return data


def _module4_library_save(data: dict):
    with open(RESPONSE_LIBRARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _module4_library_load_embeddings() -> dict:
    try:
        if os.path.exists(RESPONSE_LIBRARY_EMBEDDINGS_PATH):
            with open(RESPONSE_LIBRARY_EMBEDDINGS_PATH, 'r', encoding='utf-8') as f:
                emb = json.load(f)
            if isinstance(emb, dict):
                return emb
    except Exception as e:
        print(f"[MODULE4-LIB] Failed to load embeddings: {e}")
    return {}


def _module4_library_save_embeddings(emb: dict):
    with open(RESPONSE_LIBRARY_EMBEDDINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(emb, f, ensure_ascii=False)


def _module4_library_embed_answers(answers: list) -> dict:
    """Embed each answer's question text through the Module 2 pathway (same Cloudflare
    model, same content-hash cache). Re-embeds only question texts whose hash isn't
    already cached — the batch helper already does that per text."""
    texts = [a.get('question', '') or '' for a in answers]
    vectors = _embed_texts_batch(texts)
    out = {}
    for a, vec in zip(answers, vectors):
        aid = a.get('answer_id', '')
        if aid and vec:
            out[aid] = vec
    return out


def _module4_library_retrieve(requirement: str, industry: str | None = None,
                              tech_stack: list | None = None) -> tuple[dict | None, float]:
    """Phase 2 — semantic retrieval. Returns (best_answer, best_score) where
    best_answer is None unless best_score > _MODULE4_LIBRARY_THRESHOLD."""
    lib = _module4_library_load()
    answers = [a for a in lib.get('answers', []) if a.get('question') and a.get('answer')]
    if not answers:
        return None, 0.0
    emb = _module4_library_load_embeddings()
    query_vec = _embed_text(requirement or '')
    if not query_vec:
        print("[MODULE4-LIB] No query embedding available (Cloudflare unavailable?)")
        return None, 0.0
    industry_norm = (industry or '').strip().lower()
    tech_norm = {str(t).strip().lower() for t in (tech_stack or []) if str(t).strip()}
    best = None
    best_score = 0.0
    for a in answers:
        a_vec = emb.get(a.get('answer_id', ''))
        if not a_vec:
            continue
        if industry_norm:
            a_ind = (a.get('industry') or '').strip().lower()
            if a_ind and a_ind != industry_norm:
                continue
        if tech_norm:
            a_stack = {str(t).strip().lower() for t in (a.get('tech_stack') or []) if str(t).strip()}
            if a_stack and not (a_stack & tech_norm):
                continue
        score = _cosine_similarity(query_vec, a_vec)
        if score > best_score:
            best_score = score
            best = a
    if best is None or best_score <= _MODULE4_LIBRARY_THRESHOLD:
        return None, best_score
    return best, best_score


def _module4_library_extract_qa(text: str) -> list[dict]:
    """Phase 1 LLM step — extract reusable Q&A pairs via the main provider chain."""
    user_msg = f"Document text:\n\n{text[:_MODULE4_LIBRARY_MAX_DOC_CHARS]}"
    raw = _call_module4_fallback(_MODULE4_LIBRARY_EXTRACT_PROMPT, user_msg)
    if not raw:
        print("[MODULE4-LIB] Extraction LLM returned nothing")
        return []
    parsed = parse_resilient_json(raw)
    if isinstance(parsed, dict):
        pairs = parsed.get('qa_pairs') or []
    elif isinstance(parsed, list):
        pairs = parsed
    else:
        pairs = []
    cleaned = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        q = (p.get('question') or '').strip()
        a = (p.get('answer') or '').strip()
        if not q or not a:
            continue
        cleaned.append({
            'question': q,
            'answer': a,
            'industry': (p.get('industry') or '').strip() if p.get('industry') else '',
            'tech_stack': [str(t).strip() for t in (p.get('tech_stack') or []) if str(t).strip()],
        })
    return cleaned


def _module4_library_ingest_file(file_path: str, source_document: str) -> int:
    """Phase 1 — parse a past document and add its Q&A pairs to the library.
    Returns the number of new pairs added (0 if none)."""
    text = get_document_text(file_path, provider='gemini')
    if not text or not text.strip():
        return 0
    pairs = _module4_library_extract_qa(text)
    if not pairs:
        return 0
    lib = _module4_library_load()
    existing_q = {_module4_lib_normalize_q(a.get('question', '')) for a in lib.get('answers', [])}
    added = []
    for p in pairs:
        nq = _module4_lib_normalize_q(p['question'])
        if nq in existing_q:
            continue
        existing_q.add(nq)
        p['answer_id'] = uuid.uuid4().hex[:12]
        p['source_document'] = source_document
        p['created_at'] = datetime.utcnow().isoformat() + 'Z'
        added.append(p)
    if not added:
        return 0
    lib['answers'].extend(added)
    _module4_library_save(lib)
    new_emb = _module4_library_embed_answers(added)
    emb = _module4_library_load_embeddings()
    emb.update(new_emb)
    _module4_library_save_embeddings(emb)
    print(f"[MODULE4-LIB] Ingested {len(added)} new Q&A pairs from {source_document}")
    return len(added)


def _module4_library_case_context(case_results: dict, rec_title: str, rec_filename: str) -> dict:
    """Build the case-specific substitution fields for tone adaptation."""
    profile = _load_company_profile()
    summary = (case_results.get('summary') or '').strip() or ''
    timeline = case_results.get('timeline') or []
    if isinstance(timeline, list):
        dates = '; '.join(str(t.get('date') or t.get('milestone') or t) for t in timeline[:8])
    elif isinstance(timeline, dict):
        dates = str(timeline.get('dates') or timeline.get('summary') or '')
    else:
        dates = str(timeline)
    return {
        'agency': str(case_results.get('agency') or case_results.get('client') or ''),
        'title': str(rec_title or rec_filename or ''),
        'dates': dates,
        'scope': summary[:500],
        'company': str(profile.get('company_name') or ''),
    }


def _module4_library_adapt_answer(answer: str, requirement: str, case_context: dict) -> str:
    """Phase 3 — rewrite a stored answer for the specific case via the main provider
    chain, substituting case fields while preserving core technical claims."""
    fields = []
    for key, label in [('agency', 'Agency/client name'), ('title', 'Project/RFP title'),
                       ('dates', 'Timeline/dates'), ('scope', 'Scope summary'),
                       ('company', 'Bidder company name')]:
        val = (case_context.get(key) or '').strip()
        if val:
            fields.append(f"- {label}: {val}")
    fields_text = "\n".join(fields) if fields else "- (none provided — keep generic)"
    user_msg = (
        f"INCOMING RFP REQUIREMENT:\n{requirement}\n\n"
        f"PAST ANSWER:\n{answer}\n\n"
        f"CASE-SPECIFIC FIELDS TO SUBSTITUTE:\n{fields_text}\n"
    )
    raw = _call_module4_fallback(_MODULE4_LIBRARY_ADAPT_PROMPT, user_msg)
    if not raw:
        print("[MODULE4-LIB] Tone adaptation LLM returned nothing")
        return ''
    parsed = parse_resilient_json(raw)
    if isinstance(parsed, dict) and parsed.get('adapted'):
        return str(parsed['adapted']).strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return ''


@app.route('/api/module4/library', methods=['GET'])
def module4_library_list():
    lib = _module4_library_load()
    emb = _module4_library_load_embeddings()
    answers = []
    for a in lib.get('answers', []):
        item = dict(a)
        item['embedded'] = bool(emb.get(a.get('answer_id', '')))
        answers.append(item)
    return jsonify({'answers': answers, 'count': len(answers)})


@app.route('/api/module4/library/ingest', methods=['POST'])
def module4_library_ingest():
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': 'No file uploaded'}), 400
    fname = secure_filename(file.filename)
    if not fname.lower().endswith(('.pdf', '.docx', '.txt')):
        return jsonify({'error': 'Unsupported file type (use PDF, DOCX, or TXT)'}), 400
    fpath = os.path.join(app.config['UPLOAD_FOLDER'], 'lib_' + uuid.uuid4().hex[:8] + '_' + fname)
    file.save(fpath)
    try:
        added = _module4_library_ingest_file(fpath, fname)
    finally:
        try:
            os.remove(fpath)
        except OSError:
            pass
    lib = _module4_library_load()
    return jsonify({'added': added, 'total': len(lib.get('answers', []))})


@app.route('/api/module4/library/suggest', methods=['POST'])
def module4_library_suggest():
    data = request.get_json(silent=True) or {}
    requirement = (data.get('requirement') or '').strip()
    if not requirement:
        return jsonify({'error': 'Missing requirement text'}), 400
    industry = (data.get('industry') or '').strip() or None
    tech_stack = data.get('tech_stack') or None
    if isinstance(tech_stack, str) and tech_stack.strip():
        tech_stack = [t.strip() for t in tech_stack.split(',') if t.strip()]
    answer, score = _module4_library_retrieve(requirement, industry=industry, tech_stack=tech_stack)
    if answer is None:
        return jsonify({'match': False, 'score': round(score, 4),
                        'threshold': _MODULE4_LIBRARY_THRESHOLD})
    return jsonify({
        'match': True,
        'score': round(score, 4),
        'threshold': _MODULE4_LIBRARY_THRESHOLD,
        'answer': {
            'answer_id': answer['answer_id'],
            'question': answer['question'],
            'answer': answer['answer'],
            'source_document': answer.get('source_document', ''),
            'industry': answer.get('industry', ''),
            'tech_stack': answer.get('tech_stack', []),
        },
    })


@app.route('/api/module4/library/apply', methods=['POST'])
def module4_library_apply():
    data = request.get_json(silent=True) or {}
    case_id = (data.get('case_id') or '').strip()
    answer_id = (data.get('answer_id') or '').strip()
    requirement = (data.get('requirement') or '').strip()
    req_key = (data.get('req_key') or '').strip() or 'kr_0'
    if not case_id or not answer_id or not requirement:
        return jsonify({'error': 'Missing case_id, answer_id or requirement'}), 400
    lib = _module4_library_load()
    answer = next((a for a in lib.get('answers', []) if a.get('answer_id') == answer_id), None)
    if not answer:
        return jsonify({'error': 'Answer not found in library'}), 404
    db = get_db()
    row = db.execute('SELECT * FROM analyses WHERE id = ?', (case_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Case not found'}), 404
    case_results = _load_results(row['results'])
    context = _module4_library_case_context(case_results, row['title'], row['filename'])
    adapted = _module4_library_adapt_answer(answer.get('answer', ''), requirement, context)
    if not adapted:
        return jsonify({'error': 'Tone adaptation failed (all providers unavailable)'}), 502
    case_results.setdefault('module4_drafts', {})
    case_results['module4_drafts'][req_key] = {
        'requirement': requirement,
        'text': adapted,
        'source_answer_id': answer_id,
        'source_document': answer.get('source_document', ''),
        'adapted_at': datetime.utcnow().isoformat() + 'Z',
    }
    db.execute('UPDATE analyses SET results = ? WHERE id = ?', (json.dumps(case_results), case_id))
    db.commit()
    return jsonify({'ok': True, 'draft_key': req_key, 'adapted': adapted,
                    'source_document': answer.get('source_document', '')})


@app.route('/api/module4/library/draft', methods=['POST'])
def module4_library_draft():
    """Persist the user's in-progress answer for a requirement (minimal field added
    to the case results: module4_drafts[req_key].text)."""
    data = request.get_json(silent=True) or {}
    case_id = (data.get('case_id') or '').strip()
    req_key = (data.get('req_key') or '').strip()
    text = (data.get('text') or '').strip()
    if not case_id or not req_key:
        return jsonify({'error': 'Missing case_id or req_key'}), 400
    db = get_db()
    row = db.execute('SELECT * FROM analyses WHERE id = ?', (case_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Case not found'}), 404
    case_results = _load_results(row['results'])
    case_results.setdefault('module4_drafts', {})
    draft = case_results['module4_drafts'].get(req_key) or {}
    draft['text'] = text
    draft['requirement'] = draft.get('requirement') or (data.get('requirement') or '')
    draft['edited_at'] = datetime.utcnow().isoformat() + 'Z'
    case_results['module4_drafts'][req_key] = draft
    db.execute('UPDATE analyses SET results = ? WHERE id = ?', (json.dumps(case_results), case_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/module4/library/save-adapted', methods=['POST'])
def module4_library_save_adapted():
    """User-initiated save-back: add the adapted answer to the library."""
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    adapted = (data.get('adapted') or '').strip()
    source_doc = (data.get('source_document') or '').strip() or 'manual-entry'
    industry = (data.get('industry') or '').strip() or ''
    tech_stack = data.get('tech_stack') or []
    if isinstance(tech_stack, str) and tech_stack.strip():
        tech_stack = [t.strip() for t in tech_stack.split(',') if t.strip()]
    if not question or not adapted:
        return jsonify({'error': 'Missing question or adapted text'}), 400
    lib = _module4_library_load()
    nq = _module4_lib_normalize_q(question)
    if any(_module4_lib_normalize_q(a.get('question', '')) == nq for a in lib.get('answers', [])):
        return jsonify({'error': 'A library entry with this question already exists',
                        'duplicate': True}), 409
    entry = {
        'answer_id': uuid.uuid4().hex[:12],
        'question': question,
        'answer': adapted,
        'industry': industry,
        'tech_stack': [str(t).strip() for t in tech_stack if str(t).strip()],
        'source_document': source_doc + ' (adapted)',
        'created_at': datetime.utcnow().isoformat() + 'Z',
    }
    lib['answers'].append(entry)
    _module4_library_save(lib)
    emb = _module4_library_load_embeddings()
    emb.update(_module4_library_embed_answers([entry]))
    _module4_library_save_embeddings(emb)
    return jsonify({'ok': True, 'answer_id': entry['answer_id'], 'total': len(lib['answers'])})


@app.route('/api/module4/library/<answer_id>', methods=['DELETE'])
def module4_library_delete(answer_id):
    lib = _module4_library_load()
    before = len(lib.get('answers', []))
    lib['answers'] = [a for a in lib.get('answers', []) if a.get('answer_id') != answer_id]
    after = len(lib.get('answers', []))
    if after == before:
        return jsonify({'error': 'Answer not found'}), 404
    _module4_library_save(lib)
    emb = _module4_library_load_embeddings()
    if answer_id in emb:
        del emb[answer_id]
        _module4_library_save_embeddings(emb)
    return jsonify({'ok': True, 'total': after})


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 5 — Compliance Shred / Section L-M Crosswalk
# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: isolate the RFP's Instructions section (Section L analog) and
# Evaluation section (Section M analog), then detect shall/must/will clauses.
# Phase 2: embed each clause and the fixed proposal-draft outline, then map each
# clause to the draft section that answers it (crosswalk matrix).
# Phase 3: render "Answered In" in the existing Compliance and Checklist tabs.

_MODULE5_TEXT_CACHE: dict = {}
# Score scale = 0.5 * BM25(lexical-clean) + 0.5 * cosine against the outline, so
# genuine content matches land ~0.62-0.75 while procedural clauses sit ~0.22-0.55.
_MODULE5_SIMILARITY_THRESHOLD = 0.62
_MODULE5_MAX_CANDIDATES_PER_SECTION = 40

_PROPOSAL_OUTLINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'proposal_outline.json')

_DEFAULT_PROPOSAL_OUTLINE = [
    {'section': 'Technical Approach', 'description': 'proposed solution design and methodology'},
    {'section': 'Project Management Plan', 'description': 'project management and schedule'},
    {'section': 'Past Performance', 'description': 'relevant experience and references'},
    {'section': 'Pricing / Cost Proposal', 'description': 'cost breakdown and price forms'},
    {'section': 'Staffing and Key Personnel', 'description': 'proposed personnel and resumes'},
    {'section': 'Quality Assurance', 'description': 'quality control plan and procedures'},
    {'section': 'Compliance and Certifications', 'description': 'licenses, certifications, insurance, compliance'},
    {'section': 'Implementation Plan', 'description': 'implementation and rollout timeline'},
    {'section': 'Training Plan', 'description': 'training program for customer staff'},
    {'section': 'Security and Data Privacy', 'description': 'security controls and data protection'},
    {'section': 'Service Levels and Support', 'description': 'SLAs, support and maintenance'},
    {'section': 'Reporting', 'description': 'reports and deliverables to customer'},
]


def _load_proposal_outline() -> list:
    try:
        with open(_PROPOSAL_OUTLINE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list) and data and all(isinstance(o, dict) and o.get('section') for o in data):
            return data
    except Exception as e:
        print(f"[MODULE5] Outline load failed: {e}")
    return [dict(o) for o in _DEFAULT_PROPOSAL_OUTLINE]


# Extra L/M-style heading patterns not covered by _HEADING_RE (which handles
# "Section 3", "3.1.2", "SECTION IV", short all-caps lines).
# L/M sub-numbers ("L.1", "M.2") require a dot-digit suffix so a bare line
# like "Maryland Health Benefit Exchange" is never misread as a heading.
_LM_HEADING_RE = re.compile(
    r'^(?:'
    r'SECTION\s+[LM]\b'                # "SECTION L" / "SECTION M"
    r'|[LM](?:\.\d+){1,4}\b'           # "L.1", "M.2", "L.1.2", "M.3.1.2"
    r')', re.MULTILINE
)

_L_ANALOG_KEYWORDS = [
    'INSTRUCTIONS TO OFFERORS', 'BIDDING INSTRUCTIONS', 'BID FORMAT',
    'SUBMISSION', 'PROPOSAL FORMAT', 'OFFER FORMAT', 'FORMAT OF BID',
    'BID PREPARATION', 'PREPARATION OF BID', 'INSTRUCTIONS FOR BIDDERS',
    'SUBMISSION OF BIDS', 'SUBMISSION OF PROPOSALS', 'PROPOSAL SUBMISSION',
]
_M_ANALOG_KEYWORDS = [
    'BASIS FOR AWARD', 'BASIS OF AWARD', 'AWARD CRITERIA', 'EVALUATION',
    'EVALUATION FACTORS', 'FACTORS FOR AWARD', 'SCORING', 'AWARD',
]
_MAJOR_BOUNDARY_RE = re.compile(r'^(?:SECTION|ATTACHMENT|EXHIBIT|PART|ARTICLE)\s+[\dIVXLM]')


def _keyword_re(keywords):
    return re.compile(r'\b(?:' + '|'.join(re.escape(k) for k in keywords) + r')\b', re.IGNORECASE)


_L_KEYWORD_RE = _keyword_re(_L_ANALOG_KEYWORDS)
_M_KEYWORD_RE = _keyword_re(_M_ANALOG_KEYWORDS)


def _detect_lm_sections(text: str) -> dict:
    """Isolate Section L and Section M analogs. Returns spans dict or {'note': str}."""
    headings = _detect_headings(text)
    seen = set(h for _, h in headings)
    for m in _LM_HEADING_RE.finditer(text):
        line_start = text.rfind('\n', 0, m.start()) + 1
        line_end = text.find('\n', m.start())
        line_end = len(text) if line_end == -1 else line_end
        stripped = text[line_start:line_end].strip()
        if stripped and stripped not in seen and len(stripped) < 120:
            headings.append((line_start, stripped))
            seen.add(stripped)
    headings.sort()

    l_analog = None
    m_analog = None
    for offset, heading in headings:
        up = heading.upper()
        lm = re.match(r'^SECTION\s+([LM])\b', up)
        if lm:
            letter = lm.group(1)
            if letter == 'L' and l_analog is None:
                l_analog = (offset, heading)
            elif letter == 'M' and m_analog is None:
                m_analog = (offset, heading)
            continue
        if re.match(r'^L(?:\.\d+){1,4}\b', up) and l_analog is None:
            l_analog = (offset, heading)
            continue
        if re.match(r'^M(?:\.\d+){1,4}\b', up) and m_analog is None:
            m_analog = (offset, heading)
            continue
        # Numbered sub-headings (4.1, 4.4, 9.3.1 ...) are never section analogs.
        if re.match(r'^\d+(?:\.\d+){1,4}\s', up):
            continue
        if l_analog is None and _L_KEYWORD_RE.search(up):
            l_analog = (offset, heading)
            continue
        if m_analog is None and _M_KEYWORD_RE.search(up):
            m_analog = (offset, heading)

    majors = [off for off, h in headings if _MAJOR_BOUNDARY_RE.match(h.upper())]

    def _next_major(after: int):
        for off in majors:
            if off > after:
                return off
        return None

    spans = {}
    if l_analog:
        end = (m_analog[0] if m_analog and m_analog[0] > l_analog[0]
               else (_next_major(l_analog[0]) or len(text)))
        spans['L'] = {'start': l_analog[0], 'end': end, 'heading': l_analog[1]}
    if m_analog:
        end = _next_major(m_analog[0]) or len(text)
        spans['M'] = {'start': m_analog[0], 'end': end, 'heading': m_analog[1]}
    if not spans:
        return {'note': 'No instructions/evaluation section (Section L/M analog) detected in this document.'}
    return spans


def _extract_clause_candidates(span_text: str) -> list[str]:
    """Pattern-match sentences containing shall/must/will within a section span."""
    collapsed = re.sub(r'\s+', ' ', span_text)
    sentences = re.split(r'(?<=[.!?])\s+', collapsed)
    candidates = []
    for s in sentences:
        s = s.strip()
        if not (20 <= len(s) <= 1200):
            continue
        if re.search(r'\b(shall|must|will)\b', s, re.IGNORECASE):
            candidates.append(s)
            if len(candidates) >= _MODULE5_MAX_CANDIDATES_PER_SECTION:
                break
    return candidates


_MODULE5_CLAUSE_PROMPT = """\
You are an RFP compliance analyst. Below is a numbered list of candidate clauses \
extracted from an RFP's {section} section.

Confirm whether each candidate is a GENUINE requirement clause: a mandatory obligation, \
specification, constraint, deadline, certification, or deliverable imposed on the bidder. \
The words "shall", "must", and "will" often appear incidentally in prose — do not confirm \
those.

Return ONLY a JSON object:
{{"clauses": [{{"index": 1, "confirmed": true}}, {{"index": 2, "confirmed": false}}]}}

Include every index from 1 to {count}. No markdown, no commentary."""


def _confirm_clauses(candidates: list[str], system_context: str) -> list[int]:
    """Single LLM pass per section confirming genuine requirement clauses (Module 4's Groq)."""
    if not candidates:
        return []
    if not _MODULE4_GROQ_KEY:
        return list(range(len(candidates)))
    system_prompt = _MODULE5_CLAUSE_PROMPT.format(section=system_context, count=len(candidates))
    numbered = '\n'.join(f'{i + 1}. {t}' for i, t in enumerate(candidates))
    raw = _call_module4_groq(system_prompt, numbered)
    if raw is None:
        return list(range(len(candidates)))
    parsed = parse_resilient_json(raw)
    confirmed = []
    if isinstance(parsed, dict):
        for c in parsed.get('clauses', []):
            if isinstance(c, dict) and c.get('confirmed') and isinstance(c.get('index'), int):
                idx = c['index'] - 1
                if 0 <= idx < len(candidates):
                    confirmed.append(idx)
    if not confirmed:
        # LLM unavailable / unparseable — keep the pattern-matched candidates
        confirmed = list(range(len(candidates)))
    return confirmed


def _extract_pdf_pages(file_path: str) -> list[str]:
    pages = []
    with open(file_path, 'rb') as f:
        reader = pypdf.PdfReader(f)
        for page in reader.pages:
            pages.append(page.extract_text() or '')
    return pages


def _get_case_paged_text(record_id: str):
    """Return (full_text, page_starts) for a case, cached in-memory."""
    cached = _MODULE5_TEXT_CACHE.get(record_id)
    if cached:
        return cached
    db = get_db()
    row = db.execute('SELECT filename FROM analyses WHERE id = ?', (record_id,)).fetchone()
    if not row:
        return None
    fname = row['filename'].split(',')[0].strip()
    fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
    if not os.path.exists(fpath):
        return None
    try:
        pages = _extract_pdf_pages(fpath)
    except Exception as e:
        print(f"[MODULE5] PDF extraction failed for {fname}: {e}")
        return None
    if not pages:
        return None
    full = ''
    starts = []
    for p in pages:
        starts.append(len(full))
        full += p + '\n'
    _MODULE5_TEXT_CACHE[record_id] = (full, starts)
    return full, starts


def _page_of_offset(offset: int, starts: list[int]) -> int | None:
    page = None
    for i, s in enumerate(starts, 1):
        if offset >= s:
            page = i
        else:
            break
    return page


_MODULE5_LEXICAL_STOP_WORDS = frozenset(_STOP_WORDS) | frozenset({
    'bid', 'bids', 'bidder', 'bidders', 'rfp', 'ifb', 'solicitation', 'solicitations',
    'contract', 'contracts', 'award', 'awarded', 'awardee', 'proposal', 'proposals',
    'submit', 'submitted', 'submission', 'submissions', 'required', 'require',
    'requires', 'shall', 'must', 'state', 'states', 'procurement', 'procuring',
    'document', 'documents', 'documentation', 'section', 'sections', 'notice',
    'recommendation', 'following', 'attachment', 'attachments', 'format', 'pdf',
    'electronic', 'electronically', 'email', 'business', 'bpm', 'emma', 'maryland',
    'page', 'include', 'includes', 'included', 'listed', 'provide', 'provided',
    'received', 'receive', 'date', 'time', 'days', 'day',
})


def _m5_lexical_clean(text: str) -> str:
    """Strip structural RFP/procurement vocabulary before Module 5 BM25 scoring.

    Words like "contract", "award", "bidder" or "submitted" appear in nearly every
    clause, and BM25 over the small outline corpus gives them maximal IDF, so they
    would otherwise dominate the lexical signal and cause false mappings.
    """
    return ' '.join(t for t in _tokenize(text) if t not in _MODULE5_LEXICAL_STOP_WORDS)


def _best_outline_match(emb: list, outline: list, outline_embeddings: list,
                        text: str | None = None, lexical_norm: float = 1.0):
    """Return (best_section, best_score) for a clause/checklist embedding vs the outline.

    Uses the same hybrid as Module 2: combined = 0.5 * BM25 (lexical) + 0.5 * cosine
    (semantic), where BM25 is normalized by a corpus-global max (lexical_norm) so the
    scale stays absolute. Cosine alone plateaus below 0.70 for cross-domain
    clause/outline pairs, so the lexical term separates true matches (shared
    vocabulary such as price, insurance, training) from procedural clauses.
    Falls back to cosine-only when text is not provided.
    """
    best_name = None
    best_score = 0.0
    if emb and outline_embeddings:
        outline_texts = [_m5_lexical_clean(f"{o['section']} — {o.get('description', '')}") for o in outline]
        bm25_scores = _BM25(outline_texts).score(_m5_lexical_clean(text)) if text else None
        norm = lexical_norm if lexical_norm > 0 else 1.0
        for i, (o, oe) in enumerate(zip(outline, outline_embeddings)):
            if not oe:
                continue
            score = _cosine_similarity(emb, oe)
            if bm25_scores is not None:
                score = 0.5 * (bm25_scores[i] / norm) + 0.5 * score
            if score > best_score:
                best_score = score
                best_name = o['section']
    return best_name, best_score


def _run_module5_crosswalk(analysis_id: str) -> dict:
    """Execute Module 5 (isolate L/M clauses, embed, map to outline) and persist.
    Returns a dict with 'ok' / 'error' for JSON, plus 'results' and 'summary'."""
    db = get_db()
    row = db.execute('SELECT * FROM analyses WHERE id = ?', (analysis_id,)).fetchone()
    if not row:
        return {'error': 'analysis not found'}
    rec = dict(row)
    results = _load_results(rec['results'])

    paged = _get_case_paged_text(analysis_id)
    if paged is None:
        return {'error': 'Source document not found on disk.'}
    full_text, page_starts = paged

    sections = _detect_lm_sections(full_text)
    if 'note' in sections:
        results['module5_crosswalk'] = {
            'note': sections['note'], 'clauses': [], 'source_sections': {},
            'outline': [], 'threshold': _MODULE5_SIMILARITY_THRESHOLD,
        }
        db.execute('UPDATE analyses SET results = ? WHERE id = ?', (json.dumps(results), analysis_id))
        db.commit()
        return {
            'ok': True, 'results': results,
            'summary': {'clause_count': 0, 'mapped': 0, 'unmapped': 0, 'note': sections['note']},
        }

    outline = _load_proposal_outline()
    outline_texts = [f"{o['section']} — {o.get('description', '')}" for o in outline]
    outline_embeddings = _embed_texts_batch(outline_texts)

    clauses = []
    for letter in ('L', 'M'):
        if letter not in sections:
            continue
        span = sections[letter]
        span_text = full_text[span['start']:span['end']]
        candidates = _extract_clause_candidates(span_text)
        system_context = 'Instructions to Offerors' if letter == 'L' else 'Evaluation Factors'
        confirmed_idx = _confirm_clauses(candidates, system_context)
        for n, idx in enumerate(confirmed_idx, 1):
            clause_text = candidates[idx]
            rel = span_text.find(clause_text)
            if rel < 0 and clause_text.split():
                first_token = clause_text.split()[0]
                pos = span_text.find(first_token)
                rel = pos if pos >= 0 else rel
            global_off = span['start'] + rel if rel >= 0 else None
            clauses.append({
                'clause_id': f'SEC{letter}-{n:03d}',
                'text': clause_text,
                'rfp_section_ref': span['heading'],
                'page_num': _page_of_offset(global_off, page_starts) if global_off is not None else None,
            })

    clause_texts = [c['text'] for c in clauses]
    clause_embeddings = _embed_texts_batch(clause_texts)
    threshold = _MODULE5_SIMILARITY_THRESHOLD

    # Corpus-global BM25 normalizer so hybrid scores keep an absolute scale.
    bm25 = _BM25([_m5_lexical_clean(f"{o['section']} — {o.get('description', '')}") for o in outline])
    lexical_norm = 0.0
    checklist = results.get('strategic_checklist', {})
    checklist_items = []
    for cat in ('financial', 'legal', 'operations', 'technical'):
        for item in checklist.get(cat, []):
            if isinstance(item, dict) and item.get('item'):
                checklist_items.append(item)
    checklist_texts = [
        f"{it.get('item', '')} {it.get('rfp_evidence', '')}".strip() for it in checklist_items
    ]
    for t in clause_texts + checklist_texts:
        lexical_norm = max(lexical_norm, max(bm25.score(_m5_lexical_clean(t))))
    if lexical_norm == 0:
        lexical_norm = 1.0

    for c, emb in zip(clauses, clause_embeddings):
        best_name, best_score = _best_outline_match(emb, outline, outline_embeddings, c['text'], lexical_norm)
        if best_name and best_score >= threshold:
            c['draft_section'] = best_name
            c['status'] = 'Mapped'
        else:
            c['draft_section'] = None
            c['status'] = 'Unmapped'
        c['similarity_score'] = round(best_score, 3)

    # Phase 2 extension: Answered In for each strategic-checklist item
    checklist_embeddings = _embed_texts_batch(checklist_texts)
    for i, (item, emb) in enumerate(zip(checklist_items, checklist_embeddings)):
        best_name, best_score = _best_outline_match(emb, outline, outline_embeddings, checklist_texts[i], lexical_norm)
        if best_name and best_score >= threshold:
            item['module5_draft_section'] = best_name
            item['module5_status'] = 'Mapped'
        else:
            item['module5_draft_section'] = None
            item['module5_status'] = 'Unmapped'
        item['module5_similarity'] = round(best_score, 3)

    results['module5_crosswalk'] = {
        'note': '',
        'source_sections': {k: v['heading'] for k, v in sections.items()},
        'outline': [o['section'] for o in outline],
        'threshold': threshold,
        'clauses': clauses,
    }

    db.execute('UPDATE analyses SET results = ? WHERE id = ?', (json.dumps(results), analysis_id))
    db.commit()

    mapped = sum(1 for c in clauses if c['status'] == 'Mapped')
    return {
        'ok': True, 'results': results,
        'summary': {
            'clause_count': len(clauses),
            'mapped': mapped,
            'unmapped': len(clauses) - mapped,
            'sections': results['module5_crosswalk']['source_sections'],
        },
    }


@app.route('/module5/crosswalk/<analysis_id>', methods=['POST'])
def module5_crosswalk(analysis_id):
    out = _run_module5_crosswalk(analysis_id)
    if 'error' in out:
        return jsonify({'error': out['error']}), (404 if out['error'] == 'analysis not found' else 400)
    return jsonify(out['summary'])


@app.route('/module5/<record_id>')
def module5_page(record_id):
    db = get_db()
    row = db.execute('SELECT * FROM analyses WHERE id = ?', (record_id,)).fetchone()
    if not row:
        return make_response('Analysis not found', 404)
    rec = dict(row)
    results = _load_results(rec['results'])
    m5 = results.get('module5_crosswalk', {})
    if not m5.get('clauses'):
        out = _run_module5_crosswalk(record_id)
        if 'error' in out:
            return make_response(out['error'], 500)
        results = out['results']
        m5 = results.get('module5_crosswalk', {})
    checklist_items = []
    for cat in ('financial', 'legal', 'operations', 'technical'):
        for item in results.get('strategic_checklist', {}).get(cat, []):
            if isinstance(item, dict) and item.get('item'):
                checklist_items.append({
                    'category': cat,
                    'item': item.get('item', ''),
                    'status': item.get('module5_status', ''),
                    'draft_section': item.get('module5_draft_section', ''),
                    'similarity': item.get('module5_similarity', 0),
                })
    mapped = sum(1 for c in m5.get('clauses', []) if c.get('status') == 'Mapped')
    return render_template('module5.html',
        record_id=rec['id'],
        title=rec['title'],
        filename=rec['filename'],
        provider=rec['provider'],
        m5=m5,
        clause_count=len(m5.get('clauses', [])),
        mapped=mapped,
        unmapped=len(m5.get('clauses', [])) - mapped,
        checklist_items=checklist_items,
    )


# =============================================================================
# Module 6 — Amendment / Version Delta Tracking
# -----------------------------------------------------------------------------
# A re-upload tagged as an amendment of a prior analysis is diffed FIELD BY FIELD
# against the most recent prior analysis of the same case. Only the structured
# JSON results are compared (never the raw document text), so harmless
# reformatting of the source does not surface as a change.
#
# This is deliberately NOT the exact-hash re-upload cache: that cache detects
# IDENTICAL byte re-uploads and skips re-analysis. Module 6 instead detects and
# links DIFFERENT documents that belong to the same case (original + amendment).
#
# Phase 3's summary call reuses Module 4's dedicated Groq account
# (GROQ_MODULE4_API_KEY / llama-3.3-70b-versatile) so it stays isolated from
# Module 3's latency-sensitive key. No cross-provider fallback is used.
# =============================================================================

_M6_MONTHS = (
    r'january|february|march|april|may|june|july|august|september|october|'
    r'november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec'
)
_M6_DATE_TOKEN_RE = re.compile(
    r'\b('
    r'\d{4}-\d{1,2}-\d{1,2}'                                        # 2026-07-16
    r'|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}'                              # 07/16/2026
    r'|' + _M6_MONTHS + r'\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?'  # July 16, 2026
    r'|\d{1,2}(?:st|nd|rd|th)?\s+' + _M6_MONTHS + r'\.?(?:,?\s+\d{2,4})?'  # 16 July 2026
    r')\b',
    re.IGNORECASE,
)
_M6_DEADLINE_KW = re.compile(
    r'\b(deadline|due|submit|submission|bid|closing|award|respond|response|'
    r'proposal|quote|date|schedule|milestone|calendar|by)\b', re.IGNORECASE)
_M6_NUM_RE = re.compile(r'\d[\d,]*\.?\d*(?:[a-z%]+)?', re.IGNORECASE)

# Structural (as opposed to descriptive) fields per item type. A Modified item is
# High-severity when a structural field changed OR a date/amount changed; Low only
# when the item's descriptive prose alone changed.
_M6_STRUCTURAL_FIELDS = {
    'requirement': ['req_id', 'category', 'section_ref', 'page_num', 'initial_status', 'verified_status', 'status', 'module5_status'],
    'checklist':   ['item', 'status', 'risk_level', 'category', 'verification_status', 'module5_status'],
    'risk':        ['category', 'severity'],
    'deliverable': ['deliverable', 'parent_title', 'parent_number', 'title', 'name', 'page_number'],
    'timeline':    ['milestone', 'date_reference'],
    'default':     [],
}

# Keys added to strategic-checklist items by later modules (Module 2 verification,
# Module 5 crosswalk). They are derived state, not RFP content, so a version that has
# not been re-verified/re-crosswalked must not diff against them as if content changed.
_M6_DERIVED_CHECKLIST_KEYS = frozenset({
    'company_evidence', 'verification_status',
    'module5_status', 'module5_draft_section', 'module5_similarity',
})


def _m6_norm(value) -> str:
    if value is None:
        return ''
    if isinstance(value, (list, dict)):
        value = json.dumps(value, sort_keys=True)
    return re.sub(r'\s+', ' ', str(value)).strip().lower()


def _m6_fp(value) -> str:
    return hashlib.sha1(_m6_norm(value).encode('utf-8')).hexdigest()[:12]


def _m6_key_norm(text: str) -> str:
    """Normalize identity-bearing text for join keys: strip numbers, dates, and
    punctuation so a purely wording change still matches the same item, while a
    date/amount change keeps the same key and surfaces as a Modified item."""
    s = _m6_norm(text)
    s = _M6_NUM_RE.sub(' ', s)
    s = _M6_DATE_TOKEN_RE.sub(' ', s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _m6_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ' '.join(_m6_text(v) for v in value.values())
    if isinstance(value, list):
        return ' '.join(_m6_text(v) for v in value)
    return str(value)


def _m6_extract_dates(text: str) -> set:
    text = text or ''
    dates = {m.lower() for m in _M6_DATE_TOKEN_RE.findall(text)}
    for tok in _M6_NUM_RE.findall(text):
        if re.fullmatch(r'\d{4}', tok) and (re.search(_M6_MONTHS, text, re.I) or _M6_DEADLINE_KW.search(text)):
            dates.add(tok)
    return dates


def _m6_value_tokens(text: str) -> set:
    return set(_M6_NUM_RE.findall(text or ''))


def _m6_is_material_text_change(old: str, new: str) -> bool:
    """True when the only structural signal a change carries is a date or amount
    difference (both make the change material → High severity)."""
    return (_m6_extract_dates(old) != _m6_extract_dates(new)
            or _m6_value_tokens(old) != _m6_value_tokens(new))


def _m6_disp(value) -> str:
    if value is None:
        return '(empty)'
    if isinstance(value, (list, dict)):
        value = json.dumps(value, sort_keys=True)
    return str(value).strip()


def _m6_item_changes(old, new, type_key: str):
    """Compare two same-key items. Returns (severity|None, detail|None)."""
    if isinstance(old, dict) and isinstance(new, dict):
        keys = set(old) | set(new)
        changed_fields = [k for k in keys if _m6_norm(old.get(k)) != _m6_norm(new.get(k))]
        if type_key == 'checklist':
            changed_fields = [k for k in changed_fields if k not in _M6_DERIVED_CHECKLIST_KEYS]
        if not changed_fields:
            return None, None
        structural = _M6_STRUCTURAL_FIELDS.get(type_key, [])
        structural_changed = any(k in structural for k in changed_fields)
        material = any(_m6_is_material_text_change(str(old.get(k, '')), str(new.get(k, ''))) for k in changed_fields)
        if type_key == 'deliverable' and _m6_deliv_sub_names(old) != _m6_deliv_sub_names(new):
            structural_changed = True
            material = material or _m6_is_material_text_change(_m6_text(old), _m6_text(new))
        detail_parts = []
        for k in changed_fields[:4]:
            o = _m6_disp(old.get(k))[:80] or '(empty)'
            n = _m6_disp(new.get(k))[:80] or '(empty)'
            detail_parts.append(f"{k}: {o} → {n}")
        severity = 'High' if (structural_changed or material) else 'Low'
        return severity, ', '.join(detail_parts)
    o, n = _m6_text(old), _m6_text(new)
    if _m6_norm(o) == _m6_norm(n):
        return None, None
    severity = 'High' if _m6_is_material_text_change(o, n) else 'Low'
    return severity, f"{o[:80]} → {n[:80]}"


def _m6_diff_list(section, base_items, new_items, key_fn, name_fn, type_key, changes, _add):
    def _build_map(items):
        m = {}
        counts = {}
        for it in items:
            k = key_fn(it)
            if k in m:
                counts[k] = counts.get(k, 0) + 1
                k = f'{k}#x{counts[k]}'
            m[k] = it
        return m
    bm = _build_map(base_items if isinstance(base_items, list) else [])
    nm = _build_map(new_items if isinstance(new_items, list) else [])
    for k in nm:
        if k not in bm:
            _add({'section': section, 'change_type': 'Added', 'key': k,
                  'label': name_fn(nm[k]), 'detail': _m6_text(nm[k])[:200]})
    for k in bm:
        if k not in nm:
            _add({'section': section, 'change_type': 'Removed', 'key': k,
                  'label': name_fn(bm[k]), 'detail': _m6_text(bm[k])[:200]})
    for k in bm:
        if k in nm:
            sev, detail = _m6_item_changes(bm[k], nm[k], type_key)
            if sev:
                _add({'section': section, 'change_type': 'Modified', 'key': k,
                      'label': name_fn(nm[k]), 'severity': sev, 'detail': detail})


def _m6_req_key_text(it):
    if isinstance(it, dict):
        return ' | '.join(_m6_key_norm(str(it.get(k, ''))) for k in ('category', 'description', 'section_ref'))
    return _m6_key_norm(str(it))


def _m6_req_name(it):
    if isinstance(it, dict):
        rid = it.get('req_id', '')
        desc = (it.get('description') or '')[:100]
        return f"{rid}: {desc}" if rid else desc
    return str(it)[:120]


def _m6_timeline_key(it):
    if isinstance(it, dict):
        return _m6_fp(_m6_key_norm(it.get('milestone', '') or ''))
    return _m6_fp(_m6_key_norm(str(it)))


def _m6_timeline_name(it):
    if isinstance(it, dict):
        return it.get('milestone', '') or it.get('date_reference', '')
    return str(it)


def _m6_deliv_name(it):
    if isinstance(it, dict):
        return it.get('deliverable') or it.get('parent_title') or it.get('title') or str(it)[:80]
    return str(it)[:80]


def _m6_deliv_sub_names(it):
    if not isinstance(it, dict):
        return []
    subs = it.get('sub_deliverables') or it.get('children') or []
    out = []
    for s in subs:
        if isinstance(s, dict):
            out.append(_m6_fp(s.get('name') or s.get('title') or str(s)))
        else:
            out.append(_m6_fp(s))
    return sorted(out)


def _m6_checklist_item_text(it):
    if isinstance(it, dict):
        return it.get('item', '') or str(it)
    return str(it)


def _m6_diff_compliance(bc, nc, changes, _add):
    if isinstance(bc, list) and isinstance(nc, list):
        _m6_diff_list('compliance', bc, nc,
                      key_fn=lambda it: _m6_fp(_m6_key_norm(str(it))),
                      name_fn=lambda it: str(it)[:160],
                      type_key='default', changes=changes, _add=_add)
        return
    if not isinstance(bc, dict) or not isinstance(nc, dict):
        return
    for k in sorted(set(bc) | set(nc)):
        o, n = str(bc.get(k, '')), str(nc.get(k, ''))
        if _m6_norm(o) == _m6_norm(n):
            continue
        if k in nc and k not in bc:
            _add({'section': 'compliance', 'change_type': 'Added', 'label': k, 'detail': n[:120]})
        elif k in bc and k not in nc:
            _add({'section': 'compliance', 'change_type': 'Removed', 'label': k, 'detail': o[:120]})
        else:
            sev = 'High' if _m6_is_material_text_change(o, n) else 'Low'
            _add({'section': 'compliance', 'change_type': 'Modified', 'label': k, 'severity': sev,
                  'detail': f"{o[:80]} → {n[:80]}"})


def _m6_diff_checklist(bc, nc, changes, _add):
    if isinstance(bc, list) and isinstance(nc, list):
        _m6_diff_list('strategic_checklist', bc, nc,
                      key_fn=lambda it: _m6_fp(_m6_key_norm(_m6_checklist_item_text(it))),
                      name_fn=lambda it: _m6_checklist_item_text(it)[:120],
                      type_key='checklist', changes=changes, _add=_add)
        return
    if not isinstance(bc, dict) or not isinstance(nc, dict):
        return
    o, n = str(bc.get('executive_summary', '')), str(nc.get('executive_summary', ''))
    if _m6_norm(o) != _m6_norm(n) and (o or n):
        sev = 'High' if _m6_is_material_text_change(o, n) else 'Low'
        _add({'section': 'strategic_checklist', 'change_type': 'Modified', 'label': 'executive_summary',
              'severity': sev, 'detail': f"{o[:80]} → {n[:80]}"})
    for cat in ('financial', 'legal', 'operations', 'technical'):
        _m6_diff_list(f'strategic_checklist.{cat}', bc.get(cat, []), nc.get(cat, []),
                      key_fn=lambda it: _m6_fp(_m6_key_norm(_m6_checklist_item_text(it))),
                      name_fn=lambda it: _m6_checklist_item_text(it)[:120],
                      type_key='checklist', changes=changes, _add=_add)


def _m6_diff_gonogo(bg, ng, changes, _add):
    if not isinstance(bg, dict) or not isinstance(ng, dict):
        return
    for k in ('score', 'verdict'):
        o, n = bg.get(k), ng.get(k)
        if _m6_norm(o) != _m6_norm(n) and not (o is None and n is None):
            _add({'section': 'go_nogo', 'change_type': 'Modified', 'label': f'go_nogo.{k}', 'severity': 'High',
                  'detail': f"{o} → {n}"})
    o, n = str(bg.get('summary', '')), str(ng.get('summary', ''))
    if _m6_norm(o) != _m6_norm(n) and (o or n):
        sev = 'High' if _m6_is_material_text_change(o, n) else 'Low'
        _add({'section': 'go_nogo', 'change_type': 'Modified', 'label': 'go_nogo.summary', 'severity': sev,
              'detail': f"{o[:80]} → {n[:80]}"})
    _m6_diff_list('go_nogo.reasons', bg.get('reasons', []), ng.get('reasons', []),
                  key_fn=lambda it: _m6_fp(_m6_key_norm(_m6_text(it))),
                  name_fn=lambda it: _m6_text(it)[:120],
                  type_key='default', changes=changes, _add=_add)


def _m6_diff_verification(section, bv, nv, changes, _add):
    if not isinstance(bv, dict) and not isinstance(nv, dict):
        return
    bv = bv or {}
    nv = nv or {}
    for rid in nv:
        if rid not in bv:
            _add({'section': section, 'change_type': 'Added', 'label': rid,
                  'detail': _m6_text(nv[rid])[:200]})
    for rid in bv:
        if rid not in nv:
            _add({'section': section, 'change_type': 'Removed', 'label': rid,
                  'detail': _m6_text(bv[rid])[:200]})
        else:
            sev, detail = _m6_item_changes(bv[rid], nv[rid], 'requirement')
            if sev:
                _add({'section': section, 'change_type': 'Modified', 'label': rid,
                      'severity': sev, 'detail': detail})


def _m6_diff(base: dict, new: dict) -> dict:
    """Field-by-field structured diff between two analysis result dicts."""
    changes = []

    def _add(c):
        c.setdefault('severity', 'High' if c.get('change_type') in ('Added', 'Removed') else 'Low')
        changes.append(c)

    _m6_diff_list('timeline', base.get('timeline', []), new.get('timeline', []),
                  key_fn=_m6_timeline_key, name_fn=_m6_timeline_name,
                  type_key='timeline', changes=changes, _add=_add)
    _m6_diff_list('requirements', base.get('requirements', []), new.get('requirements', []),
                  key_fn=lambda it: _m6_fp(_m6_req_key_text(it)), name_fn=_m6_req_name,
                  type_key='requirement', changes=changes, _add=_add)
    _m6_diff_list('deliverables', base.get('deliverables', []), new.get('deliverables', []),
                  key_fn=lambda it: _m6_fp(_m6_key_norm(_m6_deliv_name(it))),
                  name_fn=_m6_deliv_name, type_key='deliverable', changes=changes, _add=_add)
    _m6_diff_list('risks', base.get('risks', []), new.get('risks', []),
                  key_fn=lambda it: _m6_fp(_m6_key_norm(_m6_text(it))),
                  name_fn=lambda it: _m6_text(it)[:120],
                  type_key='risk', changes=changes, _add=_add)
    for section in ('key_requirements', 'evaluation_criteria'):
        _m6_diff_list(section, base.get(section, []), new.get(section, []),
                      key_fn=lambda it: _m6_fp(_m6_key_norm(str(it))),
                      name_fn=lambda it: str(it)[:160],
                      type_key='default', changes=changes, _add=_add)
    _m6_diff_compliance(base.get('compliance', {}), new.get('compliance', {}), changes, _add)
    _m6_diff_checklist(base.get('strategic_checklist', {}), new.get('strategic_checklist', {}), changes, _add)
    _m6_diff_gonogo(base.get('go_nogo', {}), new.get('go_nogo', {}), changes, _add)
    # Verification sections are produced by Module 2 only. A newly uploaded amendment
    # has not been re-verified yet, so an absent new-side means "not re-run", not
    # "everything removed".
    if new.get('module2_verification'):
        _m6_diff_verification('module2_verification', base.get('module2_verification', {}),
                              new.get('module2_verification', {}), changes, _add)
        _m6_diff_verification('module2_checklist_verification', base.get('module2_checklist_verification', {}),
                              new.get('module2_checklist_verification', {}), changes, _add)
    o, n = str(base.get('summary', '')), str(new.get('summary', ''))
    if _m6_norm(o) != _m6_norm(n) and (o or n):
        sev = 'High' if _m6_is_material_text_change(o, n) else 'Low'
        _add({'section': 'summary', 'change_type': 'Modified', 'label': 'summary', 'severity': sev,
              'detail': f"{o[:80]} → {n[:80]}"})

    return {
        'changes': changes,
        'high_count': sum(1 for c in changes if c['severity'] == 'High'),
        'low_count': sum(1 for c in changes if c['severity'] == 'Low'),
    }


# --- Case linking (Phase 1) ---

def _m6_resolve_case_root(db, record_id: str):
    """Resolve an arbitrary record id to the ROOT id of its case group, so chains
    never form: every member of a case points back at the original analysis."""
    if not record_id:
        return None
    row = db.execute('SELECT case_id FROM analyses WHERE id = ?', (record_id,)).fetchone()
    if row is None:
        return None
    return row['case_id'] or record_id


def _m6_find_baseline(db, root_id: str, current_id: str | None = None):
    """Most recent prior analysis in the case group (root + all amendments of it)."""
    rows = db.execute(
        'SELECT * FROM analyses WHERE (id = ? OR case_id = ?) ORDER BY timestamp DESC LIMIT 10',
        (root_id, root_id)
    ).fetchall()
    for r in rows:
        if r['id'] != current_id:
            return dict(r)
    return None


# --- Phase 3: delta summary via Module 4's Groq client/key ---

_M6_SUMMARY_PROMPT = """\
You are an RFP amendment summarizer. A user compared two versions of the same RFP \
(baseline = original, new = amendment). Below is a JSON list of HIGH-severity \
structured changes detected by a diff engine. Write a concise executive summary \
(1-3 sentences) of the changes that most urgently affect bid preparation: deadline \
changes first, then added or removed requirements, then anything else. Do not \
invent changes. Return ONLY a JSON object: {"summary": "<text>"}."""


def _m6_call_groq(system_prompt: str, user_msg: str):
    """Module 6's summary call. Reuses Module 4's dedicated Groq account directly
    (GROQ_MODULE4_API_KEY / llama-3.3-70b-versatile). Deliberately NOT Module 3's
    key — Module 3 is latency-sensitive live chat and must stay isolated. Returns
    (raw_text|None, usage_dict|None)."""
    if not MODULE4_GROQ_ENABLED or not _MODULE4_GROQ_KEY:
        print("[MODULE6-DELTA] Summary call skipped — Module 4 Groq disabled or no key")
        return None, None
    client = groq.Groq(api_key=_MODULE4_GROQ_KEY)
    try:
        response = client.chat.completions.create(
            model=_MODULE4_GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1024,
        )
        raw = (response.choices[0].message.content or '').strip()
        usage = getattr(response, 'usage', None)
        usage_dict = {
            'prompt_tokens': getattr(usage, 'prompt_tokens', None),
            'completion_tokens': getattr(usage, 'completion_tokens', None),
            'total_tokens': getattr(usage, 'total_tokens', None),
        }
        print(f"[MODULE6-DELTA] Summary call: prompt_tokens={usage_dict['prompt_tokens']} "
              f"completion_tokens={usage_dict['completion_tokens']} total_tokens={usage_dict['total_tokens']} "
              f"(model={_MODULE4_GROQ_MODEL}, provider=Groq-Module4)")
        return raw, usage_dict
    except Exception as e:
        print(f"[MODULE6-DELTA] Summary call failed: {type(e).__name__}: {str(e)[:200]}")
        return None, None


def _m6_generate_summary(high_changes: list, baseline_title: str):
    """One LLM call (via Module 4's Groq client/key) condensing High-severity
    changes into a short human-readable summary. Falls back to a local rule-based
    sentence if Groq is unavailable — no cross-provider fallback (Module 6 is
    restricted to Module 4's key)."""
    compact = []
    for c in high_changes[:20]:
        compact.append({
            'section': c.get('section'),
            'type': c.get('change_type'),
            'label': c.get('label', ''),
            'detail': (c.get('detail') or '')[:280],
        })
    user_msg = json.dumps({'baseline': baseline_title, 'high_severity_changes': compact}, ensure_ascii=False)
    raw, usage = _m6_call_groq(_M6_SUMMARY_PROMPT, user_msg)
    if raw:
        parsed = parse_resilient_json(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get('summary'), str) and parsed['summary'].strip():
            return parsed['summary'].strip(), usage
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            s = parsed[0].get('summary')
            if isinstance(s, str) and s.strip():
                return s.strip(), usage
    parts = []
    for c in high_changes[:6]:
        parts.append(f"{c.get('section')}: {(c.get('detail') or '')[:160]}")
    tail = f" (+{len(high_changes) - 6} more)" if len(high_changes) > 6 else ""
    return "High-severity changes: " + "; ".join(parts) + tail, None


def _m6_build_delta(amends_case: str, new_results: dict, current_id: str | None = None):
    """Entry point used by /upload. Returns (resolved_case_root|None, delta_dict|None).
    Only runs when the upload was explicitly tagged as an amendment of an existing
    case. The diff uses the analysis results ALREADY produced by Module 1's normal
    extraction path — there is no special 'amendment mode' extraction."""
    amends_case = (amends_case or '').strip()
    if not amends_case:
        return None, None
    db = get_db()
    root = _m6_resolve_case_root(db, amends_case)
    if not root:
        return None, None
    baseline = _m6_find_baseline(db, root, current_id=current_id)
    if not baseline:
        return root, None
    base_results = _load_results(baseline['results'])
    diff = _m6_diff(base_results, new_results)
    delta = {
        'baseline_id': baseline['id'],
        'baseline_title': baseline.get('title') or baseline.get('filename'),
        'baseline_timestamp': baseline.get('timestamp', ''),
        'changes': diff['changes'],
        'high_count': diff['high_count'],
        'low_count': diff['low_count'],
    }
    if diff['changes']:
        summary, usage = _m6_generate_summary(
            [c for c in diff['changes'] if c['severity'] == 'High'], delta['baseline_title'])
        delta['summary'] = summary
        delta['usage'] = usage
        print(f"[MODULE6-DELTA] Amendment of {baseline['id']}: "
              f"{diff['high_count']} high / {diff['low_count']} low changes")
    else:
        delta['summary'] = 'No structured changes detected between the two versions.'
        delta['usage'] = None
        print(f"[MODULE6-DELTA] No structured changes vs baseline {baseline['id']}")
    return root, delta


if __name__ == '__main__':
    app.run(debug=True)
