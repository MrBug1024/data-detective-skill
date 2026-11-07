"""Public configuration and bounded report consumption across the lineage flow."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from lineage_config import validate_config
from read_evidence import report_slice


class LineageInterfaceTests(unittest.TestCase):
    def test_scope_specific_keys_are_accepted_and_typos_are_rejected(self):
        config = {'sources': {'all.xlsx': {'sheets': {'Orders': {'header_row': 3,
            'key_map': {'foreign_ref': {'field': 'order_id', 'file': 'orders.csv', 'sheet': None}}}}}},
            'evidence': {'max_units': 1000}}
        self.assertEqual(config, validate_config(config))
        for value in ({'max_source_key_pass': 4}, {'sources': {'x.csv': {'key_maps': {}}}},
                      {'max_source_key_passes': 0}, {'result': {'skip': True}}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Invalid lineage config'):
                validate_config(value)

    def test_summary_stays_small_and_field_page_selects_exact_row(self):
        report = {'analysis_stage': 'source_trace', 'result_rows': [],
                  'coverage': {'source_queries': [{'large': 'x' * 1000000}], 'field_count': 2},
                  'field_mappings': [{'result_row': r, 'result_field': 'amount'} for r in (2, 3)]}
        summary = report_slice(report, section='summary')
        self.assertLess(len(json.dumps(summary)), 3000)
        page = report_slice(report, section='fields', row=3, field='amount')
        self.assertEqual(1, page['matching_total'])
        self.assertEqual(3, page['items'][0]['result_row'])

    def test_target_cli_delivers_cited_wall_and_configuration_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'notes.txt'
            source.write_text('订单号 ORD-0042；合同号 CT-0098。已关联交付记录。', encoding='utf-8')
            args = [sys.executable, str(ROOT/'scripts/data_run.py'), 'trace', '--target', '订单号 ORD-0042',
                    '--sources', str(source), '--out-dir', str(root/'good')]
            process = subprocess.run(args, capture_output=True, text=True, encoding='utf-8')
            status = json.loads(process.stdout)
            self.assertEqual(0, process.returncode, process.stderr + process.stdout)
            self.assertEqual('evidence_located', status['outcome'])
            report = json.loads((root/'good/report.json').read_text(encoding='utf-8'))
            self.assertTrue(report['chain']['edges'])
            self.assertTrue(report['entity_mentions'])
            config = root/'bad.json'
            config.write_text('{"max_source_key_pass": 5}', encoding='utf-8')
            args[-1] = str(root/'bad')
            process = subprocess.run(args + ['--config', str(config)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(1, process.returncode)
            status = json.loads(process.stdout)
            self.assertEqual('failed', status['state'])
            self.assertIn('Invalid lineage config', status['error']['message'])


if __name__ == '__main__':
    unittest.main()
