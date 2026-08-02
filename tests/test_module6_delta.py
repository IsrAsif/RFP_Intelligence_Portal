"""Tests for Module 6 — Amendment / Version Delta Tracking.

Run directly:  python tests\\test_module6_delta.py
The structured diff engine is pure (no network). The Phase 3 summary call is
exercised with the Module 4 Groq client MOCKED so the suite never hits the wire.
"""
import io
import json
import os
import re
import sys
import sqlite3
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module


def _req(rid, category, description, section_ref, status='Go', extra=None):
    d = {
        'req_id': rid, 'category': category, 'description': description,
        'section_ref': section_ref, 'initial_status': status,
        'what_rfp_says': 'RFP quote for ' + rid,
    }
    if extra:
        d.update(extra)
    return d


def _checklist(item, status='Go', reasoning=None):
    return {
        'item': item, 'status': status,
        'reasoning': reasoning if reasoning is not None else 'Reasoning for ' + item,
        'risk_level': 'Low',
        'rfp_evidence': 'Evidence for ' + item, 'impact_on_bid_strategy': 'Impact',
        'mitigation_strategy': 'Mitigation', 'analysis': 'Analysis',
        'recommendation': 'Recommendation',
    }


def _baseline_v1():
    return {
        'summary': 'Baseline RFP summary.',
        'timeline': [
            {'milestone': 'Submission Deadline', 'date_reference': 'July 16, 2026'},
            {'milestone': 'Questions Due', 'date_reference': 'July 2, 2026'},
        ],
        'requirements': [
            _req('REQ-001', 'Security', 'Provide insurance coverage of $5M', '5.2'),
            _req('REQ-002', 'Reporting', 'Submit monthly status reports', '6.1'),
            _req('REQ-003', 'Compliance', 'Maintain eMMA registration', '7.1'),
            _req('REQ-004', 'Compliance', 'Provide W-9 form', '7.2'),
        ],
        'deliverables': [
            {'deliverable': 'Monthly Status Reports', 'reference': '[doc] Section 6, p. 4',
             'page_number': '4', 'sub_deliverables': [
                 {'name': 'Progress Narrative', 'reference': '6.1'},
                 {'name': 'Expense Detail', 'reference': '6.2'},
             ]},
        ],
        'risks': [
            {'category': 'Timeline', 'description': 'Risk of missing the July 16 deadline', 'severity': 'High'},
        ],
        'evaluation_criteria': ['Technical approach (40 points)'],
        'key_requirements': ['Provide data center hosting'],
        'compliance': {'Financial': 'Payment terms Net 30', 'Legal': 'MD registration required',
                       'Operations': 'Proposals must be sealed and submitted by July 16, 2026',
                       'Technical': 'Must support TLS 1.2'},
        'go_nogo': {'score': 75, 'verdict': 'Go', 'summary': 'Good fit.', 'reasons': [
            {'factor': 'Strategic Alignment', 'detail': 'Strong', 'weight': 'High'}]},
        'strategic_checklist': {
            'executive_summary': 'Overall recommendation is Go.',
            'financial': [_checklist('Payment Terms')],
            'legal': [],
            'operations': [_checklist('Submission Deadlines')],
            'technical': [],
        },
        'module2_verification': {
            'REQ-001': {'status': 'Go', 'risk_level': 'Low', 'reasoning': 'Coverage is sufficient'},
        },
    }


def _amendment_v2():
    v2 = _baseline_v1()
    v2['summary'] = 'Amended RFP summary — deadline extended.'
    # Deadline moved out by 4 days.
    v2['timeline'][0]['date_reference'] = 'July 20, 2026'
    # Requirement added, one removed, one status change, one pure prose edit.
    v2['requirements'].append(_req('REQ-005', 'Security', 'Provide pen test results', '5.9'))
    v2['requirements'] = [r for r in v2['requirements'] if r['req_id'] != 'REQ-002']
    for r in v2['requirements']:
        if r['req_id'] == 'REQ-003':
            r['initial_status'] = 'Escalate'
        if r['req_id'] == 'REQ-004':
            r['what_rfp_says'] = 'RFP quote for REQ-004 updated wording'
    # Insurance amount doubled (material number change on same-keyed item).
    for r in v2['requirements']:
        if r['req_id'] == 'REQ-001':
            r['description'] = 'Provide insurance coverage of $10M'
    # Deliverable sub-item added.
    v2['deliverables'][0]['sub_deliverables'].append({'name': 'Burn Report', 'reference': '6.3'})
    # Risk removed, new risk added.
    v2['risks'] = [{'category': 'Financial', 'description': 'Payment bond requirement added', 'severity': 'Medium'}]
    v2['evaluation_criteria'].append('Past performance (20 points)')
    v2['key_requirements'] = ['Provide data center hosting', 'Provide disaster recovery plan']
    v2['compliance']['Operations'] = 'Proposals must be sealed and submitted by July 20, 2026'
    v2['go_nogo']['score'] = 80
    v2['strategic_checklist']['financial'][0]['status'] = 'Escalate'
    v2['strategic_checklist']['operations'][0]['reasoning'] = 'Deadline moved out; resubmission window extended.'
    v2['module2_verification']['REQ-001']['reasoning'] = 'Coverage now doubled and sufficient'
    return v2


def _find(changes, **kw):
    return [c for c in changes if all(c.get(k) == v for k, v in kw.items())]


class M6DiffEngineTest(unittest.TestCase):
    def test_deadline_change_is_high(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        tl = _find(diff['changes'], section='timeline', change_type='Modified')
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]['severity'], 'High')
        self.assertIn('July 16', tl[0]['detail'])
        self.assertIn('July 20', tl[0]['detail'])

    def test_requirement_added_removed_high(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        added = _find(diff['changes'], section='requirements', change_type='Added')
        removed = _find(diff['changes'], section='requirements', change_type='Removed')
        self.assertEqual(len(added), 1)
        self.assertEqual(len(removed), 1)
        self.assertEqual(added[0]['severity'], 'High')
        self.assertEqual(removed[0]['severity'], 'High')
        self.assertIn('REQ-005', added[0]['label'])
        self.assertIn('REQ-002', removed[0]['label'])

    def test_requirement_modified_status_high_prose_low(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        mods = _find(diff['changes'], section='requirements', change_type='Modified')
        by_label = {c['label']: c for c in mods}
        self.assertIn('REQ-003: Maintain eMMA registration', by_label)
        self.assertEqual(by_label['REQ-003: Maintain eMMA registration']['severity'], 'High')
        self.assertIn('REQ-004: Provide W-9 form', by_label)
        self.assertEqual(by_label['REQ-004: Provide W-9 form']['severity'], 'Low')
        # Amount change ($5M -> $10M) on the same keyed item is material → High.
        self.assertIn('REQ-001: Provide insurance coverage of $10M', by_label)
        self.assertEqual(by_label['REQ-001: Provide insurance coverage of $10M']['severity'], 'High')

    def test_compliance_date_change_high(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        ops = _find(diff['changes'], section='compliance', label='Operations')
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]['severity'], 'High')

    def test_checklist_status_structural_high_reasoning_low(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        fin = _find(diff['changes'], section='strategic_checklist.financial', change_type='Modified')
        self.assertEqual(len(fin), 1)
        self.assertEqual(fin[0]['severity'], 'High')
        ops = _find(diff['changes'], section='strategic_checklist.operations', change_type='Modified')
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]['severity'], 'Low')

    def test_severity_counts(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        self.assertEqual(diff['high_count'] + diff['low_count'], len(diff['changes']))
        self.assertGreater(diff['high_count'], 0)
        self.assertGreater(diff['low_count'], 0)

    def test_identical_versions_no_changes(self):
        diff = app_module._m6_diff(_baseline_v1(), _baseline_v1())
        self.assertEqual(diff['changes'], [])

    def test_gonogo_score_change_high(self):
        v1 = _baseline_v1()
        v2 = _amendment_v2()
        diff = app_module._m6_diff(v1, v2)
        gn = _find(diff['changes'], section='go_nogo', label='go_nogo.score')
        self.assertEqual(len(gn), 1)
        self.assertEqual(gn[0]['severity'], 'High')

    def test_unverified_amendment_no_false_removals(self):
        # Baseline was fully processed (Module 2 verification + Module 5 crosswalk),
        # the new upload is a fresh Module-1-only analysis. Diffing them must not
        # report every verification record as Removed or every checklist item as a
        # High Modified just because the derived keys are missing on the new side.
        base = _baseline_v1()
        base['strategic_checklist']['financial'][0]['company_evidence'] = 'Coverage found'
        base['strategic_checklist']['financial'][0]['verification_status'] = 'Go'
        base['strategic_checklist']['financial'][0]['module5_status'] = 'Mapped'
        base['module2_checklist_verification'] = {
            'CHK-FIN-001': {'status': 'Go', 'risk_level': 'Low',
                            'reasoning': 'chk_FIN Evidence for Payment Terms',
                            'company_evidence': 'Coverage found'},
        }
        fresh = json.loads(json.dumps(base))
        fresh.pop('module2_verification', None)
        fresh.pop('module2_checklist_verification', None)
        for cat in ('financial', 'legal', 'operations', 'technical'):
            for it in fresh.get('strategic_checklist', {}).get(cat, []):
                it.pop('company_evidence', None)
                it.pop('verification_status', None)
                it.pop('module5_status', None)

        diff = app_module._m6_diff(base, fresh)
        self.assertEqual(
            _find(diff['changes'], section='module2_verification'), [])
        self.assertEqual(
            _find(diff['changes'], section='module2_checklist_verification'), [])
        self.assertEqual(
            _find(diff['changes'], section='strategic_checklist.financial'), [])

    def test_both_verified_still_diffs_verification(self):
        base = _baseline_v1()
        new = _amendment_v2()  # inherits base verification, REQ-001 reasoning changed
        diff = app_module._m6_diff(base, new)
        mods = _find(diff['changes'], section='module2_verification')
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]['label'], 'REQ-001')


class M6SummaryTest(unittest.TestCase):
    def test_summary_generated_via_module4_groq(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        high = [c for c in diff['changes'] if c['severity'] == 'High']
        canned = json.dumps({'summary': 'Deadline extended to July 20, 2026; 1 requirement added (REQ-005) and 1 removed.'})
        usage = {'prompt_tokens': 210, 'completion_tokens': 48, 'total_tokens': 258}
        with mock.patch.object(app_module, '_m6_call_groq', return_value=(canned, usage)) as call:
            summary, used = app_module._m6_generate_summary(high, 'Baseline case')
        self.assertIn('July 20', summary)
        self.assertIn('REQ-005', summary)
        self.assertEqual(used['prompt_tokens'], 210)
        self.assertEqual(used['completion_tokens'], 48)
        call.assert_called_once()
        # The call must reuse Module 4's key/model configuration, not Module 3's.
        self.assertIn('high_severity_changes', call.call_args.args[1])

    def test_summary_fallback_without_groq(self):
        diff = app_module._m6_diff(_baseline_v1(), _amendment_v2())
        high = [c for c in diff['changes'] if c['severity'] == 'High']
        with mock.patch.object(app_module, '_m6_call_groq', return_value=(None, None)):
            summary, used = app_module._m6_generate_summary(high, 'Baseline case')
        self.assertIn('High-severity changes', summary)
        self.assertIsNone(used)


class M6CaseLinkingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'm6.db')
        with sqlite3.connect(self.db_path) as conn:
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
                    session_id TEXT NOT NULL,
                    case_id TEXT,
                    delta TEXT
                )
            ''')
        patcher = mock.patch.object(app_module, 'DB_PATH', self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = app_module.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def _insert(self, rid, title, case_id=None, ts=None, results=None):
        db = app_module.get_db()
        db.execute(
            'INSERT INTO analyses (id, title, filename, provider, timestamp, hash, word_count, results, session_id, case_id, delta) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (rid, title, title + '.txt', 'gemini', ts or '2026-07-01T00:00:00',
             'h' + rid, 10, json.dumps(results or _baseline_v1()), 'sess', case_id, None)
        )
        db.commit()

    def test_resolve_root_chains_to_original(self):
        self._insert('BASE01', 'Original RFP', ts='2026-07-01T00:00:00')
        self._insert('AMEND01', 'Amendment 1', case_id='BASE01', ts='2026-07-03T00:00:00')
        db = app_module.get_db()
        self.assertEqual(app_module._m6_resolve_case_root(db, 'BASE01'), 'BASE01')
        self.assertEqual(app_module._m6_resolve_case_root(db, 'AMEND01'), 'BASE01')
        # Baseline = most recent prior analysis in the case group.
        baseline = app_module._m6_find_baseline(db, 'BASE01')
        self.assertEqual(baseline['id'], 'AMEND01')

    def test_build_delta_against_baseline(self):
        self._insert('BASE01', 'Original RFP', ts='2026-07-01T00:00:00')
        canned = json.dumps({'summary': 'Deadline moved to July 20, 2026.'})
        usage = {'prompt_tokens': 300, 'completion_tokens': 45, 'total_tokens': 345}
        with mock.patch.object(app_module, '_m6_call_groq', return_value=(canned, usage)):
            root, delta = app_module._m6_build_delta('BASE01', _amendment_v2())
        self.assertEqual(root, 'BASE01')
        self.assertEqual(delta['baseline_id'], 'BASE01')
        self.assertEqual(delta['summary'], 'Deadline moved to July 20, 2026.')
        self.assertGreater(delta['high_count'], 0)
        self.assertEqual(delta['usage']['prompt_tokens'], 300)

    def test_no_tag_no_delta(self):
        root, delta = app_module._m6_build_delta('', _amendment_v2())
        self.assertIsNone(root)
        self.assertIsNone(delta)


if __name__ == '__main__':
    unittest.main(verbosity=2)
