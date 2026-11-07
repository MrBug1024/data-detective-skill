"""Unified-entry regression: the original A500/B750/C30/D76 lineage remains intact."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from trace_pipeline import trace_sources
from trace_wall import generate_trace_wall, prepare_trace_wall
from run_contract import assess


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='lineage_examples_')
        cls.folder = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('lineage_example_fixture', ROOT / 'examples/generate_examples.py')
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        generator.OUTPUT_DIR = cls.folder
        with contextlib.redirect_stdout(io.StringIO()):
            generator.main()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def trace(self, sources):
        return trace_sources(self.folder / 'R1.xlsx', [self.folder / source for source in sources],
                             row=2, cache_dir=self.folder / 'cache')

    def test_original_four_source_rows_remain_exact(self):
        report = self.trace(['A.xlsx', 'B.xlsx', 'C.xlsx', 'D.xlsx', 'rules.md'])
        matches = report['result_rows'][0]['source_matches']
        selected = {Path(item['file']).name: item['row_num'] for item in matches}
        self.assertEqual(selected, {'A.xlsx': 500, 'B.xlsx': 750, 'C.xlsx': 30, 'D.xlsx': 76})
        self.assertTrue(all(item['status'] == 'located' for item in matches))
        self.assertEqual(report['source_localization']['status'], 'located')
        self.assertIsNone(report['confidence'])
        self.assertEqual(report['historical_lineage']['status'], 'not_proven')
        self.assertGreaterEqual(len(report['chain']['nodes']), 5)
        self.assertGreaterEqual(len(report['chain']['edges']), 4)

    def test_customer_identifier_and_original_cells_are_preserved(self):
        report = self.trace(['A.xlsx', 'B.xlsx'])
        matches = report['result_rows'][0]['source_matches']
        self.assertTrue(any(item['values'].get('客户ID') == 'C000500' for item in matches))
        customer = next(item for item in matches if Path(item['file']).name == 'A.xlsx')
        self.assertEqual(customer['row_num'], 500)
        self.assertEqual(customer['cells']['客户ID']['address'], 'A500')
        self.assertEqual(len(customer['source_sha256']), 64)

    def test_report_and_machine_status_have_separate_evidence_dimensions(self):
        report = self.trace(['A.xlsx'])
        for key in ('result_ref', 'result_rows', 'source_profiles', 'field_mappings', 'coverage',
                    'source_localization', 'calculation_verification', 'historical_lineage', 'chain'):
            self.assertIn(key, report)
        self.assertEqual(report['calculation_verification']['status'], 'not_checked')
        self.assertEqual(report['historical_lineage']['status'], 'not_proven')
        self.assertEqual(assess(report)['outcome'], 'sources_located')
        self.assertTrue(any(mapping['status'] == 'unresolved' for mapping in report['field_mappings']))

    def test_trace_wall_preserves_report_locations_and_values(self):
        report = self.trace(['A.xlsx'])
        path = self.folder / 'wall.html'
        generate_trace_wall(report, path)
        text = path.read_text(encoding='utf-8')
        self.assertIn('数据溯源', text)
        self.assertIn('C000500', text)
        self.assertGreater(len(text), 1000)
        view = prepare_trace_wall(report)['views'][0]
        source = next(n for n in view['chain']['nodes'] if n['node_type'] == 'source')
        self.assertEqual(source['row_num'], 500)
        self.assertEqual(source['source_file'], str(self.folder / 'A.xlsx'))

    def test_missing_requested_source_is_partial_and_actionable(self):
        report = self.trace(['A.xlsx', 'missing.xlsx'])
        self.assertEqual(report['source_localization']['status'], 'partial')
        self.assertTrue(any(i['code'] == 'source_missing' for i in report['issues']))
        self.assertEqual(assess(report)['exit_code'], 2)
        self.assertEqual(report['result_rows'][0]['source_matches'][0]['row_num'], 500)

    def test_unified_cli_writes_matching_terminal_status_and_artifacts(self):
        out_dir = self.folder / 'cli-run'
        completed = subprocess.run([sys.executable, str(ROOT / 'scripts/data_run.py'), 'trace',
            '--result', str(self.folder / 'R1.xlsx'), '--row', '2', '--sources', str(self.folder / 'A.xlsx'),
            '--out-dir', str(out_dir), '--cache-dir', str(self.folder / 'cache')],
            capture_output=True, encoding='utf-8', cwd=ROOT)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads((out_dir / 'status.json').read_text(encoding='utf-8'))
        report = json.loads((out_dir / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['outcome'], 'sources_located')
        self.assertEqual(status['exit_code'], completed.returncode)
        self.assertTrue((out_dir / 'wall.html').is_file())
        self.assertEqual(report['result_rows'][0]['source_matches'][0]['row_num'], 500)
        terminal = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(terminal['event'], 'data_run_finished')
        self.assertEqual(terminal['run_id'], status['run_id'])


if __name__ == '__main__':
    unittest.main()
