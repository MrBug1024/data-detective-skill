"""Observable source-localization behavior, including misleading identifiers."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from trace_pipeline import trace_sources


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv(self, name, fields, rows):
        file = self.root / name
        with file.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            writer.writerows(rows)
        return file

    def trace(self, result, sources, **kwargs):
        return trace_sources(result, sources, cache_dir=self.root / 'cache', **kwargs)

    def test_batch_multihop_and_renamed_value_vectors(self):
        result = self.csv('result.csv', ['order_id', 'customer_label', 'total'],
                          [['O-100', 'Alice Evans', '123.40'], ['O-101', 'Bob Dennis', '51.20']])
        orders = self.csv('orders.csv', ['order_id', 'customer_id', 'gross_amount'],
                          [['O-101', 'C-020', '51.20'], ['O-100', 'C-019', '123.40'], ['O-999', 'C-999', '0']])
        customers = self.csv('customers.csv', ['customer_id', 'display_name'],
                             [['C-019', 'Alice Evans'], ['C-020', 'Bob Dennis']])
        p = self.trace(result, [customers, orders], rows='all')
        self.assertEqual(p['source_localization']['status'], 'located')
        self.assertEqual(p['status'], 'source_chain_reconstructed')
        self.assertEqual([len(r['source_matches']) for r in p['result_rows']], [2, 2])
        self.assertEqual(len(p['field_mappings']), 6)
        renamed = [m for m in p['field_mappings'] if m['result_field'] in ('total', 'customer_label')]
        self.assertTrue(all(m['status'] == 'mapped' for m in renamed))
        self.assertEqual(p['calculation_verification']['status'], 'not_checked')
        self.assertEqual(p['historical_lineage']['status'], 'not_proven')

    def test_duplicate_id_requires_evidence_not_first_row(self):
        result = self.csv('result.csv', ['order_id', 'description', 'amount'], [['O-100', 'second', '17.25']])
        source = self.csv('source.csv', ['order_id', 'description', 'amount'],
                          [['O-100', 'first', '22.80'], ['O-100', 'second', '17.25']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'located')
        self.assertEqual(p['result_rows'][0]['source_matches'][0]['row_num'], 3)
        same = self.csv('same.csv', ['order_id', 'description', 'amount'],
                        [['O-100', 'second', '17.25'], ['O-100', 'second', '17.25']])
        p = self.trace(result, [same])
        self.assertEqual(p['source_localization']['status'], 'ambiguous')
        self.assertEqual(len(p['result_rows'][0]['source_matches']), 2)

    def test_conflicting_stable_identifier_blocks_amount_match(self):
        result = self.csv('result.csv', ['order_id', 'person_id', 'amount'], [['O-100', 'P-001', '100']])
        source = self.csv('source.csv', ['order_id', 'person_id', 'amount'], [['O-100', 'P-002', '100']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'not_found')
        self.assertEqual(p['result_rows'][0]['source_matches'], [])
        self.assertEqual(p['source_localization']['unmatched_sources'][0]['reason'], 'identifier_conflict')

    def test_record_identifier_localizes_despite_display_precision(self):
        result = self.csv('result.csv', ['order_id', 'record_id', 'rate'], [['O-100', 'ROW-001', '0.56']])
        source = self.csv('source.csv', ['order_id', 'record_id', 'rate'],
                          [['O-100', 'ROW-002', '0.56'], ['O-100', 'ROW-001', '0.5600000000000001']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'located')
        self.assertEqual(p['result_rows'][0]['source_matches'][0]['row_num'], 3)
        field = next(m for m in p['field_mappings'] if m['result_field'] == 'rate')
        self.assertEqual(field['status'], 'representation_discrepancy')

    def test_missing_key_on_one_requested_row_is_not_complete_chain(self):
        result = self.csv('result.csv', ['order_id', 'customer_id', 'amount'],
                          [['O-100', 'C-001', '11'], ['O-101', '', '22']])
        orders = self.csv('orders.csv', ['order_id', 'amount'], [['O-100', '11'], ['O-101', '22']])
        customers = self.csv('customers.csv', ['customer_id', 'name'], [['C-001', 'Alice Evans']])
        p = self.trace(result, [orders, customers], rows='all')
        self.assertEqual(p['source_localization']['status'], 'partial')
        self.assertFalse(p['coverage']['ingestion_complete'])
        self.assertTrue(any(u['reason'] == 'requested_row_missing_source_key'
                            for u in p['source_localization']['unmatched_sources']))

    def test_nonidentifying_common_values_do_not_create_a_join(self):
        result = self.csv('result.csv', ['name', 'sex', 'flag'], [['Alice', 'female', '1']])
        source = self.csv('source.csv', ['name', 'sex', 'flag'], [['Alice', 'female', '1']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'not_found')
        self.assertFalse(p['result_rows'][0]['source_matches'])
        self.assertTrue(any(i['reason'] == 'no_shared_stable_key' for i in p['source_localization']['unmatched_sources']))

    def test_zero_one_identifiers_need_explicit_authorized_key(self):
        result = self.csv('result.csv', ['id', 'name'], [['1', 'Alice Evans']])
        source = self.csv('source.csv', ['id', 'name'], [['1', 'Alice Evans']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'not_found')
        p = self.trace(result, [source], config={'sources': {'source.csv': {'keys': ['id']}}})
        self.assertEqual(p['source_localization']['status'], 'located')

    def test_tenant_key_is_complete_tuple(self):
        result = self.csv('result.csv', ['tenant_id', 'record_id', 'amount'],
                          [['A-01', 'R-01', '11'], ['B-02', 'R-02', '22']])
        source = self.csv('source.csv', ['tenant_id', 'record_id', 'amount'],
                          [['A-01', 'R-02', '900'], ['A-01', 'R-01', '11'],
                           ['B-02', 'R-01', '900'], ['B-02', 'R-02', '22']])
        p = self.trace(result, [source], rows='all',
                       config={'sources': {'source.csv': {'keys': ['tenant_id', 'record_id']}}})
        self.assertEqual(p['source_localization']['status'], 'located')
        self.assertEqual(p['coverage']['source_queries'][0]['coverage']['matched_rows'], 2)
        self.assertEqual([r['source_matches'][0]['row_num'] for r in p['result_rows']], [3, 5])

    def test_order_reference_and_sku_form_a_transaction_line_key(self):
        result = self.csv('result.csv', ['order_ref', 'sku', 'line_value'], [['ORDER-77', 'SKU-B', '96']])
        source = self.csv('source.csv', ['order_ref', 'sku', 'units', 'line_value'],
                          [['ORDER-77', 'SKU-A', '2', '96'], ['ORDER-77', 'SKU-B', '3', '96'],
                           ['ORDER-88', 'SKU-B', '3', '96']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'located')
        match = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(match['row_num'], 3)
        self.assertEqual(set(match['match_keys']), {'order_ref', 'sku'})
        self.assertEqual(p['coverage']['source_queries'][0]['coverage']['matched_rows'], 1)

    def test_sku_alone_does_not_claim_transaction_identity(self):
        result = self.csv('result.csv', ['sku', 'amount'], [['SKU-A', '96']])
        source = self.csv('source.csv', ['sku', 'amount'], [['SKU-A', '96']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'not_found')

    def test_explicit_renamed_key_and_missing_source_are_visible(self):
        result = self.csv('result.csv', ['order_id', 'amount'], [['O-100', '9.50']])
        source = self.csv('source.csv', ['external_reference', 'amount'], [['O-100', '9.50']])
        p = self.trace(result, [source, self.root / 'missing.csv'],
                       config={'sources': {'source.csv': {'key_map': {'external_reference': 'order_id'}}}})
        self.assertEqual(p['result_rows'][0]['source_localization']['status'], 'located')
        self.assertEqual(p['source_localization']['status'], 'partial')
        self.assertTrue(any(i['code'] == 'source_missing' for i in p['issues']))

    def test_alias_cache_is_isolated_between_runs(self):
        result = self.csv('result.csv', ['order_id', 'customer_id', 'amount'], [['O-100', 'C-001', '9.50']])
        source = self.csv('source.csv', ['foreign_ref', 'amount'], [['O-100', '9.50']])
        first = self.trace(result, [source], config={'field_aliases': {'order_id': ['foreign_ref']}})
        self.assertEqual(first['source_localization']['status'], 'located')
        second = self.trace(result, [source], config={'field_aliases': {'customer_id': ['foreign_ref']}})
        self.assertEqual(second['source_localization']['status'], 'not_found')

    def test_jsonl_nested_locations_and_leading_zero_ids(self):
        result = self.csv('result.csv', ['customer_id', 'amount'], [['0012', '19.20']])
        source = self.root / 'source.jsonl'
        source.write_text('{"customer_id":"12","amount":19.20}\n'
                          '{"customer_id":"0012","amount":19.20,"meta":{"note":"ok"}}\n', encoding='utf-8')
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'located')
        match = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(match['row_num'], 2)
        self.assertEqual(match['field_pointers']['/meta/note'], '/meta/note')

    def test_json_object_is_structured_and_has_exact_pointers(self):
        result = self.csv('result.csv', ['case_id', 'amount'], [['CASE-0001', '19.20']])
        source = self.root / 'source.json'
        source.write_text('{"case_id":"CASE-0001","amount":19.20,"meta":{"note":"original"}}', encoding='utf-8')
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'located')
        match = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(match['json_pointer'], '')
        self.assertEqual(match['row_num_kind'], 'json_record_index')
        self.assertEqual(match['field_pointers']['/meta/note'], '/meta/note')
        self.assertTrue(p['chain']['nodes'])

    def test_json_array_and_explicit_field_map_disambiguate_duplicate_ids(self):
        result = self.csv('result.csv', ['case_id', 'description'], [['CASE-0001', 'Correct detail']])
        source = self.root / 'source.json'
        source.write_text(json.dumps([
            {'case_id': 'CASE-0001', 'details': {'note': 'Another detail'}},
            {'case_id': 'CASE-0001', 'details': {'note': 'Correct detail'}},
        ]), encoding='utf-8')
        p = self.trace(result, [source], config={'sources': {'source.json': {'field_map': {'description': '/details/note'}}}})
        self.assertEqual(p['source_localization']['status'], 'located')
        match = p['result_rows'][0]['source_matches'][0]
        self.assertEqual(match['json_pointer'], '/1')
        self.assertEqual(match['field_pointers']['/details/note'], '/1/details/note')

    def test_rule_id_column_does_not_hide_structural_order_join(self):
        result = self.csv('result.csv', ['order_id', 'amount'], [['ORDER-123', '17.25']])
        source = self.csv('source.csv', ['order_id', 'amount', 'rule_id'], [['ORDER-123', '17.25', 'RULE-9']])
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'located')
        self.assertEqual(p['result_rows'][0]['source_matches'][0]['row_num'], 2)

    def test_document_identifier_is_reported_without_fabricated_record_join(self):
        result = self.csv('result.csv', ['case_id', 'amount'], [['CASE-0001', '17.25'], ['CASE-0002', '18.50']])
        source = self.root / 'source.txt'
        source.write_text('Original record CASE-0001: amount 17.25.\nOriginal record CASE-0002: amount 18.50.', encoding='utf-8')
        p = self.trace(result, [source], rows='all')
        self.assertEqual(p['source_localization']['status'], 'evidence_only')
        self.assertEqual(p['document_localization']['status'], 'passages_found')
        for result_row in p['result_rows']:
            self.assertFalse(result_row['source_matches'])
            self.assertTrue(result_row['document_evidence'])
            self.assertFalse(result_row['document_evidence'][0]['record_join_proven'])
            self.assertTrue(all(origin['result_row'] == result_row['row_num']
                                for doc in result_row['document_evidence']
                                for term in doc['matched_identifiers'] for origin in term['origins']))

    def test_rule_text_without_identifier_is_related_evidence_not_row_identity(self):
        result = self.csv('result.csv', ['case_id', 'service'], [['CASE-0001', 'Long specific procedure name']])
        source = self.root / 'policy.txt'
        source.write_text('Rule: Long specific procedure name requires a supporting document.', encoding='utf-8')
        p = self.trace(result, [source])
        self.assertEqual(p['source_localization']['status'], 'not_found')
        self.assertFalse(p['result_rows'][0]['document_evidence'])
        self.assertTrue(p['related_evidence'])

    def test_xlsx_title_header_and_auto_first_data_row(self):
        import openpyxl
        def book(name, headers, values):
            path = self.root / name
            w = openpyxl.Workbook()
            s = w.active
            s.append(['Attachment'])
            s.append(['Exported records for review'])
            s.append(headers)
            s.append(values)
            w.save(path)
            return path
        result = book('result.xlsx', ['order_id', 'amount'], ['O-100', 13.25])
        source = book('source.xlsx', ['order_id', 'amount'], ['O-100', 13.25])
        p = self.trace(result, [source], row=None,
                       config={'result': {'header_row': 3}, 'sources': {'source.xlsx': {'header_row': 3}}})
        self.assertEqual(p['result_rows'][0]['row_num'], 4)
        self.assertEqual(p['result_rows'][0]['source_matches'][0]['cells']['amount']['address'], 'B4')
        self.assertEqual(p['source_localization']['status'], 'located')


if __name__ == '__main__':
    unittest.main()
