"""Public-pipeline checks on independent commerce, manufacturing and freight data.

Every fixture is synthetic and temporary. Explicit mappings are supplied by
the caller; these checks do not imply automatic rule or schema inference.
"""
import csv
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from reconcile import verify_plan
from xlsx_query import query_xlsx


class GenericSchemaVariants(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='generic-lineage-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def xlsx(self, name, headers, records, *, sheet='Records', header_row=1,
             extra_sheet=False):
        from openpyxl import Workbook
        workbook = Workbook()
        target = workbook.active
        target.title = sheet
        if header_row > 1:
            target.cell(1, 1, 'Synthetic export title')
        for column, value in enumerate(headers, 1):
            target.cell(header_row, column, value)
        for row, record in enumerate(records, header_row + 1):
            for column, value in enumerate(record, 1):
                target.cell(row, column, value)
        if extra_sheet:
            other = workbook.create_sheet('Instructions')
            other.append(['description'])
            other.append(['The operational rows are on the named sheet.'])
        path = self.root / name
        workbook.save(path)
        workbook.close()
        return path

    def csv(self, name, headers, records):
        path = self.root / name
        with path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(records)
        return path

    def text(self, name, content):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return path

    def rule(self, statement):
        self.text('rule.md', statement)
        return [{'file': 'rule.md', 'quote': statement}]

    def reconcile_cli(self, plan):
        plan_file = self.root / 'plan.json'
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        report_file = self.root / 'reconciliation.json'
        unrelated = self.root / 'unrelated-working-directory'
        unrelated.mkdir(exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/reconcile.py'), '--plan', str(plan_file),
             '--out', str(report_file)], cwd=unrelated, capture_output=True,
            text=True, encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'},
        )
        self.assertTrue(report_file.is_file(), completed.stdout + completed.stderr)
        return completed, json.loads(report_file.read_text(encoding='utf-8'))

    def freight_plan(self):
        self.csv('dispatch.csv', ['consignment_ref', 'itinerary_code'],
                 [['PK-069', 'L-008'], ['PK-070', 'L-009']])
        self.text('legs.sql',
                  'CREATE TABLE legs (route_code TEXT, carrier_key TEXT, distance_km DECIMAL);\n'
                  "INSERT INTO legs VALUES ('L-008','CK-44',42.5),('L-009','CK-45',9.0);\n")
        self.xlsx('tariffs.xlsx', ['rate_per_km', 'carrier_key', 'currency'],
                  [['1.76', 'CK-44', 'EUR'], ['8.00', 'CK-45', 'EUR']], sheet='Tariff v2')
        self.csv('freight.csv', ['tracking_ref', 'freight_total', 'carrier', 'route_km'],
                 [['PK-069', '74.80', 'CK-44', '42.50']])
        return {
            'version': 1,
            'result': {'file': 'freight.csv', 'row': 2},
            'sources': {
                'dispatch': {'file': 'dispatch.csv'},
                'leg': {'file': 'legs.sql', 'table': 'legs'},
                'tariff': {'file': 'tariffs.xlsx', 'sheet': 'Tariff v2'},
            },
            'select': {'source': 'dispatch', 'where': [
                {'field': 'consignment_ref', 'result_field': 'tracking_ref'}]},
            'joins': [
                {'source': 'leg', 'left': {'source': 'dispatch', 'field': 'itinerary_code'},
                 'right_field': 'route_code', 'cardinality': 'one_to_one'},
                {'source': 'tariff', 'left': {'source': 'leg', 'field': 'carrier_key'},
                 'right_field': 'carrier_key', 'cardinality': 'many_to_one'},
            ],
            'outputs': [
                {'result_field': 'freight_total', 'expression': {'mul': [
                    {'source': 'leg', 'field': 'distance_km'},
                    {'source': 'tariff', 'field': 'rate_per_km'}]},
                 'quantize': '0.01', 'rounding': 'ROUND_HALF_UP'},
                {'result_field': 'carrier', 'type': 'text',
                 'expression': {'source': 'leg', 'field': 'carrier_key'}},
                {'result_field': 'route_km',
                 'expression': {'source': 'leg', 'field': 'distance_km'}},
            ],
            'rule_evidence': self.rule(
                'Freight total equals route distance multiplied by the carrier tariff, rounded to cents.'),
        }

    def test_query_variants_use_explicit_sheet_header_names_and_column_order(self):
        variants = [
            ('Export A', 1, ['site_ref', 'lot_ref', 'net_mass'],
             {'site': 'site_ref', 'lot': 'lot_ref', 'mass': 'net_mass'}),
            ('Plant ledger', 4, ['净重量', '厂区', '厂内料号'],
             {'site': '厂区', 'lot': '厂内料号', 'mass': '净重量'}),
            ('Vendor version 9', 7, ['Batch / external', 'Weight [kg]', 'Facility code'],
             {'site': 'Facility code', 'lot': 'Batch / external', 'mass': 'Weight [kg]'}),
        ]
        logical_records = [
            {'site': 'east', 'lot': '00041', 'mass': '2.375'},
            {'site': 'west', 'lot': '00041', 'mass': '90.0'},
            {'site': 'east', 'lot': '41', 'mass': '80.0'},
            {'site': 'east', 'lot': '00041', 'mass': '1.125'},
        ]
        for index, (sheet, header_row, headers, mapping) in enumerate(variants):
            with self.subTest(sheet=sheet):
                reverse = {label: role for role, label in mapping.items()}
                records = [[record[reverse[label]] for label in headers]
                           for record in logical_records]
                path = self.xlsx(f'variant-{index}.xlsx', headers, records, sheet=sheet,
                                 header_row=header_row, extra_sheet=True)
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                packet = query_xlsx(path, sheet=sheet, header_row=header_row,
                                    where={mapping['site']: ['east'], mapping['lot']: ['00041']},
                                    columns=[mapping['mass'], mapping['lot']])
                self.assertEqual(packet['coverage']['scanned_rows'], 4)
                self.assertEqual(packet['coverage']['matched_rows'], 2)
                self.assertTrue(packet['coverage']['filter_evaluation_complete'])
                self.assertEqual([record['row_num'] for record in packet['rows']],
                                 [header_row + 1, header_row + 4])
                self.assertEqual([record['values'][mapping['lot']] for record in packet['rows']],
                                 ['00041', '00041'])
                mass = sum(Decimal(record['values'][mapping['mass']]) for record in packet['rows'])
                self.assertEqual(mass, Decimal('3.500'))
                from openpyxl.utils import get_column_letter
                mass_column = get_column_letter(headers.index(mapping['mass']) + 1)
                self.assertEqual(packet['rows'][1]['cells'][mapping['mass']]['address'],
                                 f'{mass_column}{header_row + 4}')
                self.assertEqual(packet['source_sha256'], before)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_multisheet_and_missing_columns_require_explicit_valid_mapping(self):
        path = self.xlsx('warehouse.xlsx', ['warehouse_ref', 'parcel_ref'],
                         [['north', '00083']], sheet='Loads', extra_sheet=True)
        with self.assertRaises(ValueError):
            query_xlsx(path, where={'parcel_ref': ['00083']})
        with self.assertRaises(ValueError):
            query_xlsx(path, sheet='Unknown', where={'parcel_ref': ['00083']})
        with self.assertRaises(ValueError):
            query_xlsx(path, sheet='Loads', where={'unmapped_external_key': ['00083']})
        actual = query_xlsx(path, sheet='Loads', where={'parcel_ref': ['00083']})
        self.assertEqual(actual['rows'][0]['values']['parcel_ref'], '00083')

    def test_csv_sql_xlsx_three_hop_chain_recomputes_freight_via_public_cli(self):
        plan = self.freight_plan()
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.root.iterdir() if path.is_file()}
        completed, report = self.reconcile_cli(plan)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr + json.dumps(report, ensure_ascii=False))
        checks = {check['result_field']: check for check in report['checks']}
        self.assertEqual(Decimal(checks['freight_total']['calculated']), Decimal('74.80'))
        self.assertEqual(Decimal(checks['route_km']['calculated']), Decimal('42.5'))
        self.assertEqual(checks['carrier']['calculated'], 'CK-44')
        self.assertTrue(all(check['status'] == 'equal' for check in checks.values()))
        self.assertEqual([(join['input_rows'], join['output_rows']) for join in report['joins']],
                         [(1, 1), (1, 1)])
        citations = {item['source']: item for item in report['source_rows']}
        self.assertEqual(set(citations), {'dispatch', 'leg', 'tariff'})
        self.assertEqual(citations['dispatch']['location']['row'], 2)
        self.assertEqual(citations['leg']['location']['metadata']['table'], 'legs')
        self.assertEqual(citations['tariff']['location']['sheet'], 'Tariff v2')
        self.assertEqual(citations['tariff']['location']['row'], 2)
        self.assertEqual(report['status'], 'reconciled_not_proven_lineage')
        self.assertIsNone(report['confidence'])
        for path, digest in original.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_one_to_many_order_lines_are_all_included_in_amount_and_count(self):
        self.xlsx('orders.xlsx', ['order_ref'], [['O-841'], ['O-842']], sheet='Orders')
        self.text('lines.sql',
                  'CREATE TABLE order_lines (order_token TEXT, sku TEXT, units DECIMAL, unit_net DECIMAL);\n'
                  "INSERT INTO order_lines VALUES ('O-841','S-A',3,6.25),"
                  "('O-841','S-B',2,4.10),('O-841','S-C',1,5.28),('O-842','S-Z',9,90.0);\n")
        self.csv('order-summary.csv', ['requested_order', 'gross', 'line_count'],
                 [['O-841', '32.23', '3']])
        plan = {
            'version': 1, 'result': {'file': 'order-summary.csv', 'row': 2},
            'sources': {'order': {'file': 'orders.xlsx', 'sheet': 'Orders'},
                        'line': {'file': 'lines.sql', 'table': 'order_lines'}},
            'select': {'source': 'order', 'where': [
                {'field': 'order_ref', 'result_field': 'requested_order'}]},
            'joins': [{'source': 'line', 'left': {'source': 'order', 'field': 'order_ref'},
                       'right_field': 'order_token', 'cardinality': 'one_to_many'}],
            'outputs': [
                {'result_field': 'gross', 'aggregate': 'sum', 'expression': {'mul': [
                    {'source': 'line', 'field': 'units'}, {'source': 'line', 'field': 'unit_net'}]}},
                {'result_field': 'line_count', 'aggregate': 'count'},
            ],
            'rule_evidence': self.rule('Order gross is the sum of every line quantity times its unit net price.'),
        }
        report = verify_plan(plan, self.root)
        checks = {check['result_field']: check for check in report['checks']}
        self.assertEqual(Decimal(checks['gross']['calculated']), Decimal('32.23'))
        self.assertEqual(Decimal(checks['line_count']['calculated']), 3)
        self.assertEqual(report['joins'][0]['output_rows'], 3)
        line_citations = [item for item in report['source_rows'] if item['source'] == 'line']
        self.assertEqual({item['fields']['sku'] for item in line_citations}, {'S-A', 'S-B', 'S-C'})
        self.assertEqual(len(line_citations), 3)

    def test_long_material_records_reconcile_separate_wide_output_columns(self):
        self.xlsx('production-summary.xlsx', ['batch_ref', 'steel_kg', 'resin_kg', 'total_kg'],
                  [['B-0039', '13.2', '8.7', '21.9']], sheet='Build Summary')
        self.csv('material-inputs.csv', ['batch_key', 'material', 'net_mass'],
                 [['B-0039', 'steel', '8.1'], ['B-0039', 'resin', '8.7'],
                  ['B-0039', 'steel', '5.1'], ['B-0040', 'steel', '900.0']])
        rules = self.rule('For each batch and material, sum the recorded input net masses; total sums all materials.')
        for field, material, expected, expected_rows in [
            ('steel_kg', 'steel', '13.2', 2), ('resin_kg', 'resin', '8.7', 1),
            ('total_kg', None, '21.9', 3),
        ]:
            with self.subTest(field=field):
                predicates = [{'field': 'batch_key', 'result_field': 'batch_ref'}]
                if material is not None:
                    predicates.append({'field': 'material', 'literal': material})
                plan = {
                    'version': 1,
                    'result': {'file': 'production-summary.xlsx', 'sheet': 'Build Summary', 'row': 2},
                    'sources': {'input': {'file': 'material-inputs.csv'}},
                    'select': {'source': 'input', 'where': predicates},
                    'outputs': [{'result_field': field, 'aggregate': 'sum',
                                 'expression': {'source': 'input', 'field': 'net_mass'}}],
                    'rule_evidence': rules,
                }
                report = verify_plan(plan, self.root)
                self.assertEqual(report['status'], 'reconciled_not_proven_lineage')
                self.assertEqual(Decimal(report['checks'][0]['calculated']), Decimal(expected))
                self.assertEqual(report['checks'][0]['input_rows'], expected_rows)
                self.assertEqual(len(report['source_rows']), expected_rows)

    def test_grouped_aggregate_uses_both_fields_of_the_result_group(self):
        self.csv('moves.csv', ['depot_ref', 'operation_day', 'loaded_mass'],
                 [['D-07', '2031-04-03', '5.25'], ['D-07', '2031-04-03', '8.75'],
                  ['D-07', '2031-04-03', '1.00'], ['D-08', '2031-04-03', '3.50'],
                  ['D-08', '2031-04-03', '2.25'], ['D-07', '2031-04-04', '700']])
        self.xlsx('daily-loads.xlsx', ['day', 'site', 'loaded_kg', 'movement_count'],
                  [['2031-04-03', 'D-07', '15.00', '3'],
                   ['2031-04-03', 'D-08', '5.75', '2']], sheet='Daily Loads')
        rule = self.rule('Daily loaded mass and movement count group by both depot and operation day.')
        for row, expected_mass, expected_count in [(2, '15.00', 3), (3, '5.75', 2)]:
            with self.subTest(result_row=row):
                report = verify_plan({
                    'version': 1,
                    'result': {'file': 'daily-loads.xlsx', 'sheet': 'Daily Loads', 'row': row},
                    'sources': {'move': {'file': 'moves.csv'}},
                    'select': {'source': 'move', 'where': [
                        {'field': 'depot_ref', 'result_field': 'site'},
                        {'field': 'operation_day', 'result_field': 'day'}]},
                    'outputs': [
                        {'result_field': 'loaded_kg', 'aggregate': 'sum',
                         'expression': {'source': 'move', 'field': 'loaded_mass'}},
                        {'result_field': 'movement_count', 'aggregate': 'count'}],
                    'rule_evidence': rule,
                }, self.root)
                checks = {item['result_field']: item for item in report['checks']}
                self.assertEqual(Decimal(checks['loaded_kg']['calculated']), Decimal(expected_mass))
                self.assertEqual(int(checks['movement_count']['calculated']), expected_count)
                self.assertEqual(len(report['source_rows']), expected_count)
                self.assertTrue(all(item['fields']['operation_day'] == '2031-04-03'
                                    for item in report['source_rows']))

    def test_missing_join_column_and_missing_join_record_fail_explicitly(self):
        for missing_column in (True, False):
            with self.subTest(missing_column=missing_column):
                plan = self.freight_plan()
                if missing_column:
                    plan['joins'][0]['right_field'] = 'unmapped_route_column'
                else:
                    self.text('legs.sql',
                              'CREATE TABLE legs (route_code TEXT, carrier_key TEXT, distance_km DECIMAL);\n'
                              "INSERT INTO legs VALUES ('OTHER','CK-44',42.5);\n")
                completed, report = self.reconcile_cli(plan)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report['status'], 'invalid_or_unavailable')
                self.assertNotIn('checks', report)
                self.assertTrue(report['error'])

    def test_duplicate_unique_lookup_key_is_not_double_counted(self):
        plan = self.freight_plan()
        self.xlsx('tariffs.xlsx', ['rate_per_km', 'carrier_key', 'currency'],
                  [['1.76', 'CK-44', 'EUR'], ['2.00', 'CK-44', 'EUR']], sheet='Tariff v2')
        completed, report = self.reconcile_cli(plan)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report['status'], 'invalid_or_unavailable')
        self.assertNotIn('checks', report)

    def test_uncached_formula_blocks_filter_and_numeric_reconciliation(self):
        plan = self.freight_plan()
        path = self.xlsx('tariffs.xlsx', ['rate_per_km', 'carrier_key', 'currency'],
                         [['=88/50', 'CK-44', 'EUR']], sheet='Tariff v2')
        filtered = query_xlsx(path, sheet='Tariff v2', where={'rate_per_km': [None]})
        self.assertFalse(filtered['coverage']['filter_evaluation_complete'])
        self.assertEqual(filtered['coverage']['unavailable_filter_rows'], 1)
        self.assertEqual(filtered['coverage']['matched_rows'], 0)
        completed, report = self.reconcile_cli(plan)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report['status'], 'invalid_or_unavailable')
        self.assertNotIn('checks', report)

    def test_equal_uncited_plan_remains_incomplete(self):
        plan = self.freight_plan()
        plan.pop('rule_evidence')
        report = verify_plan(plan, self.root)
        self.assertEqual(report['status'], 'incomplete')
        self.assertTrue(all(check['status'] == 'equal' for check in report['checks']))
        self.assertEqual(Decimal(report['checks'][0]['calculated']), Decimal('74.80'))
        self.assertEqual(len(report['source_rows']), 3)
        self.assertIsNone(report['confidence'])

    def test_wrong_result_total_exposes_signed_difference(self):
        plan = self.freight_plan()
        self.csv('freight.csv', ['tracking_ref', 'freight_total', 'carrier', 'route_km'],
                 [['PK-069', '74.81', 'CK-44', '42.50']])
        report = verify_plan(plan, self.root)
        amount = next(check for check in report['checks'] if check['result_field'] == 'freight_total')
        self.assertEqual(report['status'], 'mismatch')
        self.assertEqual(amount['status'], 'mismatch')
        self.assertEqual(Decimal(amount['calculated']), Decimal('74.80'))
        self.assertEqual(Decimal(amount['actual']), Decimal('74.81'))
        self.assertEqual(Decimal(amount['difference']), Decimal('-0.01'))

    def test_trace_cli_exposes_candidates_without_claiming_completed_lineage(self):
        source = self.csv('dispatch-lines.csv', ['order_ref', 'sku', 'units', 'line_value'],
                          [['EC-152', 'P-026', '3', '18.75'],
                           ['EC-153', 'P-027', '8', '18.75']])
        result = self.csv('picked-line.csv', ['order_ref', 'sku', 'line_value'],
                          [['EC-152', 'P-026', '18.75']])
        output = self.root / 'candidate-report.json'
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/detective.py'), 'trace', '--result', str(result),
             '--row', '2', '--sources', str(source), '--out', str(output), '--no-html'],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8',
            env={**os.environ, 'PYTHONUTF8': '1'},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(report['analysis_stage'], 'candidate_links')
        locations = [item for item in report['source_locations'] if Path(item['file']) == source]
        self.assertTrue(any(item['row'] == 2 for item in locations))
        self.assertTrue(any(item['field_name'] == 'line_value'
                            and Decimal(item['matched_value']) == Decimal('18.75')
                            for item in report['evidence']))
        self.assertTrue(report['conclusions'])
        self.assertTrue(all(item['status'] == 'tentative' for item in report['conclusions']))

    def compound_parcels(self):
        path = self.xlsx('parcel-register.xlsx',
                         ['scan_day', 'mass', 'bag_ref', 'kind', 'origin'], [
                             ['2033-11-10', '2.10', '00061', 'release', 'north'],
                             ['2033-11-10', '3.20', '00062', 'release', 'south'],
                             ['2033-11-10', '999', '00062', 'release', 'north'],
                             ['2033-11-10', '888', '00061', 'release', 'south'],
                             ['2033-11-11', '777', '00061', 'release', 'north'],
                             ['2033-11-10', '666', '00061', 'pending', 'north'],
                         ], sheet='Parcel Register', header_row=2)
        keys = [{'origin': 'north', 'bag_ref': '00061'},
                {'origin': 'south', 'bag_ref': '00062'}]
        where = {'scan_day': ['2033-11-10'], 'kind': ['release']}
        return path, keys, where

    def test_batched_compound_tuples_exclude_cross_product_and_reuse_cache(self):
        path, keys, where = self.compound_parcels()
        common = {'sheet': 'Parcel Register', 'header_row': 2,
                  'where': where, 'columns': ['mass']}
        compatible = query_xlsx(path, key_rows=keys, reader='compatible', **common)
        fast = query_xlsx(path, key_rows=keys, reader='fast',
                          cache_dir=self.root / 'query-cache', **common)
        self.assertEqual(fast['rows'], compatible['rows'])
        self.assertEqual(fast['coverage']['scanned_rows'], 6)
        self.assertEqual(fast['coverage']['matched_rows'], 2)
        self.assertEqual([row['row_num'] for row in fast['rows']], [3, 4])
        self.assertEqual(sum(Decimal(row['values']['mass']) for row in fast['rows']), Decimal('5.30'))
        self.assertTrue(all(set(row['values']) == {'mass'} for row in fast['rows']))
        self.assertFalse(fast['cache']['hit'])
        warm = query_xlsx(path, key_rows=[keys[1], keys[0], keys[1]], reader='fast',
                          cache_dir=self.root / 'query-cache', **common)
        self.assertTrue(warm['cache']['hit'])
        self.assertEqual(warm['cache']['key'], fast['cache']['key'])
        self.assertEqual(warm['rows'], fast['rows'])
        constrained = query_xlsx(path, key_rows=keys, sheet='Parcel Register', header_row=2,
                                 where={**where, 'origin': ['north']}, columns=['mass'])
        self.assertEqual(constrained['coverage']['matched_rows'], 1)
        self.assertEqual(constrained['rows'][0]['values']['mass'], '2.10')

    def test_compound_key_cli_writes_only_requested_tuple_rows(self):
        path, keys, where = self.compound_parcels()
        key_file = self.text('keys.json', json.dumps(keys))
        where_file = self.text('where.json', json.dumps(where))
        output = self.root / 'parcels.jsonl'
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/xlsx_query.py'), '--file', str(path),
             '--sheet', 'Parcel Register', '--header-row', '2', '--keys-json', str(key_file),
             '--where-json', str(where_file), '--columns', 'mass', '--reader', 'fast',
             '--no-cache', '--out', str(output)], cwd=self.root, capture_output=True,
            text=True, encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        lines = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(lines[0]['metadata']['coverage']['matched_rows'], 2)
        self.assertEqual(len(lines[1:]), 2)
        self.assertEqual(sum(Decimal(row['values']['mass']) for row in lines[1:]), Decimal('5.30'))

    def manufacturing_plan(self):
        self.csv('work-orders.csv', ['plant_ref', 'build_ref'],
                 [['P-N', '00052'], ['P-S', '00052']])
        self.xlsx('components.xlsx', ['units', 'external_order', 'catalog_code', 'factory_ref'], [
            ['2', '00052', 'K-100', 'P-N'], ['3', '00052', 'K-200', 'P-N'],
            ['70', '00052', 'K-100', 'P-S'], ['80', '00053', 'K-100', 'P-N'],
        ], sheet='Component ledger', header_row=3, extra_sheet=True)
        self.text('catalog.sql',
                  'CREATE TABLE component_catalog (plant_code TEXT, part_ref TEXT, net_cost DECIMAL);\n'
                  "INSERT INTO component_catalog VALUES ('P-N','K-100',4.45),('P-N','K-200',1.80),"
                  "('P-S','K-100',99.00),('P-S','K-200',99.00);\n")
        self.xlsx('work-in-process.xlsx', ['material_total', 'plant', 'reference', 'component_rows'],
                  [['14.30', 'P-N', '00052', '2'], ['999', 'P-S', '00052', '1']],
                  sheet='WIP cost audit', header_row=5, extra_sheet=True)
        plan = {
            'version': 1,
            'result': {'file': 'work-in-process.xlsx', 'sheet': 'WIP cost audit', 'row': 6,
                       'header_row': 5, 'query': {'where': {'plant': ['P-N']}}},
            'sources': {
                'work': {'file': 'work-orders.csv'},
                'part': {'file': 'components.xlsx', 'sheet': 'Component ledger', 'header_row': 3,
                         'query': {'key_rows': [{'factory_ref': 'P-N', 'external_order': '00052'}],
                                   'columns': ['factory_ref', 'external_order', 'catalog_code', 'units']}},
                'catalog': {'file': 'catalog.sql', 'table': 'component_catalog'},
            },
            'select': {'source': 'work', 'where': [
                {'field': 'plant_ref', 'result_field': 'plant'},
                {'field': 'build_ref', 'result_field': 'reference'}]},
            'joins': [
                {'source': 'part', 'keys': [
                    {'left': {'source': 'work', 'field': 'plant_ref'}, 'right_field': 'factory_ref'},
                    {'left': {'source': 'work', 'field': 'build_ref'}, 'right_field': 'external_order'}],
                 'cardinality': 'one_to_many'},
                {'source': 'catalog', 'keys': [
                    {'left': {'source': 'work', 'field': 'plant_ref'}, 'right_field': 'plant_code'},
                    {'left': {'source': 'part', 'field': 'catalog_code'}, 'right_field': 'part_ref'}],
                 'cardinality': 'many_to_one'},
            ],
            'outputs': [
                {'result_field': 'material_total', 'aggregate': 'sum', 'expression': {'mul': [
                    {'source': 'part', 'field': 'units'}, {'source': 'catalog', 'field': 'net_cost'}]}},
                {'result_field': 'component_rows', 'aggregate': 'count'},
            ],
            'rule_evidence': self.rule(
                'Match both plant and external order; sum component units times that plant catalog cost.'),
        }
        return plan

    def test_composite_multihop_joins_adapt_late_headers_without_synthetic_join_keys(self):
        plan = self.manufacturing_plan()
        completed, report = self.reconcile_cli(plan)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr + json.dumps(report, ensure_ascii=False))
        self.assertEqual(report['status'], 'reconciled_not_proven_lineage')
        checks = {check['result_field']: check for check in report['checks']}
        self.assertEqual(Decimal(checks['material_total']['calculated']), Decimal('14.30'))
        self.assertEqual(Decimal(checks['component_rows']['calculated']), 2)
        self.assertTrue(all(check['status'] == 'equal' for check in checks.values()))
        self.assertEqual([(join['input_rows'], join['output_rows']) for join in report['joins']],
                         [(1, 2), (2, 2)])
        part_rows = [item for item in report['source_rows'] if item['source'] == 'part']
        self.assertEqual({item['location']['row'] for item in part_rows}, {4, 5})
        self.assertTrue(all(item['location']['sheet'] == 'Component ledger' for item in part_rows))
        for item in part_rows:
            number = item['location']['row']
            cells = item['location']['metadata']['cells']
            self.assertEqual(cells['units']['address'], f'A{number}')
            self.assertEqual(cells['factory_ref']['address'], f'D{number}')
            self.assertEqual(cells['external_order']['value'], '00052')
        scopes = report['coverage']['source_queries']
        part_scope = next(scope for scope in scopes.values()
                          if Path(scope['file']).name == 'components.xlsx')
        self.assertEqual(part_scope['header_row'], 3)
        self.assertEqual(part_scope['coverage']['scanned_rows'], 4)
        self.assertEqual(part_scope['coverage']['matched_rows'], 2)
        self.assertTrue(part_scope['coverage']['scan_complete'])
        self.assertFalse(part_scope['coverage']['truncated'])
        self.assertFalse(part_scope['business_population_completeness_proven'])
        self.assertEqual(report['result']['row'], 6)
        self.assertEqual(report['result']['sheet'], 'WIP cost audit')
        self.assertEqual(report['result']['metadata']['cells']['material_total']['address'], 'A6')

    def test_compound_unique_key_collision_and_missing_component_fail(self):
        for invalid in ('duplicate', 'missing_component'):
            with self.subTest(invalid=invalid):
                plan = self.manufacturing_plan()
                if invalid == 'duplicate':
                    self.text('catalog.sql',
                              'CREATE TABLE component_catalog (plant_code TEXT, part_ref TEXT, net_cost DECIMAL);\n'
                              "INSERT INTO component_catalog VALUES ('P-N','K-100',4.45),"
                              "('P-N','K-100',4.99),('P-N','K-200',1.80);\n")
                else:
                    plan['joins'][1]['keys'][1]['right_field'] = 'unmapped_part_column'
                completed, report = self.reconcile_cli(plan)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report['status'], 'invalid_or_unavailable')
                self.assertNotIn('checks', report)

    def test_streamed_query_uncached_arithmetic_operand_is_not_zero(self):
        plan = self.manufacturing_plan()
        self.xlsx('components.xlsx', ['units', 'external_order', 'catalog_code', 'factory_ref'],
                  [['=1+1', '00052', 'K-100', 'P-N'], ['3', '00052', 'K-200', 'P-N']],
                  sheet='Component ledger', header_row=3, extra_sheet=True)
        completed, report = self.reconcile_cli(plan)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report['status'], 'invalid_or_unavailable')
        self.assertNotIn('checks', report)

    def test_truncated_streamed_subset_cannot_be_a_completed_reconciliation(self):
        plan = self.manufacturing_plan()
        plan['sources']['part']['query']['limit'] = 1
        completed, report = self.reconcile_cli(plan)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotEqual(report['status'], 'reconciled_not_proven_lineage')
        self.assertFalse(report['coverage']['ingestion_complete'])
        scope = report['coverage']['source_queries']['source:part']
        self.assertTrue(scope['coverage']['truncated'])
        self.assertEqual(scope['coverage']['matched_rows'], 2)
        self.assertEqual(scope['coverage']['returned_rows'], 1)


if __name__ == '__main__':
    unittest.main()
