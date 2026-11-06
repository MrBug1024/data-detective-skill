"""Generic streamed-workbook reconciliation and componentwise join contracts."""
import contextlib
import copy
from datetime import datetime, time
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

import jsonschema
from openpyxl import Workbook
from openpyxl.utils.datetime import MAC_EPOCH

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from reconcile import PlanError, validate_plan_shape, verify_plan, verify_plan_batch
from parsers import FileParser
import reconcile
import xlsx_query


class ReconcilePlanSchemas(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.schema = json.loads((ROOT / 'schemas/lineage-plan.schema.json').read_text(encoding='utf-8'))

    def workbook(self, name, headers, rows, header_row=1):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Records'
        if header_row > 1:
            sheet.cell(1, 1, 'Manufacturing export: source records')
        for column, value in enumerate(headers, 1):
            sheet.cell(header_row, column, value)
        for number, row in enumerate(rows, header_row + 1):
            for column, value in enumerate(row, 1):
                sheet.cell(number, column, value)
        book.save(self.base / name)
        book.close()

    def plan(self):
        self.workbook('results.xlsx', ['order', 'total', 'lines'],
                      [['0001', '9.70', 2], ['0002', '9.10', 1]], header_row=2)
        self.workbook('work.xlsx', ['order', 'part', 'plant', 'quantity'],
                      [['0001', 'ab', 'c', 2], ['0001', 'a', 'bc', 3],
                       ['0002', 'a|b', 'c', 1], ['0003', 'a', 'b|c', 999]], header_row=3)
        self.workbook('costs.xlsx', ['part', 'plant', 'unit_cost'],
                      [['ab', 'c', '1.25'], ['a', 'bc', '2.40'],
                       ['a|b', 'c', '9.10'], ['a', 'b|c', '0.01']], header_row=2)
        self.workbook('rules.xlsx', ['code', 'rule'],
                      [['R1', 'Join part and plant exactly; sum quantity times unit cost.']], header_row=3)
        return {
            'version': 1,
            'result': {'file': 'results.xlsx', 'sheet': 'Records', 'header_row': 2, 'row': 3,
                       'query': {'where': {'order': ['0001', '0002']}}},
            'sources': {
                'work': {'file': 'work.xlsx', 'sheet': 'Records', 'header_row': 3,
                         'query': {'where': {'order': ['0001', '0002']},
                                   'columns': ['order', 'part', 'plant', 'quantity']}},
                'cost': {'file': 'costs.xlsx', 'sheet': 'Records', 'header_row': 2,
                         'query': {'key_rows': [{'part': 'ab', 'plant': 'c'},
                                                {'part': 'a', 'plant': 'bc'},
                                                {'part': 'a|b', 'plant': 'c'}],
                                   'columns': ['part', 'plant', 'unit_cost']}},
            },
            'select': {'source': 'work', 'where': [{'field': 'order', 'result_field': 'order'}]},
            'joins': [{'source': 'cost', 'keys': [
                {'left': {'source': 'work', 'field': 'part'}, 'right_field': 'part'},
                {'left': {'source': 'work', 'field': 'plant'}, 'right_field': 'plant'}],
                'cardinality': 'many_to_one'}],
            'outputs': [{'result_field': 'total', 'aggregate': 'sum', 'expression': {'mul': [
                {'source': 'work', 'field': 'quantity'}, {'source': 'cost', 'field': 'unit_cost'}]},
                'quantize': '0.01', 'rounding': 'ROUND_HALF_UP'},
                {'result_field': 'lines', 'aggregate': 'count'}],
            'rule_evidence': [{'file': 'rules.xlsx', 'sheet': 'Records', 'header_row': 3,
                               'quote': 'Join part and plant exactly'}],
        }

    def verify(self, plan):
        with contextlib.redirect_stdout(io.StringIO()):
            return verify_plan(plan, self.base)

    def test_streamed_plan_and_schema_preserve_original_locations(self):
        plan = self.plan()
        jsonschema.Draft7Validator.check_schema(self.schema)
        jsonschema.validate(plan, self.schema)
        report = self.verify(plan)
        self.assertEqual('reconciled_not_proven_lineage', report['status'])
        self.assertEqual(['9.70', '2'], [c['calculated'] for c in report['checks']])
        self.assertEqual(3, report['result']['row'])
        self.assertEqual('B3', report['result']['metadata']['cells']['total']['address'])
        self.assertEqual(str(self.base / 'results.xlsx'), report['result']['file'])
        work_rows = [r for r in report['source_rows'] if r['source'] == 'work']
        self.assertEqual({4, 5}, {r['location']['row'] for r in work_rows})
        self.assertTrue(all(r['location']['file'] == str(self.base / 'work.xlsx') for r in work_rows))
        costs = [r for r in report['source_rows'] if r['source'] == 'cost']
        self.assertEqual({'C3', 'C4'}, {r['location']['metadata']['cells']['unit_cost']['address'] for r in costs})
        scope = report['coverage']['source_queries']['source:work']
        self.assertEqual(4, scope['coverage']['scanned_rows'])
        self.assertEqual(3, scope['coverage']['matched_rows'])
        self.assertTrue(scope['coverage']['scan_complete'])
        self.assertTrue(scope['scope_review_required'])
        self.assertFalse(scope['business_population_completeness_proven'])
        self.assertIn('not_proof_of_historical_lineage', report['semantics'])
        self.assertEqual(4, report['rule_evidence'][0]['locations'][0]['row'])

    def test_batch_query_cache_reused_for_another_result_row(self):
        plan = self.plan()
        with mock.patch.object(xlsx_query, '_query_xlsx_core', wraps=xlsx_query._query_xlsx_core) as scan:
            first = self.verify(plan)
            cold_calls = scan.call_count
            second_plan = copy.deepcopy(plan)
            second_plan['result']['row'] = 4
            second = self.verify(second_plan)
            self.assertEqual(cold_calls, scan.call_count)
        self.assertEqual('9.70', first['checks'][0]['calculated'])
        self.assertEqual('9.10', second['checks'][0]['calculated'])
        self.assertTrue(all(q['cache']['hit'] for q in second['coverage']['source_queries'].values()))
        self.assertTrue((self.base / '.dd_cache/xlsx_query').is_dir())

    def test_tuple_boundaries_and_delimiters_do_not_collide(self):
        plan = self.plan()
        self.assertEqual('9.70', self.verify(plan)['checks'][0]['calculated'])
        plan['result']['row'] = 4
        self.assertEqual('9.10', self.verify(plan)['checks'][0]['calculated'])
        # The excluded (a, b|c) record would collide under a naive pipe-join.
        scope = self.verify(plan)['coverage']['source_queries']['source:cost']
        self.assertEqual(3, scope['coverage']['matched_rows'])

    def test_legacy_single_key_join_syntax_remains_supported(self):
        plan = self.plan()
        join = plan['joins'][0]
        join.pop('keys')
        join.update(left={'source': 'work', 'field': 'part'}, right_field='part')
        jsonschema.validate(plan, self.schema)
        report = self.verify(plan)
        self.assertEqual('reconciled_not_proven_lineage', report['status'])
        self.assertEqual(1, len(report['joins'][0]['keys']))

    def test_duplicate_composite_right_key_violates_declared_cardinality(self):
        plan = self.plan()
        self.workbook('costs.xlsx', ['part', 'plant', 'unit_cost'],
                      [['ab', 'c', '1.25'], ['ab', 'c', '1.25'], ['a', 'bc', '2.40']], header_row=2)
        with self.assertRaisesRegex(PlanError, '右键不唯一'):
            self.verify(plan)

    def test_null_component_cannot_become_a_matching_tuple(self):
        plan = self.plan()
        self.workbook('work.xlsx', ['order', 'part', 'plant', 'quantity'],
                      [['0001', 'ab', None, 2]], header_row=3)
        with self.assertRaisesRegex(PlanError, '左键缺失'):
            self.verify(plan)
        plan = self.plan()
        self.workbook('costs.xlsx', ['part', 'plant', 'unit_cost'],
                      [['ab', None, '1.25'], ['a', 'bc', '2.40']], header_row=2)
        # The full tuple prefilter must not replace a missing plant component.
        with self.assertRaisesRegex(PlanError, '缺连接对象'):
            self.verify(plan)

    def test_many_to_many_expansion_obeys_join_budget(self):
        plan = self.plan()
        self.workbook('work.xlsx', ['order', 'part', 'plant', 'quantity'],
                      [['0001', 'ab', 'c', 2]], header_row=3)
        self.workbook('costs.xlsx', ['part', 'plant', 'unit_cost'],
                      [['ab', 'c', '1.25'], ['ab', 'c', '1.25']], header_row=2)
        plan['joins'][0]['cardinality'] = 'many_to_many'
        plan['max_join_rows'] = 1
        with self.assertRaisesRegex(PlanError, '连接结果超过'):
            self.verify(plan)

    def test_same_workbook_distinct_queries_do_not_share_wrong_in_memory_rows(self):
        self.workbook('roles.xlsx', ['role', 'code', 'value'],
                      [['base', 'A', '2'], ['rate', 'A', '9']])
        (self.base / 'result.csv').write_text('total\n18\n', encoding='utf-8')
        (self.base / 'rule.md').write_text('Multiply the base by the matching rate.', encoding='utf-8')
        plan = {'version': 1, 'result': {'file': 'result.csv', 'row': 2},
                'sources': {role: {'file': 'roles.xlsx', 'query': {'where': {'role': [role]},
                    'columns': ['code', 'value']}} for role in ('base', 'rate')},
                'select': {'source': 'base'},
                'joins': [{'source': 'rate', 'left': {'source': 'base', 'field': 'code'},
                           'right_field': 'code', 'cardinality': 'one_to_one'}],
                'outputs': [{'result_field': 'total', 'expression': {'mul': [
                    {'source': 'base', 'field': 'value'}, {'source': 'rate', 'field': 'value'}]}}],
                'rule_evidence': [{'file': 'rule.md', 'quote': 'Multiply the base'}]}
        report = self.verify(plan)
        self.assertEqual('18', report['checks'][0]['calculated'])
        self.assertEqual({2, 3}, {r['location']['row'] for r in report['source_rows']})

    def test_truncated_query_can_report_equal_without_claiming_complete_reconciliation(self):
        plan = self.plan()
        self.workbook('results.xlsx', ['order', 'total', 'lines'], [['0001', '2.50', 1]], header_row=2)
        plan['sources']['work']['query']['limit'] = 1
        report = self.verify(plan)
        self.assertTrue(all(c['status'] == 'equal' for c in report['checks']))
        self.assertEqual('incomplete', report['status'])
        self.assertFalse(report['coverage']['ingestion_complete'])
        self.assertTrue(report['coverage']['source_queries']['source:work']['coverage']['truncated'])

    def test_projection_missing_a_required_join_component_is_not_guessed(self):
        plan = self.plan()
        plan['sources']['cost']['query']['columns'].remove('plant')
        with self.assertRaisesRegex(PlanError, '缺字段'):
            self.verify(plan)

    def test_unknown_query_keys_unsupported_formats_and_invalid_shapes_are_rejected(self):
        base = self.plan()
        mutations = [
            {'query': {'file': 'other.xlsx'}}, {'query': {'sheet': 'Other'}},
            {'query': {'header_row': 5}}, {'query': {'cache_dir': 'elsewhere'}},
            {'query': {'where': {'order': []}}}, {'query': {'where': {'order': [{'eval': 'no'}]}}},
            {'query': {'columns': ['part', 'part']}}, {'query': {'key_rows': []}},
            {'query': {'key_rows': [{'part': 'A'}, {'plant': 'P'}]}},
            {'query': {'limit': True}}, {'query': {'reader': 'unvalidated'}}, {'header_row': False},
        ]
        for update in mutations:
            with self.subTest(update=update):
                plan = copy.deepcopy(base)
                plan['sources']['work'].update(update)
                with self.assertRaises(PlanError): validate_plan_shape(plan)
        for file in ('work.csv', 'work.xls', 'work.sql'):
            plan = copy.deepcopy(base)
            plan['sources']['work']['file'] = file
            with self.assertRaisesRegex(PlanError, '仅支持XLSX'):
                validate_plan_shape(plan)
            self.assertTrue(list(jsonschema.Draft7Validator(self.schema).iter_errors(plan)))

    def test_compound_and_legacy_keys_are_strictly_exclusive(self):
        plan = self.plan()
        plan['joins'][0]['left'] = {'source': 'work', 'field': 'part'}
        plan['joins'][0]['right_field'] = 'part'
        with self.assertRaisesRegex(PlanError, '不能混用'):
            validate_plan_shape(plan)
        self.assertTrue(list(jsonschema.Draft7Validator(self.schema).iter_errors(plan)))
        plan = self.plan()
        plan['joins'][0]['keys'] = []
        with self.assertRaises(PlanError): validate_plan_shape(plan)
        self.assertTrue(list(jsonschema.Draft7Validator(self.schema).iter_errors(plan)))

    def test_streamed_date_and_time_match_legacy_result_across_excel_epochs(self):
        for value in (datetime(2025, 4, 3, 15, 28, 17), time(12, 30, 15)):
            with self.subTest(value=value):
                self.workbook('date_result.xlsx', ['shipment', 'departed'], [['001', value]])
                book = Workbook()
                book.epoch = MAC_EPOCH
                sheet = book.active
                sheet.title = 'Records'
                sheet.append(['shipment', 'departed'])
                sheet.append(['001', value])
                book.save(self.base / 'date_source.xlsx')
                book.close()
                (self.base / 'date_rule.md').write_text('Copy departure for the exact shipment identifier.', encoding='utf-8')
                plan = {'version': 1, 'result': {'file': 'date_result.xlsx', 'row': 2, 'sheet': 'Records'},
                        'sources': {'event': {'file': 'date_source.xlsx', 'sheet': 'Records', 'header_row': 1,
                            'query': {'where': {'shipment': ['001']}}}},
                        'select': {'source': 'event', 'where': [{'field': 'shipment', 'result_field': 'shipment'}]},
                        'outputs': [{'result_field': 'departed', 'type': 'text',
                                     'expression': {'source': 'event', 'field': 'departed'}}],
                        'rule_evidence': [{'file': 'date_rule.md', 'quote': 'Copy departure'}]}
                report = self.verify(plan)
                self.assertEqual('reconciled_not_proven_lineage', report['status'])
                self.assertEqual('equal', report['checks'][0]['status'])
                source_cell = report['source_rows'][0]['location']['metadata']['cells']['departed']
                self.assertEqual('1904', source_cell['excel_epoch'])
                self.assertIn('raw_value', source_cell)
                self.assertEqual('B2', source_cell['address'])
                if isinstance(value, datetime):
                    result_cell = report['result']['metadata']['cells']['departed']
                    self.assertEqual(str(value), report['checks'][0]['actual'])
                    self.assertNotEqual(source_cell['raw_value'], str(report['checks'][0]['actual']))

    def test_cached_shared_formula_value_keeps_unknown_expression_visible(self):
        plan = self.plan()
        path = self.base / 'costs.xlsx'
        with ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        main = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        document = ET.fromstring(entries['xl/worksheets/sheet1.xml'])
        for address, expression, value in [('C3', '1+0.25', '1.25'), ('C4', None, '2.40')]:
            cell = document.find(f'.//{main}c[@r="{address}"]')
            cell.attrib.pop('t', None)
            for child in list(cell): cell.remove(child)
            formula = ET.SubElement(cell, main + 'f', {'t': 'shared', 'si': '0'})
            if expression is not None:
                formula.text = expression
                formula.attrib['ref'] = 'C3:C4'
            ET.SubElement(cell, main + 'v').text = value
        entries['xl/worksheets/sheet1.xml'] = ET.tostring(document)
        with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
            for name, data in entries.items(): archive.writestr(name, data)
        report = self.verify(plan)
        self.assertEqual('equal', report['checks'][0]['status'])
        self.assertEqual('incomplete', report['status'])
        self.assertFalse(report['coverage']['ingestion_complete'])
        codes = {issue['code'] for issues in report['ingestion_issues'].values() for issue in issues}
        self.assertIn('shared_formula_unresolved', codes)
        follower = next(row for row in report['source_rows'] if row['source'] == 'cost' and row['location']['row'] == 4)
        cell = follower['location']['metadata']['cells']['unit_cost']
        self.assertTrue(cell['formula_present'])
        self.assertTrue(cell['formula_cached'])
        self.assertFalse(cell['formula_expression_complete'])

    def test_batch_reuses_original_workbook_queries_once_and_reports_each_row(self):
        plan = self.plan()
        with mock.patch.object(xlsx_query, 'query_xlsx', wraps=xlsx_query.query_xlsx) as query:
            report = verify_plan_batch(plan, [4, 999, 3], self.base)
        self.assertEqual(4, query.call_count)  # result, rule, work, cost; not four per result row
        self.assertEqual('batch_processed_with_errors', report['status'])
        self.assertEqual({'requested_rows': 3, 'processed_rows': 2, 'failed_rows': 1,
                          'report_status_counts': {'reconciled_not_proven_lineage': 2}}, report['summary'])
        self.assertEqual([4, 999, 3], [r['row'] for r in report['results']])
        self.assertEqual('9.10', report['results'][0]['report']['checks'][0]['calculated'])
        self.assertEqual('error', report['results'][1]['status'])
        self.assertEqual(str(self.base / 'results.xlsx'), report['results'][1]['result']['file'])
        self.assertEqual('9.70', report['results'][2]['report']['checks'][0]['calculated'])
        self.assertEqual(4, report['coverage']['loaded_ref_snapshots'])

    def test_batch_legacy_parser_and_file_hash_are_shared_across_result_rows(self):
        (self.base / 'results.csv').write_text('order,total\n001,2.50\n002,7.20\n', encoding='utf-8')
        (self.base / 'details.csv').write_text('order,quantity,price\n001,2,1.25\n002,3,2.40\n', encoding='utf-8')
        (self.base / 'rules.md').write_text('Multiply order quantity by price.', encoding='utf-8')
        plan = {'version': 1, 'result': {'file': 'results.csv', 'row': 2},
                'sources': {'line': {'file': 'details.csv'}},
                'select': {'source': 'line', 'where': [{'field': 'order', 'result_field': 'order'}]},
                'outputs': [{'result_field': 'total', 'expression': {'mul': [
                    {'source': 'line', 'field': 'quantity'}, {'source': 'line', 'field': 'price'}]}}],
                'rule_evidence': [{'file': 'rules.md', 'quote': 'Multiply order quantity'}]}
        original_parse = FileParser.parse
        with mock.patch.object(FileParser, 'parse', autospec=True, side_effect=original_parse) as parse:
            with mock.patch.object(reconcile, 'sha256_file', wraps=reconcile.sha256_file) as digest:
                report = verify_plan_batch(plan, [2, 3], self.base)
        self.assertEqual(3, parse.call_count)
        self.assertEqual(3, digest.call_count)
        self.assertEqual(['2.50', '7.20'], [r['report']['checks'][0]['calculated'] for r in report['results']])
        self.assertEqual(0, report['summary']['failed_rows'])

    def test_batch_bad_source_is_attempted_once_but_next_batch_can_retry_after_fix(self):
        (self.base / 'results.csv').write_text('order,total\n001,2.50\n002,7.20\n', encoding='utf-8')
        (self.base / 'rules.md').write_text('Multiply order quantity by price.', encoding='utf-8')
        self.workbook('bad_source.xlsx', ['title'], [], header_row=1)
        plan = {'version': 1, 'result': {'file': 'results.csv', 'row': 2},
                'sources': {'line': {'file': 'bad_source.xlsx', 'header_row': 3,
                                    'query': {'where': {'order': ['001', '002']}}}},
                'select': {'source': 'line', 'where': [{'field': 'order', 'result_field': 'order'}]},
                'outputs': [{'result_field': 'total', 'expression': {'mul': [
                    {'source': 'line', 'field': 'quantity'}, {'source': 'line', 'field': 'price'}]}}],
                'rule_evidence': [{'file': 'rules.md', 'quote': 'Multiply order quantity'}]}
        with mock.patch.object(xlsx_query, 'query_xlsx', wraps=xlsx_query.query_xlsx) as query:
            failed = verify_plan_batch(plan, [2, 3], self.base)
            self.assertEqual(1, query.call_count)
            self.assertEqual(['error', 'error'], [r['status'] for r in failed['results']])
            self.assertEqual(2, failed['summary']['failed_rows'])
            self.assertEqual(1, failed['coverage']['failed_ref_snapshots'])
            self.assertEqual(failed['results'][0]['error'], failed['results'][1]['error'])
            self.assertEqual(['ValueError', 'ValueError'], [r['error_type'] for r in failed['results']])
            self.workbook('bad_source.xlsx', ['order', 'quantity', 'price'],
                          [['001', 2, '1.25'], ['002', 3, '2.40']], header_row=3)
            corrected = verify_plan_batch(plan, [2, 3], self.base)
            self.assertEqual(2, query.call_count)
        self.assertEqual(0, corrected['summary']['failed_rows'])
        self.assertEqual(0, corrected['coverage']['failed_ref_snapshots'])
        self.assertEqual(['2.50', '7.20'], [r['report']['checks'][0]['calculated'] for r in corrected['results']])

    def test_cli_batch_retains_successes_around_an_invalid_requested_row(self):
        plan = self.plan()
        plan_path, output_path = self.base / 'plan.json', self.base / 'batch.json'
        plan_path.write_text(json.dumps(plan), encoding='utf-8')
        completed = subprocess.run([sys.executable, str(ROOT / 'scripts/reconcile.py'),
            '--plan', str(plan_path), '--rows', '3', '999', '4', '--out', str(output_path)],
            capture_output=True, text=True)
        self.assertEqual(2, completed.returncode)
        report = json.loads(output_path.read_text(encoding='utf-8'))
        self.assertEqual(['processed', 'error', 'processed'], [r['status'] for r in report['results']])


if __name__ == '__main__':
    unittest.main()
