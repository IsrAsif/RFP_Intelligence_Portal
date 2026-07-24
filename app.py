import os
import io
import re
import json
import uuid
import hashlib
import time
import base64
import random
import sqlite3
from datetime import datetime
from collections import defaultdict

import pypdf
import fitz
import docx
import groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

def _get_env(key: str) -> str:
    v = os.getenv(key, '')
    return v.strip().strip('"').strip("'") if v else v

from flask import Flask, render_template, request, make_response, Response, session, g, url_for
from werkzeug.utils import secure_filename
from typing import List, Union
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError
from xhtml2pdf import pisa

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

def extract_text_from_pdf(file_path: str) -> str:
    text = ""
    with open(file_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page in reader.pages:
            content = page.extract_text()
            if content:
                text += content + "\n"
    return text

def extract_text_from_docx(file_path: str) -> str:
    doc = docx.Document(file_path)
    return "\n".join([paragraph.text for paragraph in doc.paragraphs])

def get_document_text(file_path: str) -> str:
    if file_path.endswith('.pdf'):
        return extract_text_from_pdf(file_path)
    elif file_path.endswith('.docx'):
        return extract_text_from_docx(file_path)
    else:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

def compute_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]

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
    'evaluation_criteria': "evaluation_criteria (array of strings): EXTRACT EVERY scoring metric, point allocation, evaluation factor, and judgment guideline from the RFP. Be exhaustive — include all criteria no matter how minor.",
    'compliance': "compliance (array of strings): EXTRACT ALL compliance requirements across Financial, Legal, Operations, and Technical categories. Be thorough — include every mandatory clause, certification, and regulatory requirement.",
    'risks': "risks (array of strings): EXTRACT ALL key risks from the RFP. Include performance, technical, financial, schedule, and any other risk categories mentioned.",
    'timeline': "timeline (array of strings): EXTRACT EVERY date, milestone, deadline, and time-sensitive requirement mentioned in the RFP. Include submission deadlines, review periods, project phases, and deliverable due dates.",
    'key_requirements': "key_requirements (array of strings): EXTRACT ALL important requirements from the RFP. Be exhaustive — include technical, functional, business, staffing, security, and any other requirements. Do not limit to 5-10.",
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

_TEMPLATE_KEYS = ['summary', 'deliverables', 'evaluation_criteria', 'compliance', 'risks', 'timeline', 'key_requirements', 'go_nogo', 'strategic_checklist', 'sections_analyzed']

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
        elif key == 'sections_analyzed':
            result[key] = sections or filenames or []
        else:
            if isinstance(raw, list):
                result[key] = [str(v) for v in raw if isinstance(v, (str, int, float))]
            elif raw:
                result[key] = [str(raw)]
            else:
                result[key] = []

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
        "Every key MUST be present. OMITTING OR LEAVING A KEY EMPTY IS STRICTLY FORBIDDEN.",
        "Use an empty array [] for any key where no data is found (never omit it).",
        "",
        "CRITICAL — EXTRACTION DIRECTIVE: You MUST extract EVERY item from the document. Do NOT limit, truncate, summarize, or omit anything. If the document contains 200 items, output all 200. Maximum completeness is required.",
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
    # Try truncated JSON recovery for object
    start = s.find('{')
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        good_end = -1
        for i in range(start, len(s)):
            ch = s[i]
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
                if ch in ('{', '['):
                    depth += 1
                elif ch in ('}', ']'):
                    depth -= 1
                    if depth >= 0:
                        good_end = i
        if good_end > start:
            candidate = s[start:good_end + 1]
            miss_brace = candidate.count('{') - candidate.count('}')
            miss_brack = candidate.count('[') - candidate.count(']')
            if miss_brace > 0 or miss_brack > 0:
                candidate += '}' * max(0, miss_brace) + ']' * max(0, miss_brack)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            # Try trimming broken suffix and retry
            for trim in range(1, min(2000, len(candidate))):
                sub = candidate[:-trim]
                mb = sub.count('{') - sub.count('}')
                mbr = sub.count('[') - sub.count(']')
                pad = '}' * max(0, mb) + ']' * max(0, mbr)
                try:
                    return json.loads(sub + pad)
                except json.JSONDecodeError:
                    continue
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
    if not gemini_key:
        raise ValueError("GEMINI_API_KEY is not set in .env file.")

    document_text = _sanitize_text((text_override or get_document_text(file_path))[:MAX_DOC_CHARS])
    print(f"[GEMINI] Input text length: {len(document_text)} chars")

    client = genai.Client(api_key=gemini_key)
    selected_keys = [s for s in (sections or ALL_SECTIONS[:]) if s in SECTION_PROMPTS]
    prompt = _build_prompt(selected_keys)

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    def _call_gemini():
        return client.models.generate_content(
            model='gemini-3.5-flash',
            contents=f"RFP Document:\n\n{document_text}\n\n{prompt}",
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=65536,
            ),
        )

    max_503_retry = 180
    max_429_retry = 300
    start_time = time.time()

    for attempt in range(20):
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_call_gemini)
                response = fut.result(timeout=300)
            raw = response.text
            if raw is None and response.candidates:
                for c in response.candidates:
                    if c.content and c.content.parts:
                        raw = ''.join(getattr(p, 'text', '') or '' for p in c.content.parts)
                        if raw:
                            break
            raw = raw or "{}"
            print(f"[GEMINI] Raw response: {len(raw)} chars, preview: {raw[:300]}...")
            print("[GEMINI] DEBUG FULL:", repr(raw[:1500]))
            parsed = parse_resilient_json(raw)
            result = coerce_analysis(parsed, filenames or [file_path], sections=selected_keys)
            print(f"[GEMINI] Success. Keys: {list(result.keys())}")
            # Diagnostic dump
            os.makedirs('debug_dumps', exist_ok=True)
            ts = time.strftime('%Y%m%d_%H%M%S')
            with open(f'debug_dumps/gemini_raw_{ts}.json', 'w', encoding='utf-8') as df:
                df.write(raw)
            with open(f'debug_dumps/gemini_result_{ts}.json', 'w', encoding='utf-8') as df:
                json.dump(result, df, indent=2, default=str)
            return result
        except ServerError as e:
            print(f"[GEMINI] 503: attempt {attempt}")
            elapsed = time.time() - start_time
            if elapsed >= max_503_retry:
                raise
            time.sleep(min(2 ** (attempt + 1), 30))
        except FuturesTimeout:
            print(f"[GEMINI] Timeout: attempt {attempt}")
            if attempt >= 2:
                raise TimeoutError("Gemini API timed out")
            time.sleep(2 * (attempt + 1))
        except ClientError as e:
            err_str = str(e)
            if '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str:
                elapsed = time.time() - start_time
                if elapsed >= max_429_retry:
                    raise
                wait = min(5 * (2 ** attempt), 120) * random.uniform(0.8, 1.2)
                print(f"[GEMINI] 429: attempt {attempt}, waiting {wait:.0f}s")
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            print(f"[GEMINI] Exception attempt {attempt}: {e}")
            if attempt >= 2:
                raise
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"[GEMINI] Exhausted all retries without successful analysis. Last-seen error logged above.")


def analyze_rfp_groq(file_path: str, text_override: str | None = None, sections: list[str] | None = None, filenames: list[str] | None = None) -> dict:
    api_key = _get_env('GROQ_API_KEY')
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set in .env file.")

    GROQ_MAX_CHARS = 25000  # fits within Groq free tier 12000 token limit
    document_text = _sanitize_text((text_override or get_document_text(file_path))[:GROQ_MAX_CHARS])
    print(f"[GROQ] Input text length: {len(document_text)} chars")

    client = groq.Groq(api_key=api_key)
    selected_keys = [s for s in (sections or ALL_SECTIONS[:]) if s in SECTION_PROMPTS]
    system_prompt = _build_prompt(selected_keys)

    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"RFP Document Text:\n\n{document_text}"},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=4000,
            )
            raw = response.choices[0].message.content or "{}"
            print(f"[GROQ] Raw response: {len(raw)} chars, preview: {raw[:300]}...")
            print("[GROQ] DEBUG FULL:", repr(raw[:1500]))
            parsed = parse_resilient_json(raw)
            result = coerce_analysis(parsed, filenames or [file_path], sections=selected_keys)
            print(f"[GROQ] Success. Keys: {list(result.keys())}")
            # Diagnostic dump
            os.makedirs('debug_dumps', exist_ok=True)
            ts = time.strftime('%Y%m%d_%H%M%S')
            with open(f'debug_dumps/groq_raw_{ts}.json', 'w', encoding='utf-8') as df:
                df.write(raw)
            with open(f'debug_dumps/groq_result_{ts}.json', 'w', encoding='utf-8') as df:
                json.dump(result, df, indent=2, default=str)
            return result
        except Exception as e:
            print(f"[GROQ] Error attempt {attempt}: {type(e).__name__}: {str(e)[:300]}")
            if attempt == 1:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError("Groq analysis failed after retries")

@app.before_request
def ensure_session():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())

@app.route('/')
def index() -> str:
    return render_template('index.html')

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
    for f in files:
        fname = secure_filename(str(f.filename))
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        f.save(fpath)
        saved_filenames.append(fname)
        doc_text = get_document_text(fpath)
        combined_parts.append(f"[DOCUMENT: {fname}]\n{doc_text}")

    combined_text = "\n\n---NEXT DOCUMENT---\n\n".join(combined_parts)
    display_filenames = ', '.join(saved_filenames)

    file_hash = hashlib.sha256(combined_text.encode()).hexdigest()[:16]
    word_count = len(combined_text.split())

    try:
        provider = request.form.get('provider', 'gemini')
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

        if provider == 'groq':
            analysis_results = analyze_rfp_groq('', text_override=combined_text, sections=sections, filenames=saved_filenames)
        else:
            analysis_results = analyze_rfp('', text_override=combined_text, sections=sections, filenames=saved_filenames)

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
        db = get_db()
        db.execute(
            'INSERT INTO analyses (id, title, filename, provider, timestamp, hash, word_count, results, session_id) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (record_id, analysis_title, display_filenames, provider, datetime.now().isoformat(),
             file_hash, word_count, json.dumps(analysis_results), sid)
        )
        db.commit()

        return render_template('results.html', results=analysis_results, filename=display_filenames, record_id=record_id, record_title=analysis_title, provider=provider)
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

    return render_template('compare.html', r1=r1, r2=r2)

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
    return render_template('results.html',
        results=results,
        filename=rec['filename'],
        record_id=rec['id'],
        record_title=rec['title'],
        provider=rec['provider']
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

if __name__ == '__main__':
    app.run(debug=True)
