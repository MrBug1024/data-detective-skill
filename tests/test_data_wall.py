"""Evidence-bearing DATA walls retain extraction rows and trace candidates."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from data_wall import generate_data_wall
from trace_pipeline import trace_sources
from trace_wall import generate_trace_wall
from evidence_retrieval import explore_evidence
from record_query import query_records
from xlsx_query import query_xlsx


class DataWallEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def render(self, report):
        path = self.root / 'wall.html'
        generate_data_wall(report, path)
        html = path.read_text(encoding='utf-8')
        payload = re.search(r'const D=(.*?);\nconst labels=', html, re.S).group(1)
        return html, json.loads(payload)

    def test_every_extracted_row_has_its_original_values_cells_and_source_snapshot(self):
        from openpyxl import Workbook
        path = self.root / 'source.xlsx'
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['visit_id', 'amount'])
        for index in range(30):
            sheet.append([str(index).zfill(6), index + 0.25])
        workbook.save(path)
        workbook.close()
        report = query_xlsx(path)
        report['analysis_stage'] = 'exact_extraction'
        _, data = self.render(report)
        self.assertEqual(len(data['cards']), 30)  # Beyond the first DOM page.
        self.assertEqual([c['row'] for c in data['cards']], list(range(2, 32)))
        self.assertTrue(all(c['field_count'] == 2 for c in data['cards']))
        self.assertEqual([c['evidence'] for c in data['cards']], report['rows'])
        for card in data['cards']:
            source = data['source_catalog'][card['sources'][0]['source_ref']]
            self.assertEqual(source['file'], str(path))
            self.assertEqual(source['sha256'], report['source_sha256'])
            self.assertEqual(card['evidence']['cells']['visit_id']['address'], 'A' + str(card['row']))
            self.assertEqual(card['outcome'], 'extraction_complete')
        self.assertEqual(data['assessment']['lineage_status'], 'not_proven')

    def test_streamed_jsonl_keeps_physical_lines_and_json_pointers(self):
        path = self.root / 'records.jsonl'
        path.write_text('{"record":{"id":"0001"},"amount":"1.50"}\n\n'
                        '{"record":{"id":"0002"},"amount":"2.50"}\n', encoding='utf-8')
        report = query_records(path)
        report['analysis_stage'] = 'exact_extraction'
        _, data = self.render(report)
        self.assertEqual([card['row'] for card in data['cards']], [1, 3])
        self.assertEqual([card['evidence'] for card in data['cards']], report['records'])
        self.assertTrue(data['cards'][1]['evidence']['field_pointers'])
        self.assertGreater(data['cards'][1]['evidence']['byte_offset'], 0)

    def test_trace_retains_each_source_candidate_and_related_match_evidence(self):
        result = self.root / 'result.csv'
        source = self.root / 'source.csv'
        result.write_text('order_id,amount\nORD-0001,10.25\n', encoding='utf-8')
        source.write_text('order_id,amount\nORD-0001,10.25\nORD-0002,88.25\n', encoding='utf-8')
        report = trace_sources(result, [source], row=2, cache_dir=self.root / 'cache')
        path = self.root / 'mosaic.html'
        generate_trace_wall(report, path)
        html = path.read_text(encoding='utf-8')
        data = json.loads(re.search(r'const REPORT = (.*?);\nlet D=REPORT;', html, re.S).group(1))
        self.assertEqual(data['assessment']['outcome'], 'sources_located')
        self.assertEqual(data['historical_lineage']['status'], 'not_proven')
        view = data['views'][0]
        self.assertEqual(view['row_num'], 2)
        nodes = [node for node in view['chain']['nodes'] if node['node_type'] == 'source']
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['row_num'], 2)
        self.assertEqual(nodes[0]['source_file'], str(source))
        self.assertEqual(nodes[0]['field_values'], ['ORD-0001', '10.25'])
        self.assertEqual(len(view['chain']['edges']), 1)
        self.assertEqual(view['evidence'][0]['source_location']['row'], 2)
        self.assertEqual(view['source_locations'][0]['file'], str(source))
        self.assertEqual({m['result_field'] for m in view['field_mappings']}, {'order_id', 'amount'})

    def test_target_wall_keeps_entity_spans_and_source_text_without_a_result_table(self):
        source = self.root / 'handoff.txt'
        source.write_text('Order ID O-001 references Contract ID C-001.', encoding='utf-8')
        report = explore_evidence('Order ID O-001', [source])
        path = self.root / 'target.html'
        generate_trace_wall(report, path)
        html = path.read_text(encoding='utf-8')
        data = json.loads(re.search(r'const REPORT = (.*?);\nlet D=REPORT;', html, re.S).group(1))
        self.assertEqual(data['assessment']['outcome'], 'evidence_located')
        self.assertTrue(data['views'])
        self.assertEqual({m['raw_value'] for m in data['entity_mentions']}, {'O-001', 'C-001'})
        self.assertEqual(data['rule_evidence'][0]['source_location']['file'], str(source))
        self.assertIn('Order ID O-001', data['rule_evidence'][0]['value'])
        self.assertEqual(data['lookup_seeds'][0]['value'], 'C-001')

    def test_evidence_markup_remains_data_and_empty_extraction_retains_gap(self):
        malicious = '</script><script>throw new Error("must not execute")</script>'
        report = {'analysis_stage': 'exact_extraction', 'file': 'source.csv',
                  'coverage': {'scan_complete': True, 'filter_evaluation_complete': True,
                               'truncated': False, 'matched_rows': 1},
                  'rows': [{'row_num': 7, 'values': {'note': malicious}}]}
        html, data = self.render(report)
        self.assertEqual(data['cards'][0]['evidence']['values']['note'], malicious)
        self.assertNotIn(malicious, html)
        report['rows'] = []
        report['coverage']['matched_rows'] = 0
        _, data = self.render(report)
        self.assertEqual(data['assessment']['outcome'], 'no_matches')
        self.assertEqual(data['cards'][0]['outcome'], 'no_matches')


if __name__ == '__main__':
    unittest.main()
