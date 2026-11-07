"""Synthetic raw business records with independently declared linkage truth.

No hospital labels, record values, or generated reports are used by these cases.
The returned expectations describe record identities and evidence gaps, not
implementation scores. Generated files belong in a caller-supplied temp folder.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


def build_cases(root):
    root = Path(root)
    cases = []
    def case(name, domain):
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        item = {'name': name, 'domain': domain, 'folder': folder, 'sources': [],
                'config': {}, 'rows': 'all', 'expected_pairs': [], 'checks': []}
        cases.append(item)
        return item
    def table(c, name, fields, rows, result=False):
        file = c['folder'] / name
        with file.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            writer.writerows(rows)
        if result:
            c['result'] = file
        else:
            c['sources'].append(file)
        return file
    def pair(c, result_row, file, source_row, sheet=None, tuple_num=None):
        c['expected_pairs'].append((result_row, file, sheet, source_row, tuple_num))

    c = case('commerce_composite_lines', 'ecommerce')
    table(c, 'result.csv', ['order_ref', 'sku', 'line_total'], [['ORD-501', 'SKU-A', '18.60'], ['ORD-502', 'SKU-B', '21.70']], True)
    table(c, 'lines.csv', ['order_ref', 'sku', 'line_total'], [['ORD-501', 'SKU-B', '99'], ['ORD-501', 'SKU-A', '18.60'], ['ORD-502', 'SKU-A', '99'], ['ORD-502', 'SKU-B', '21.70']])
    pair(c, 2, 'lines.csv', 3); pair(c, 3, 'lines.csv', 5)
    c['expected_status'] = 'located'

    c = case('logistics_unscoped_generic_id', 'logistics')
    table(c, 'result.csv', ['id', 'parcel_description'], [['0007', 'Temperature controlled shipment']], True)
    table(c, 'invoices.csv', ['id', 'net_amount'], [['0007', '815.40']])
    c['expected_status'] = 'ambiguous'
    c['checks'].append(('has_reason', 'generic_identifier_without_corroboration'))

    c = case('finance_missing_is_not_agreement', 'finance')
    table(c, 'result.csv', ['transaction_id', 'memo'], [['TX-770', '']], True)
    table(c, 'ledger.csv', ['transaction_id', 'memo'], [['TX-770', '']])
    pair(c, 2, 'ledger.csv', 2)
    c['expected_status'] = 'located'
    c['checks'].append(('field_status', 2, 'memo', 'unresolved'))

    c = case('laboratory_case_sensitive_ids', 'laboratory')
    table(c, 'result.csv', ['sample_id', 'analyte'], [['AB-107', 'Potassium']], True)
    table(c, 'specimens.csv', ['sample_id', 'analyte'], [['ab-107', 'Potassium']])
    c['expected_status'] = 'not_found'

    c = case('manufacturing_missing_namespace_component', 'manufacturing')
    table(c, 'result.csv', ['tenant_id', 'work_order_id', 'quantity'], [['PLANT-A', 'WO-01', '42'], ['', 'WO-02', '17']], True)
    table(c, 'orders.csv', ['tenant_id', 'work_order_id', 'quantity'], [['PLANT-A', 'WO-01', '42'], ['PLANT-B', 'WO-02', '17']])
    pair(c, 2, 'orders.csv', 2)
    c['expected_status'] = 'partial'

    c = case('logistics_late_bridge_conflict', 'logistics')
    table(c, 'result.csv', ['shipment_id'], [['SHIP-701']], True)
    table(c, 'manifest.csv', ['shipment_id', 'container_id'], [['SHIP-701', 'CONT-A']])
    table(c, 'route.csv', ['container_id', 'port_id'], [['CONT-A', 'PORT-1']])
    table(c, 'ports.csv', ['port_id', 'port_name'], [['PORT-1', 'North terminal']])
    table(c, 'amended_manifest.csv', ['shipment_id', 'container_id'], [['SHIP-701', 'CONT-B']])
    pair(c, 2, 'manifest.csv', 2); pair(c, 2, 'amended_manifest.csv', 2)
    c['expected_status'] = 'ambiguous'
    c['checks'].append(('has_reason', 'conflicting_propagated_identifier'))

    c = case('finance_indistinguishable_columns', 'finance')
    table(c, 'result.csv', ['transaction_id', 'reported_amount'], [['TX-801', '32.70'], ['TX-802', '65.40']], True)
    table(c, 'ledger.csv', ['transaction_id', 'debit_amount', 'daily_budget'], [['TX-801', '32.70', '32.70'], ['TX-802', '65.40', '65.40']])
    pair(c, 2, 'ledger.csv', 2); pair(c, 3, 'ledger.csv', 3)
    c['expected_status'] = 'located'
    c['checks'].extend([('field_status', 2, 'reported_amount', 'candidate_ambiguous'), ('field_status', 3, 'reported_amount', 'candidate_ambiguous')])

    c = case('laboratory_complementary_missing_keys', 'laboratory')
    table(c, 'result.csv', ['run_id', 'sample_id', 'reading'], [['RUN-10', '', '5.81'], ['', 'SAM-20', '7.23']], True)
    table(c, 'measurements.csv', ['run_id', 'sample_id', 'reading'], [['RUN-10', 'SAM-10', '5.81'], ['RUN-20', 'SAM-20', '7.23']])
    pair(c, 2, 'measurements.csv', 2); pair(c, 3, 'measurements.csv', 3)
    c['expected_status'] = 'located'

    c = case('commerce_repeated_names_are_not_keys', 'ecommerce')
    table(c, 'result.csv', ['customer_name', 'gender', 'active'], [['Alex Morgan', 'F', '1']], True)
    table(c, 'accounts.csv', ['customer_name', 'gender', 'active'], [['Alex Morgan', 'F', '1'], ['Alex Morgan', 'F', '1']])
    c['expected_status'] = 'not_found'

    c = case('manufacturing_multisheet_namespaces', 'manufacturing')
    table(c, 'result.csv', ['work_order_id', 'supplier_id'], [['WO-900', 'SUP-71']], True)
    import openpyxl
    w = openpyxl.Workbook(); orders = w.active; orders.title = 'Orders'
    orders.append(['Production export']); orders.append(['Generated data']); orders.append(['external_key', 'quantity']); orders.append(['WO-900', 25])
    suppliers = w.create_sheet('Suppliers'); suppliers.append(['external_key', 'company']); suppliers.append(['SUP-71', 'Rotor Supply Co'])
    file = c['folder'] / 'production.xlsx'; w.save(file); c['sources'].append(file)
    c['config'] = {'sources': {'production.xlsx': {'sheets': {
        'Orders': {'header_row': 3, 'key_map': {'external_key': 'work_order_id'}},
        'Suppliers': {'header_row': 1, 'key_map': {'external_key': 'supplier_id'}}}}}}
    pair(c, 2, 'production.xlsx', 4, 'Orders'); pair(c, 2, 'production.xlsx', 2, 'Suppliers')
    c['expected_status'] = 'located'

    c = case('finance_static_sql_tuple_locations', 'finance')
    table(c, 'result.csv', ['journal_id', 'line_id', 'amount'], [['J-150', 'L-2', '14.25']], True)
    file = c['folder'] / 'journal.sql'
    file.write_text("CREATE TABLE entries (journal_id TEXT, line_id TEXT, amount DECIMAL);\nINSERT INTO entries VALUES ('J-150','L-1',8.50),('J-150','L-2',14.25);\n", encoding='utf-8')
    c['sources'].append(file)
    pair(c, 2, 'journal.sql', 2, 'entries', 2)
    c['expected_status'] = 'located'

    c = case('laboratory_literal_id_not_numeric_equivalence', 'laboratory')
    table(c, 'result.csv', ['sample_id', 'reading'], [['0012', '5.50']], True)
    table(c, 'samples.csv', ['sample_id', 'reading'], [['12', '5.50'], ['0012', '5.50']])
    pair(c, 2, 'samples.csv', 3)
    c['expected_status'] = 'located'

    c = case('manufacturing_explicit_origin_namespace', 'manufacturing')
    table(c, 'result.csv', ['work_order_id', 'supplier_id'], [['WO-440', 'SUP-88']], True)
    table(c, 'orders.csv', ['work_order_id', 'external_key'], [['WO-440', 'EXT-ORDER-A']])
    table(c, 'suppliers.csv', ['supplier_id', 'external_key'], [['SUP-88', 'EXT-SUPPLIER-B']])
    table(c, 'specifications.csv', ['external_key', 'tolerance_mm'], [['EXT-ORDER-A', '0.02'], ['EXT-SUPPLIER-B', '0.50']])
    c['config'] = {'sources': {'specifications.csv': {'key_map': {'external_key': {'field': 'external_key', 'file': 'orders.csv'}}}}}
    pair(c, 2, 'orders.csv', 2); pair(c, 2, 'suppliers.csv', 2); pair(c, 2, 'specifications.csv', 2)
    c['expected_status'] = 'located'
    c['checks'].append(('key_origin_file', 'specifications.csv', 'orders.csv'))

    c = case('laboratory_source_key_missing_alternative_available', 'laboratory')
    table(c, 'result.csv', ['run_id', 'sample_id', 'reading'], [['RUN-10', 'SAMPLE-10', '5.81']], True)
    table(c, 'measurements.csv', ['run_id', 'sample_id', 'reading'], [['', 'SAMPLE-10', '5.81']])
    pair(c, 2, 'measurements.csv', 2)
    c['expected_status'] = 'located'

    c = case('commerce_late_key_refines_ambiguous_lines', 'ecommerce')
    table(c, 'result.csv', ['order_ref'], [['ORD-711'], ['ORD-712']], True)
    table(c, 'lines.csv', ['order_ref', 'sku', 'unit_price'],
          [['ORD-711', 'SKU-A', '19.80'], ['ORD-711', 'SKU-B', '19.80'],
           ['ORD-712', 'SKU-A', '22.50'], ['ORD-712', 'SKU-C', '22.50']])
    table(c, 'selected_items.csv', ['order_ref', 'sku'], [['ORD-711', 'SKU-B'], ['ORD-712', 'SKU-C']])
    pair(c, 2, 'lines.csv', 3); pair(c, 3, 'lines.csv', 5)
    pair(c, 2, 'selected_items.csv', 2); pair(c, 3, 'selected_items.csv', 3)
    c['expected_status'] = 'located'

    c = case('commerce_generic_id_cannot_override_contradictory_fields', 'ecommerce')
    table(c, 'result.csv', ['order_ref', 'id', 'description', 'amount'], [['ORD-888', '0007', 'Selected item', '88.20']], True)
    table(c, 'lines.csv', ['order_ref', 'id', 'description', 'amount'],
          [['ORD-888', '0007', 'Unrelated item', '45.00'], ['ORD-888', '0008', 'Selected item', '88.20']])
    c['expected_status'] = 'ambiguous'

    return cases


def evaluate(case, report):
    predicted = []
    for result in report['result_rows']:
        for match in result['source_matches']:
            if match['status'] == 'located':
                predicted.append((result['row_num'], Path(match['file']).name,
                                  match.get('sheet'), match['row_num'], match.get('tuple_num')))
    expected = set(case['expected_pairs']); actual = set(predicted)
    failures = []
    if actual != expected:
        failures.append({'check': 'record_identity', 'false_positive': sorted(actual - expected, key=str),
                         'false_negative': sorted(expected - actual, key=str)})
    if report['source_localization']['status'] != case['expected_status']:
        failures.append({'check': 'localization_state', 'expected': case['expected_status'], 'actual': report['source_localization']['status']})
    text = json.dumps(report, ensure_ascii=False)
    for check in case['checks']:
        if check[0] == 'has_reason' and check[1] not in text:
            failures.append({'check': 'reason_present', 'expected': check[1]})
        elif check[0] == 'field_status':
            _, row, field, status = check
            item = next(m for m in report['field_mappings'] if m['result_row'] == row and m['result_field'] == field)
            if item['status'] != status:
                failures.append({'check': 'field_status', 'row': row, 'field': field, 'expected': status, 'actual': item['status']})
        elif check[0] == 'key_origin_file':
            _, target_file, origin_file = check
            locations = {m['source_ref']: Path(m['file']).name for r in report['result_rows'] for m in r['source_matches']}
            target = next(m for r in report['result_rows'] for m in r['source_matches'] if Path(m['file']).name == target_file)
            origins = {locations.get(o['origin'], o['origin']) for p in target.get('key_provenance', []) for o in p['origins']}
            if origins != {origin_file}:
                failures.append({'check': 'key_origin_file', 'expected': origin_file, 'actual': sorted(origins)})
    return {'case': case['name'], 'domain': case['domain'], 'passed': not failures,
            'expected_links': len(expected), 'predicted_links': len(actual),
            'true_positive': len(actual & expected), 'false_positive': len(actual - expected),
            'false_negative': len(expected - actual), 'failures': failures}
