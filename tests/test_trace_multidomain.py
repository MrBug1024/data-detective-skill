"""Cross-domain raw scenarios, source-order changes and declared record grains."""
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'test' / 'lineage'))
from trace_pipeline import trace_sources
from build_scenarios import build_cases, evaluate


class MultiDomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = {c['name']: c for c in build_cases(self.root / 'raw')}

    def trace(self, case, sources=None, config=None):
        return trace_sources(case['result'], sources or case['sources'], rows=case['rows'],
                             config=case['config'] if config is None else config,
                             cache_dir=self.root / 'cache' / case['name'])

    def test_independent_business_scenarios(self):
        for name, case in self.cases.items():
            with self.subTest(name=name, domain=case['domain']):
                checked = evaluate(case, self.trace(case))
                self.assertTrue(checked['passed'], checked['failures'])

    def test_late_conflict_never_yields_confirmed_downstream_under_reordering(self):
        case = self.cases['logistics_late_bridge_conflict']
        for order in itertools.permutations(case['sources']):
            with self.subTest(order=[p.name for p in order]):
                p = self.trace(case, list(order))
                actual = {Path(m['file']).name for r in p['result_rows'] for m in r['source_matches'] if m['status'] == 'located'}
                self.assertEqual(actual, {'manifest.csv', 'amended_manifest.csv'})
                self.assertNotEqual(p['source_localization']['status'], 'located')
                self.assertTrue(any(i['code'] == 'conflicting_propagated_identifier' for i in p['issues']))

    def test_explicit_origin_scope_survives_unrelated_same_column_names(self):
        case = self.cases['manufacturing_explicit_origin_namespace']
        for order in itertools.permutations(case['sources']):
            with self.subTest(order=[p.name for p in order]):
                p = self.trace(case, list(order))
                self.assertTrue(evaluate(case, p)['passed'])
                target = next(m for m in p['result_rows'][0]['source_matches'] if Path(m['file']).name == 'specifications.csv')
                self.assertEqual(target['key_provenance'][0]['known_reference']['file'], 'orders.csv')

    def test_multiple_blocks_are_bounded_and_coverage_is_explicit(self):
        case = self.cases['laboratory_complementary_missing_keys']
        report = self.trace(case, config={'max_source_key_passes': 1})
        self.assertEqual(len(report['coverage']['source_queries']), 1)
        self.assertFalse(report['coverage']['ingestion_complete'])
        self.assertTrue(any(i['code'] == 'key_pass_budget_exhausted' for i in report['issues']))
        report = self.trace(case)
        self.assertEqual(len(report['coverage']['source_queries']), 2)
        self.assertEqual(report['coverage']['source_queries'][1]['target_result_rows'], [3])
        self.assertEqual(report['source_localization']['status'], 'located')

    def test_later_keys_refine_candidates_without_extra_source_scan(self):
        case = self.cases['commerce_late_key_refines_ambiguous_lines']
        for order in itertools.permutations(case['sources']):
            with self.subTest(order=[p.name for p in order]):
                report = self.trace(case, list(order))
                self.assertTrue(evaluate(case, report)['passed'])
                self.assertEqual(len(report['coverage']['source_queries']), 2)
                for row in report['result_rows']:
                    line = next(m for m in row['source_matches'] if Path(m['file']).name == 'lines.csv')
                    self.assertEqual(set(line['match_keys']), {'order_ref', 'sku'})
                    self.assertTrue(any(p['source_field'] == 'sku' and any(o['origin'] != 'result' for o in p['origins'])
                                        for p in line['key_provenance']))
                if order[0].name == 'lines.csv':
                    self.assertEqual(len(report['result_rows'][0]['source_candidate_history'][0]['candidates']), 2)

    def test_static_sql_is_not_executed_and_dynamic_sql_is_not_complete(self):
        case = self.cases['finance_static_sql_tuple_locations']
        p = self.trace(case)
        match = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(match['tuple_num'], 2)
        self.assertIsInstance(match['char_offset'], int)
        self.assertIsInstance(match['statement_span'], list)
        dynamic = case['folder'] / 'dynamic.sql'
        dynamic.write_text("INSERT INTO entries SELECT * FROM live_entries;", encoding='utf-8')
        p = self.trace(case, [dynamic])
        self.assertFalse(p['coverage']['ingestion_complete'])
        self.assertFalse(any(m['status'] == 'located' for r in p['result_rows'] for m in r['source_matches']))

    def test_static_sql_projection_preserves_tuple_location(self):
        case = self.cases['finance_static_sql_tuple_locations']
        p = self.trace(case)
        match = p['result_rows'][0]['source_matches'][0]
        fields = list(match['values'])[:2]
        p = self.trace(case, config={'sources': {case['sources'][0].name: {'columns': fields}}})
        projected = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(set(projected['values']), set(fields))
        self.assertEqual(projected['tuple_num'], 2)
        self.assertEqual(projected['source_ref'], match['source_ref'])

    def test_prepared_snapshot_is_used_and_missing_snapshot_never_falls_back(self):
        import openpyxl
        from xlsx_snapshot import prepare_snapshot
        case = self.cases['finance_missing_is_not_agreement']
        source = case['folder'] / 'ledger.xlsx'
        workbook = openpyxl.Workbook()
        workbook.active.append(['transaction_id', 'memo'])
        workbook.active.append(['TX-770', None])
        workbook.save(source)
        index = self.root / 'snapshot'
        config = {'sources': {source.name: {'snapshot_dir': str(index), 'header_row': 1}}}
        before = self.trace(case, [source], config=config)
        self.assertFalse(before['coverage']['ingestion_complete'])
        self.assertEqual(before['coverage']['source_queries'], [])
        self.assertTrue(any(i.get('error_type') == 'SnapshotUnavailable' for i in before['issues']))
        prepare_snapshot(source, index_dir=index, key_columns=['transaction_id'])
        after = self.trace(case, [source], config=config)
        self.assertEqual(after['source_localization']['status'], 'located')
        self.assertEqual(after['coverage']['source_queries'][0]['cache']['kind'], 'source_snapshot')

    def test_legacy_xls_dependency_failure_is_visible(self):
        case = self.cases['finance_missing_is_not_agreement']
        file = case['folder'] / 'legacy.xls'
        file.write_bytes(b'legacy-reader-test')
        with mock.patch('parsers.FileParser.parse', side_effect=RuntimeError('Missing optional xlrd dependency')):
            p = self.trace(case, [file])
        self.assertEqual(p['coverage']['source_files_unavailable'], 1)
        self.assertFalse(p['coverage']['ingestion_complete'])
        self.assertTrue(any('xlrd' in i.get('message', '') for i in p['issues']))

    def test_document_bridge_is_an_actual_typed_query_and_negation_does_not_seed(self):
        case = self.cases['finance_missing_is_not_agreement']
        result = case['folder'] / 'bridge_result.csv'
        result.write_text('order_id\nORD-731\n', encoding='utf-8')
        document = case['folder'] / 'bridge.txt'
        document.write_text('Order ORD-731 references invoice INV-441. Order ORD-731 does not reference invoice INV-442.', encoding='utf-8')
        invoice = case['folder'] / 'invoices.csv'
        invoice.write_text('invoice_id,amount\nINV-441,70.20\nINV-442,70.20\n', encoding='utf-8')
        p = trace_sources(result, [invoice, document], rows='all', cache_dir=self.root / 'bridge-cache')
        self.assertEqual(p['source_localization']['status'], 'located')
        match = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(match['row_num'], 2)
        self.assertEqual(match['key_provenance'][0]['known_identifier_namespace'], 'invoice')
        self.assertTrue(match['key_provenance'][0]['origins'][0]['origin'].startswith('seed_'))
        self.assertTrue(p['coverage']['source_queries'][0]['coverage']['scan_complete'])

    def test_document_bridge_cannot_drop_source_scope(self):
        case = self.cases['finance_missing_is_not_agreement']
        result = case['folder'] / 'bridge_result.csv'
        result.write_text('publication_id\nPUB-601\n', encoding='utf-8')
        document = case['folder'] / 'bridge.txt'
        document.write_text('Publication PUB-601 uses sample SAM-004.', encoding='utf-8')
        source = case['folder'] / 'samples.csv'
        source.write_text('sample_id,lab_id,reading\nSAM-004,LAB-A,3.1\n', encoding='utf-8')
        p = trace_sources(result, [source, document], rows='all', cache_dir=self.root / 'bridge-cache')
        self.assertFalse(any(m['status'] == 'located' for r in p['result_rows'] for m in r['source_matches']))

    def test_late_conflict_propagates_across_a_document_bridge(self):
        case = self.cases['finance_missing_is_not_agreement']
        result = case['folder'] / 'bridge_result.csv'
        result.write_text('order_id\nORD-900\n', encoding='utf-8')
        link = case['folder'] / 'orders.csv'
        link.write_text('order_id,payment_id\nORD-900,PAY-A\n', encoding='utf-8')
        document = case['folder'] / 'bridge.txt'
        document.write_text('Payment ID PAY-A references invoice INV-8.', encoding='utf-8')
        conflicting = case['folder'] / 'invoice.csv'
        conflicting.write_text('invoice_id,payment_id\nINV-8,PAY-B\n', encoding='utf-8')
        p = trace_sources(result, [link, conflicting, document], rows='all', cache_dir=self.root / 'bridge-cache')
        selected = [Path(m['file']).name for r in p['result_rows'] for m in r['source_matches'] if m['status'] == 'located']
        self.assertEqual(selected, ['orders.csv'])
        self.assertEqual(p['source_localization']['status'], 'ambiguous')

    def test_later_withdrawal_revokes_already_queried_document_dependency(self):
        from evidence_retrieval import retrieve_evidence
        case = self.cases['finance_missing_is_not_agreement']
        result = case['folder'] / 'bridge_result.csv'
        result.write_text('order_id\nORD-811\n', encoding='utf-8')
        document = case['folder'] / 'bridge.txt'
        document.write_text('Order ORD-811 references invoice INV-811.', encoding='utf-8')
        invoice = case['folder'] / 'invoice.csv'
        invoice.write_text('invoice_id,amount\nINV-811,4.50\n', encoding='utf-8')
        pending = case['folder'] / 'unrelated.csv'
        pending.write_text('shipment_id\nSHIP-999\n', encoding='utf-8')
        calls = []
        def updated_evidence(*args, **kwargs):
            if kwargs.get('_expand') is False:
                return retrieve_evidence(*args, **kwargs)
            calls.append(1)
            packet = retrieve_evidence(*args, **kwargs)
            if len(calls) >= 2:
                for item in packet['evidence']:
                    for bridge in item.get('bridge_candidates', []):
                        bridge['can_seed_lookup'] = False
                        bridge['reason'] = 'explicit_same_source_previous_unit_retraction'
            return packet
        with mock.patch('evidence_retrieval.retrieve_evidence', side_effect=updated_evidence):
            p = trace_sources(result, [invoice, pending, document], rows='all', cache_dir=self.root / 'withdraw-cache')
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any(q['coverage']['matched_rows'] for q in p['coverage']['source_queries']))
        self.assertFalse(any(m['status'] == 'located' for r in p['result_rows'] for m in r['source_matches']))
        self.assertTrue(any(i['code'] == 'document_bridge_invalidated' for i in p['issues']))

    def test_note_bridge_uses_only_independently_located_record_without_rescan(self):
        case = self.cases['finance_missing_is_not_agreement']
        result = case['folder'] / 'note_result.csv'
        result.write_text('order_id\nORD-404\n', encoding='utf-8')
        orders = case['folder'] / 'orders.csv'
        orders.write_text('order_id,note\nORD-404,Order ORD-404 references invoice INV-91.\n', encoding='utf-8')
        invoices = case['folder'] / 'invoices.csv'
        invoices.write_text('invoice_id,amount\nINV-91,23.50\nINV-92,23.50\n', encoding='utf-8')
        p = trace_sources(result, [orders, invoices], rows='all', cache_dir=self.root / 'note-cache')
        self.assertEqual(p['source_localization']['status'], 'located')
        self.assertEqual({(Path(m['file']).name, m['row_num']) for m in p['result_rows'][0]['source_matches']},
                         {('orders.csv', 2), ('invoices.csv', 2)})
        self.assertEqual(len(p['coverage']['source_queries']), 2)
        record_evidence = [e for e in p['related_evidence'] if e.get('kind') == 'located_record']
        self.assertEqual(len(record_evidence), 1)
        self.assertFalse(record_evidence[0]['coverage']['source_scan_claim'])


if __name__ == '__main__':
    unittest.main()
