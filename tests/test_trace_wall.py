"""The restored mosaic keeps every requested row and exact evidence locations."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from trace_wall import generate_trace_wall, prepare_trace_wall, attach_trace_graph
from lineage_graph import seed_reference


class MosaicTests(unittest.TestCase):
    def test_two_claims_in_one_document_keep_their_own_paths(self):
        report = self.report()
        row = report['result_rows'][0]
        seeds = []
        for value in ('INV-001', 'INV-002'):
            seed = {'evidence_id': 'same-paragraph', 'identifier_namespace': 'invoice',
                    'value': value, 'can_seed_lookup': True, 'bridge_id': value,
                    'source_location': {'file': 'notes.txt', 'paragraph': 1},
                    'provenance': [{'from': 'ORDER-1', 'to': value}]}
            seed['seed_ref'] = seed_reference(seed)
            seeds.append(seed)
        row['document_lookup_seeds'] = seeds
        row['source_matches'][0]['key_provenance'] = [
            {'source_field': 'invoice_id', 'origins': [{'origin': seed['seed_ref'], 'field': 'invoice_id'}]}
            for seed in seeds]
        report = attach_trace_graph(report)
        dependencies = report['lineage_dependencies']
        self.assertTrue(dependencies['references_resolved'])
        self.assertEqual(2, len(dependencies['document_origins']))
        for reference in dependencies['key_dependencies']:
            claim = dependencies['document_origins'][reference['known_source_ref']]
            self.assertEqual(claim['value'], claim['provenance'][-1]['to'])

    def test_target_only_wall_has_a_real_target_and_cited_document(self):
        report = {'analysis_stage': 'evidence_search', 'target': {'description': '订单 O-102'},
                  'coverage': {'retrieval_complete': True, 'truncated': False},
                  'related_evidence': [{'evidence_id': 'e1', 'kind': 'identifier_mention',
                      'source_location': {'file': 'notes.txt', 'line': 3}, 'quote': '订单 O-102 对应装箱 P-903'}]}
        report = attach_trace_graph(report)
        view = prepare_trace_wall(report)['views'][0]
        self.assertEqual('query', view['view_kind'])
        self.assertIsNone(view['row_num'])
        ids = {n['node_id'] for n in report['chain']['nodes']}
        self.assertEqual(2, len(ids))
        self.assertEqual(1, len(report['chain']['edges']))
        self.assertTrue(all(e['from_node'] in ids and e['to_node'] in ids for e in report['chain']['edges']))

    def test_actual_multihop_keys_and_field_dependencies_are_distinct(self):
        report = self.report()
        row = report['result_rows'][0]
        first = row['source_matches'][0]
        row['source_matches'].append({**first, 'source_ref': 'payment', 'file': 'payments.csv',
            'key_provenance': [{'source_field': 'order_ref', 'known_field': 'order_id',
                'origins': [{'origin': first['source_ref'], 'field': 'order_id'}]}]})
        report = attach_trace_graph(report)
        deps = report['lineage_dependencies']
        self.assertTrue(deps['references_resolved'])
        self.assertEqual('INDIRECT', deps['key_dependencies'][0]['dependency_type'])
        self.assertTrue(all(d['dependency_type'] == 'DIRECT' for d in deps['field_dependencies']))
        self.assertTrue(any(e.get('relationship') == 'reconstructed_key_join' for e in report['chain']['edges']))

    def report(self):
        return {'analysis_stage': 'source_trace', 'result_ref': {'file': 'result.xlsx', 'sheet': 'Results'},
                'coverage': {'requested_physical_rows': [2, 3], 'ingestion_complete': True},
                'source_localization': {'status': 'located'},
                'result_rows': [{'row_num': r, 'values': {'id': str(r), 'amount': 0},
                    'source_matches': [{'source_ref': 'src'+str(r), 'file': 'source.xlsx', 'sheet': 'Data',
                        'row_num': r+10, 'status': 'located', 'match_keys': ['id'],
                        'values': {'id': str(r), 'amount': 0}, 'cells': {'amount': {'address': 'B'+str(r+10)}}}]} for r in (2, 3)],
                'field_mappings': [{'result_row': r, 'result_field': 'amount', 'status': 'exact',
                                    'sources': [{'source_ref': 'src'+str(r), 'address': 'B'+str(r+10)}]} for r in (2, 3)],
                'rule_evidence': [{'kind': 'rule_candidate', 'source_location': {'file': 'rules.xlsx', 'sheet': 'Rules', 'row': 260},
                                   'quote': '</script><script>throw new Error("data")</script>'}]}

    def test_rows_keep_separate_source_graphs_and_all_mappings(self):
        from jsonschema import validate
        validate(attach_trace_graph(self.report())['chain'],
                 json.loads((ROOT/'schemas/chain.schema.json').read_text(encoding='utf-8')))
        data = prepare_trace_wall(self.report())
        self.assertEqual('sources_located', data['assessment']['outcome'])
        self.assertEqual([2, 3], [v['row_num'] for v in data['views']])
        for view in data['views']:
            self.assertEqual(2, len(view['chain']['nodes']))
            self.assertEqual(1, len(view['chain']['edges']))
            self.assertEqual(view['row_num'] + 10, view['source_locations'][0]['row'])
            self.assertEqual([view['row_num']], [m['result_row'] for m in view['field_mappings']])

    def test_offline_mosaic_uses_original_canvas_and_escapes_source_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'wall.html'
            generate_trace_wall(self.report(), path)
            html = path.read_text(encoding='utf-8')
            self.assertIn('function initCanvas()', html)
            self.assertIn('id="trace-row"', html)
            self.assertIn('id="pane-mapping"', html)
            self.assertNotIn('<script>throw new Error', html)
            self.assertIn('\\u003c/script', html)
            payload = re.search(r'const REPORT = (.*?);\nlet D=REPORT;', html, re.S).group(1)
            data = json.loads(payload)
            self.assertEqual(260, data['rule_evidence'][0]['source_location']['row'])
            self.assertEqual(0, data['views'][0]['chain']['nodes'][1]['field_values'][1])
            self.assertNotIn('src="http', html)

    def test_document_identifier_does_not_link_to_unmentioned_result_rows(self):
        report = self.report()
        report['related_evidence'] = [{'evidence_id': 'doc1', 'kind': 'identifier_mention',
            'source_location': {'file': 'record.txt', 'paragraph': 1}, 'value': 'Record 2 only',
            'matched_terms': [{'text': '2', 'identifier': True,
                               'origins': [{'kind': 'value', 'field': 'id', 'result_row': 2}]}]}]
        report = attach_trace_graph(report)
        document = next(n for n in report['chain']['nodes'] if n.get('source_file') == 'record.txt')
        linked_ids = [e['to_node'] for e in report['chain']['edges'] if e['from_node'] == document['node_id']]
        results = [n['row_num'] for n in report['chain']['nodes'] if n['node_id'] in linked_ids]
        self.assertEqual([2], results)


if __name__ == '__main__':
    unittest.main()
