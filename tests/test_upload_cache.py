"""End-to-end test for the exact whole-document content cache (Part 1 of the
main-pipeline caching task).

Run directly:  python tests\\test_upload_cache.py
Uses Flask's test client against the real /upload route, with the AI analyzers
patched out so no provider is contacted. A temp DB and temp upload dir are used
so the real analyses.db / uploads folder are untouched.
"""
import io
import os
import sys
import sqlite3
import tempfile
import unittest
import contextlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module


ALL_SECTIONS_DEFAULT = (
    'summary,deliverables,evaluation_criteria,compliance,risks,timeline,'
    'key_requirements,go_nogo,strategic_checklist'
)


def _fake_analysis():
    return {
        'summary': 'Fake cached summary.',
        'deliverables': ['Deliverable A'],
        'evaluation_criteria': ['Scoring'],
        'compliance': {'Financial': 'F', 'Legal': 'L', 'Operations': 'O', 'Technical': 'T'},
        'risks': ['Risk 1'],
        'timeline': ['Milestone 1'],
        'key_requirements': ['Requirement 1'],
        'go_nogo': {'score': 75, 'verdict': 'Go', 'summary': 'OK', 'reasons': []},
        'strategic_checklist': {},
        'sections_analyzed': ALL_SECTIONS_DEFAULT.split(','),
    }


class UploadCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'test_analyses.db')
        self.upload_dir = os.path.join(self.tmpdir, 'uploads')
        os.makedirs(self.upload_dir, exist_ok=True)

        patchers = [
            mock.patch.object(app_module, 'DB_PATH', self.db_path),
            mock.patch.object(app_module.app.config, '__getitem__', side_effect=lambda k: self.upload_dir if k == 'UPLOAD_FOLDER' else app_module.app.config[k]),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        # Create schema on the temp DB exactly like init_db does.
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
        self.client = app_module.app.test_client()

    def _post(self, filename='rfp.txt', provider='openrouter', force=False, data=b'RFP content here.\n'):
        form = {
            'rfp_file': (io.BytesIO(data), filename),
            'provider': provider,
            'sections': ALL_SECTIONS_DEFAULT,
            'analysis_title': 'Test Case',
        }
        if force:
            form['force_reanalyze'] = '1'
        return self.client.post('/upload', data=form, content_type='multipart/form-data')

    def test_repeat_upload_served_from_cache_zero_ai_calls(self):
        with mock.patch.object(app_module, 'analyze_rfp_openrouter', return_value=_fake_analysis()) as ai:
            with mock.patch.object(app_module, 'analyze_rfp', return_value=_fake_analysis()):
                r1 = self._post(provider='openrouter')
                self.assertEqual(r1.status_code, 200)
                self.assertIn(b'Fake cached summary', r1.data)
                self.assertNotIn(b'served from cache', r1.data)
                self.assertEqual(ai.call_count, 1)

                # Re-upload identical bytes, same provider → cache hit, no AI call.
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    r2 = self._post(provider='openrouter')
                self.assertEqual(r2.status_code, 200)
                self.assertIn(b'Fake cached summary', r2.data)
                self.assertIn('[CACHE] Analysis served from cache', buf.getvalue())
                self.assertIn('identical document already analyzed as', buf.getvalue())
                self.assertIn('zero AI calls', buf.getvalue())
                self.assertEqual(ai.call_count, 1)  # still only the first real run

    def test_force_reanalyze_bypasses_cache(self):
        with mock.patch.object(app_module, 'analyze_rfp_openrouter', return_value=_fake_analysis()) as ai:
            with mock.patch.object(app_module, 'analyze_rfp', return_value=_fake_analysis()):
                self._post(provider='openrouter')
                self.assertEqual(ai.call_count, 1)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    r2 = self._post(provider='openrouter', force=True)
                self.assertEqual(r2.status_code, 200)
                self.assertNotIn('[CACHE] Analysis served from cache', buf.getvalue())
                self.assertEqual(ai.call_count, 2)

    def test_different_provider_bypasses_cache(self):
        with mock.patch.object(app_module, 'analyze_rfp_openrouter', return_value=_fake_analysis()) as ai_or:
            with mock.patch.object(app_module, 'analyze_rfp', return_value=_fake_analysis()) as ai_g:
                self._post(provider='openrouter')
                self.assertEqual(ai_or.call_count, 1)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    r2 = self._post(provider='gemini')
                self.assertEqual(r2.status_code, 200)
                self.assertNotIn('[CACHE] Analysis served from cache', buf.getvalue())
                self.assertEqual(ai_or.call_count, 1)
                self.assertEqual(ai_g.call_count, 1)

    def test_different_content_bypasses_cache(self):
        with mock.patch.object(app_module, 'analyze_rfp_openrouter', return_value=_fake_analysis()) as ai:
            with mock.patch.object(app_module, 'analyze_rfp', return_value=_fake_analysis()):
                self._post(provider='openrouter', data=b'Version one.\n')
                self._post(provider='openrouter', data=b'Version two, different bytes.\n')
                self.assertEqual(ai.call_count, 2)

    def test_content_hash_is_of_bytes_not_filename(self):
        h1 = app_module.hashlib.sha256(b'X' * 10).hexdigest()
        h2 = app_module.hashlib.sha256(b'X' * 10).hexdigest()
        self.assertEqual(h1, h2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
