"""CLI test harness for the RFP Automation Portal backend.

Runs each module's backend functions directly (no HTTP) and prints
per-step timing plus which AI provider/API each step used.

Examples:
  python cli_modules.py --module 1 --file "path\\to\\rfp.pdf" --store
  python cli_modules.py --module 2 --case 7c9d9c6e
  python cli_modules.py --module 3 --case 7c9d9c6e --question "What are the deadlines?"
  python cli_modules.py --module 4 --case 7c9d9c6e
  python cli_modules.py --module 5 --case 7c9d9c6e
  python cli_modules.py --module 6 --case 7c9d9c6e --file "path\\to\\amendment.pdf" --provider openrouter
  python cli_modules.py --all --case 7c9d9c6e --module 5
"""
import argparse
import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import app as A

RESULTS = []


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def step(name, api, note=""):
    RESULTS.append((name, api, note))
    log(f"STEP {name}: api={api}  {note}")


def _case_dict(db, case_id):
    row = db.execute('SELECT * FROM analyses WHERE id = ?', (case_id,)).fetchone()
    if not row:
        raise SystemExit(f"Case {case_id} not found in database.")
    rec = dict(row)
    return rec, (rec.get('results') or '{}')


def _store(db, title, filename, provider, digest, word_count, results, session_id, case_id, delta):
    rid = str(uuid.uuid4())[:8]
    db.execute(
        'INSERT INTO analyses (id, title, filename, provider, timestamp, hash, word_count, results, session_id, case_id, delta) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (rid, title, filename, provider, time.strftime('%Y-%m-%dT%H:%M:%S'),
         digest, word_count, json.dumps(results), session_id, case_id,
         json.dumps(delta) if delta else None))
    db.commit()
    return rid


def run_module1(args, db):
    fpath = args.file
    if not fpath or not os.path.exists(fpath):
        raise SystemExit("--file <path> is required for module 1.")
    fname = os.path.basename(fpath)

    t0 = time.time()
    text = A.get_document_text(fpath)
    step("M1 extract text", "pypdf", f"chars={len(text)} words={len(text.split())} ({time.time()-t0:.1f}s)")

    target = os.path.join(A.app.config['UPLOAD_FOLDER'], fname)
    if not os.path.exists(target):
        shutil.copy2(fpath, target)
    digest = __import__('hashlib').sha256(open(fpath, 'rb').read()).hexdigest()[:16]
    wc = len(text.split())

    t0 = time.time()
    if args.provider == 'openrouter':
        results = A.analyze_rfp_openrouter('', text_override=text, filenames=[fname])
        api = "OpenRouter " + A.OPENROUTER_DEFAULT_MODEL
    else:
        results = A.analyze_rfp('', text_override=text, filenames=[fname])
        api = "Gemini gemini-3.5-flash"
    step("M1 full analysis", api, f"{len(results.get('sections_analyzed', []))} sections, {time.time()-t0:.1f}s")

    t0 = time.time()
    reqs = A._extract_requirements_single(text, provider='gemini')
    norm = A._normalize_requirements(reqs)
    results['requirements'] = norm
    step("M1 requirements extraction", "Cloudflare @cf/meta/llama-3.1-8b-instruct",
         f"{len(norm)} requirements ({time.time()-t0:.1f}s)")

    if args.store:
        title = args.title or fname.rsplit('.', 1)[0].replace('-', ' ').replace('_', ' ')
        rid = _store(db, title, fname, args.provider, digest, wc, results, args.session, None, None)
        log(f"STORED case id: {rid}  ->  /view/{rid}")
    else:
        log("Not stored (pass --store to insert into analyses.db)")
    return results


def run_module2(args, db):
    rec, results_json = _case_dict(db, args.case)
    results = json.loads(results_json)
    profile = A._load_company_profile()
    facts = A._flatten_profile_to_facts(profile)
    if not facts or all(not f.get('text', '').strip() for f in facts):
        raise SystemExit("Company profile is empty. Populate /company_profile first.")

    reqs = results.get('requirements', [])
    if not reqs:
        raise SystemExit("No requirements found for this case. Run module 1 or extract requirements first.")

    t0 = time.time()
    verif = A._verify_all_requirements(reqs, profile, args.provider)
    from collections import Counter
    step("M2 verify requirements", "Cloudflare embeddings + verify / BM25",
         f"verified={len(verif)} statuses={dict(Counter(v.get('status', '?') for v in verif.values()))} ({time.time()-t0:.1f}s)")

    t0 = time.time()
    chk = A._verify_checklist_items(results.get('strategic_checklist', {}), profile, args.provider)
    step("M2 verify checklist", "Cloudflare embeddings + verify / BM25", f"checklist_verified={len(chk)} ({time.time()-t0:.1f}s)")

    results['module2_verification'] = verif
    results['module2_checklist_verification'] = chk
    db.execute('UPDATE analyses SET results=? WHERE id=?', (json.dumps(results), args.case))
    db.commit()
    log(f"Updated case {args.case} in DB.")


def run_module3(args, db):
    rec, results_json = _case_dict(db, args.case)
    case = json.loads(results_json)

    q = args.question or "What are the submission deadlines and required forms?"
    t0 = time.time()
    r = A._sentinel_direct_match(q, case)
    step("M3 direct match", "local (keyword/bigram scoring)",
         ("hit" if r else "no direct match") + f" ({time.time()-t0:.1f}s)")

    if args.groq or not r:
        t0 = time.time()
        try:
            r2 = A._sentinel_fallback_groq(q, case)
            step("M3 Groq fallback", "Groq llama-3.3-70b-versatile",
                 f"confidence={r2.get('confidence')} fields={r2.get('cited_fields', [])} ({time.time()-t0:.1f}s)")
        except Exception as e:
            step("M3 Groq fallback", "ERR", f"{type(e).__name__}: {str(e)[:150]}")

    t0 = time.time()
    try:
        r4 = A._sentinel_rehearse(q, case)
        step("M3 rehearsal", "Groq llama-3.3-70b-versatile", f"len={len(r4.get('answer', ''))} ({time.time()-t0:.1f}s)")
    except Exception as e:
        step("M3 rehearsal", "ERR", f"{type(e).__name__}: {str(e)[:150]}")

    t0 = time.time()
    try:
        cards = A._sentinel_check_partner_matches(args.case, case)
        step("M3 partner matching", "rule-based gap detection", f"cards={len(cards)} ({time.time()-t0:.1f}s)")
    except Exception as e:
        step("M3 partner matching", "ERR", f"{type(e).__name__}: {str(e)[:150]}")


def run_module4(args, db):
    rec, results_json = _case_dict(db, args.case)
    fname = (rec['filename'] or '').split(',')[0].strip()
    fpath = os.path.join(A.app.config['UPLOAD_FOLDER'], fname)
    if not os.path.exists(fpath):
        raise SystemExit(f"Source file not found on disk: {fpath}")

    t0 = time.time()
    text = A.get_document_text(fpath)
    qa = A._module4_extract_qa(text[:40000])
    step("M4 Q&A extraction", "Groq (Module4) llama-3.3-70b-versatile", f"qa_pairs={len(qa)} ({time.time()-t0:.1f}s)")

    t0 = time.time()
    draft = "We think our team can deliver the solution. We believe we are the best fit for this project."
    adapted = A._module4_adapt_tone(draft)
    step("M4 tone adaptation", "Groq (Module4) llama-3.3-70b-versatile", f"len={len(adapted or '')} ({time.time()-t0:.1f}s)")


def run_module5(args, db):
    t0 = time.time()
    m5 = A._run_module5_crosswalk(args.case)
    if m5.get('error'):
        step("M5 crosswalk", "ERR", m5['error'])
    else:
        s = m5.get('summary', {})
        step("M5 crosswalk", "Cloudflare embeddings + Groq(Module4)",
             f"clauses={s.get('clause_count')} mapped={s.get('mapped')} unmapped={s.get('unmapped')} ({time.time()-t0:.1f}s)")
        if s.get('note'):
            log(f"  note: {s['note']}")


def run_module6(args, db):
    rec, results_json = _case_dict(db, args.case)
    fpath = args.file
    if not fpath or not os.path.exists(fpath):
        raise SystemExit("--file <amendment-document> is required for module 6.")
    fname = os.path.basename(fpath)

    t0 = time.time()
    text = A.get_document_text(fpath)
    if args.provider == 'openrouter':
        amend = A.analyze_rfp_openrouter('', text_override=text, filenames=[fname])
        api = "OpenRouter " + A.OPENROUTER_DEFAULT_MODEL
    else:
        amend = A.analyze_rfp('', text_override=text, filenames=[fname])
        api = "Gemini gemini-3.5-flash"
    amend['requirements'] = A._normalize_requirements(amend.get('requirements') or amend.get('key_requirements'))
    step("M6 amendment analysis", api, f"deliverables={len(amend.get('deliverables', []))} ({time.time()-t0:.1f}s)")

    amend_id = str(uuid.uuid4())[:8]
    t0 = time.time()
    root, delta = A._m6_build_delta(args.case, amend, current_id=amend_id)
    step("M6 build delta + summary", "local diff + Groq(Module4) summary",
         f"root={root} high={delta.get('high_count') if delta else None} low={delta.get('low_count') if delta else None} ({time.time()-t0:.1f}s)")
    if delta and delta.get('summary'):
        log(f"  summary: {delta['summary'][:220]}")

    digest = __import__('hashlib').sha256(open(fpath, 'rb').read()).hexdigest()[:16]
    rid = _store(db, rec['title'] + ' [AMENDMENT]', fname, args.provider, digest, len(text.split()),
                 amend, rec['session_id'], args.case, delta)
    log(f"STORED amendment case id: {rid}  ->  /view/{rid}  (delta vs baseline {args.case})")


def main():
    p = argparse.ArgumentParser(description="RFP Automation Portal — backend module test CLI")
    p.add_argument('--module', type=int, choices=[1, 2, 3, 4, 5, 6], help='Module to test')
    p.add_argument('--all', action='store_true', help='Run all applicable modules for a case')
    p.add_argument('--case', help='DB record id (required for modules 2-6)')
    p.add_argument('--file', help='Path to a document (required for modules 1 and 6)')
    p.add_argument('--provider', default='gemini', choices=['gemini', 'openrouter'])
    p.add_argument('--question', help='Question for module 3')
    p.add_argument('--groq', action='store_true', help='Force Groq fallback in module 3')
    p.add_argument('--store', action='store_true', help='Store module-1 result in DB')
    p.add_argument('--title', help='Title when storing module-1 result')
    p.add_argument('--session', default='cli-session', help='session_id when storing')
    args = p.parse_args()

    with A.app.app_context():
        db = A.get_db()
        if args.all:
            run_module2(args, db)
            run_module3(args, db)
            run_module4(args, db)
            run_module5(args, db)
        elif args.module == 1:
            run_module1(args, db)
        elif args.module == 2:
            run_module2(args, db)
        elif args.module == 3:
            run_module3(args, db)
        elif args.module == 4:
            run_module4(args, db)
        elif args.module == 5:
            run_module5(args, db)
        elif args.module == 6:
            run_module6(args, db)
        else:
            p.print_help()
            return

    print("\n==== TIMING SUMMARY ====")
    for name, api, note in RESULTS:
        print(f"  {name:28} {api:50} {note}")

    print("\nVerify in browser:  http://127.0.0.1:5000/history")


if __name__ == '__main__':
    main()
