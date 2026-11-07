"""Cross-format source citations and negative controls for lexical retrieval."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from evidence_retrieval import retrieve_evidence


def _target(**values):
    return {'rows': [{'row_num': 2, 'values': values}]}


def _text(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode('utf-8'))
    return path


def test_later_rule_header_and_exact_cell_quotes(tmp_path):
    from openpyxl import Workbook
    source = tmp_path / '规则表.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '业务规则'
    sheet.append(['附件一'])
    sheet.append(['费用复核规则清单'])
    sheet.append(['序号', '规则名称', '规则说明', '参考示例'])
    sheet.append([1, '仓储规则', '收取冷藏保管费用时，不得重复收取普通保管费用。', '例：冷藏保管和普通保管同时收费。'])
    sheet.append([2, '运输规则', '运输单据核对路线名称。', '路线改变须变更运输单据。'])
    workbook.save(source)
    profile = {'file': str(source), 'sheets': [{'sheet': '业务规则', 'header_row': 3, 'header_status': 'selected'}]}
    report = retrieve_evidence({'description': '收取冷藏保管费用时，重复收取普通保管费用',
                                'rows': [{'values': {'项目名称': '普通保管', '配对项目': '冷藏保管'}}]},
                               [source], profiles={str(source): profile})
    assert report['coverage']['scan_complete'] is True
    assert report['coverage']['matched_units'] == 1
    evidence = report['evidence'][0]
    assert evidence['kind'] == 'rule_candidate'
    assert evidence['source_location']['row'] == 4
    assert {quote['source_location']['cell'] for quote in evidence['quotes']} == {'C4', 'D4'}
    assert evidence['rule_applicability'] == 'not_verified'
    assert evidence['confidence'] is None


def test_shared_product_does_not_link_another_order(tmp_path):
    correct = _text(tmp_path, 'correct.md', 'Order ORD-202501 was delivered with a hydraulic pressure pump.')
    wrong = _text(tmp_path, 'different.md', 'Order ORD-202502 was delivered with a hydraulic pressure pump.')
    report = retrieve_evidence(_target(order_id='ORD-202501', product='hydraulic pressure pump'), [correct, wrong])
    hits = {Path(e['source_location']['file']).name: e for e in report['evidence']}
    assert hits['correct.md']['kind'] == 'identifier_mention'
    assert hits['different.md']['kind'] == 'text_candidate'
    assert hits['different.md']['entity_linkage'] == 'unresolved'
    assert all(e['verification'] == 'related_source_passage_not_verified_derivation' for e in hits.values())


def test_identifier_boundary_and_common_values_are_not_evidence(tmp_path):
    source = _text(tmp_path, 'notes.txt', 'Order ORD-1234567, female, active, 1, true; no target order here.')
    report = retrieve_evidence(_target(order_id='ORD-12345', sex='female', state='active', flag='1', yes='true'), [source])
    assert report['evidence'] == []


def test_text_quotes_retain_decoded_offsets_across_crlf_and_chunk_boundaries(tmp_path):
    content = '说明\r\n\r\n' + '填' * 8100 + '\r\n客户订单 ORD-000123 已确认。\r\n'
    source = _text(tmp_path, 'long.txt', content)
    report = retrieve_evidence(_target(order_id='ORD-000123'), [source])
    assert report['coverage']['scan_complete'] is True
    assert report['evidence']
    for evidence in report['evidence']:
        for quote in evidence['quotes']:
            loc = quote['source_location']
            assert content[loc['char_offset']:loc['char_end']] == quote['text']
            assert 'ORD-000123' in quote['text']
            assert loc['metadata']['offset_scope'] == 'decoded_source_text'


def test_same_content_is_not_independent_confirmation(tmp_path):
    first = _text(tmp_path, 'one.md', 'Order ORD-000321 is recorded here.')
    duplicate = _text(tmp_path, 'copy.md', first.read_text(encoding='utf-8'))
    report = retrieve_evidence(_target(order_id='ORD-000321'), [first, duplicate])
    assert len(report['evidence']) == 2
    assert len({e['duplicate_group'] for e in report['evidence']}) == 1
    assert len({e['dependency_group'] for e in report['evidence']}) == 1
    assert all(e['independence'] == 'unknown_same_content_not_independent' for e in report['evidence'])


def test_json_nested_value_has_pointer_not_invented_line(tmp_path):
    source = _text(tmp_path, 'event.json', json.dumps({'event': {'order_id': 'ORD-000456'}, 'description': 'shipment released'}))
    report = retrieve_evidence(_target(order_id='ORD-000456'), [source])
    evidence = report['evidence'][0]
    assert evidence['source_location']['row'] is None
    quote = evidence['quotes'][0]
    assert quote['source_location']['json_pointer'] == '/event/order_id'
    assert quote['kind'] == 'field_value'
    assert quote['text'] == 'ORD-000456'


def test_docx_paragraph_citation_has_ooxml_path_no_fake_page(tmp_path):
    from docx import Document
    source = tmp_path / 'memo.docx'
    doc = Document()
    doc.add_paragraph('Routine header')
    doc.add_paragraph('Order ORD-000789 has been reviewed.')
    doc.save(source)
    report = retrieve_evidence(_target(order_id='ORD-000789'), [source])
    location = report['evidence'][0]['source_location']
    assert location['paragraph'] == 2
    assert location['page'] is None
    assert location['metadata']['xml_path'].endswith('/p[2]')


def test_missing_and_unsupported_sources_do_not_become_no_match_success(tmp_path):
    missing = tmp_path / 'missing.txt'
    unsupported = _text(tmp_path, 'binary.weird', 'ORD-000123')
    report = retrieve_evidence(_target(order_id='ORD-000123'), [missing, unsupported])
    assert report['coverage']['scan_complete'] is False
    assert not report['evidence']
    assert any(i['code'] == 'unsupported_format' for i in report['issues'])
    assert any(i['code'] == 'evidence_source_unavailable' for i in report['issues'])


def test_output_limit_reports_remaining_candidates_without_short_circuiting_scan(tmp_path):
    source = _text(tmp_path, 'notes.md', '\n\n'.join(f'Order ORD-000123 event {i}.' for i in range(5)))
    report = retrieve_evidence(_target(order_id='ORD-000123'), [source], limit=2)
    assert report['coverage']['scan_complete'] is True
    assert report['coverage']['matched_units'] == 5
    assert report['coverage']['returned_units'] == 2
    assert report['coverage']['truncated'] is True


def test_stale_supplied_query_is_rejected(tmp_path):
    from record_query import query_records
    source = _text(tmp_path, 'rules.csv', 'rule,description\nR001,Order ORD-000123 must be checked\n')
    query = query_records(source)
    source.write_text('rule,description\nR002,Other order\n', encoding='utf-8')
    report = retrieve_evidence(_target(order_id='ORD-000123'), [source], tables={str(source): query})
    assert report['coverage']['scan_complete'] is False
    assert not report['evidence']
    assert 'hash' in report['issues'][0]['message']


@pytest.mark.parametrize('extension', ['csv', 'jsonl'])
def test_text_tables_retain_source_file_and_physical_record_location(tmp_path, extension):
    content = 'rule,description\nR001,Order ORD-000123 must be checked\n' if extension == 'csv' else (
        '{"rule":"R001","description":"Order ORD-000123 must be checked"}\n')
    source = _text(tmp_path, 'policy.' + extension, content)
    report = retrieve_evidence(_target(order_id='ORD-000123'), [source])
    assert report['coverage']['scan_complete'] is True
    assert report['evidence'][0]['source_location']['file'] == str(source)
    assert report['evidence'][0]['source_location']['row'] == (2 if extension == 'csv' else 1)


def test_extraction_diagnostics_remain_partial_with_retrieved_evidence(tmp_path):
    source = _text(tmp_path, 'notes.md', 'Order ORD-000123 is recorded.')
    from parsers import FileParser
    original = FileParser.parse
    def parse_with_gap(self, file):
        result = original(self, file)
        self.last_diagnostics = [{'code': 'uncovered_region', 'severity': 'warning', 'message': 'Test extraction gap'}]
        return result
    with patch.object(FileParser, 'parse', parse_with_gap):
        report = retrieve_evidence(_target(order_id='ORD-000123'), [source])
    assert report['evidence']
    assert report['coverage']['scan_complete'] is False


def test_term_budget_is_explicit_partial(tmp_path):
    source = _text(tmp_path, 'notes.md', 'Orders ORD-000123 and ORD-000456.')
    report = retrieve_evidence({'rows': [{'values': {'order_id': v}} for v in ('ORD-000123', 'ORD-000456')]},
                               [source], max_terms=1)
    assert report['coverage']['query_terms_truncated'] is True
    assert report['coverage']['scan_complete'] is False


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'gb18030'])
def test_streaming_text_beyond_materialization_budget_retains_offsets(tmp_path, encoding):
    content = '客户记录\r\n' + '填' * 15900 + '订单 ORD-889900 已出库。'
    source = tmp_path / 'large.txt'
    source.write_bytes(content.encode(encoding))
    report = retrieve_evidence(_target(order_id='ORD-889900'), [source], max_source_bytes=1)
    assert report['coverage']['scan_complete'] is True
    assert report['evidence']
    for evidence in report['evidence']:
        for quote in evidence['quotes']:
            location = quote['source_location']
            assert content[location['char_offset']:location['char_end']] == quote['text']
            assert 'ORD-889900' in quote['text']
            assert location['metadata']['streaming_chunk'] > 1


def test_candidate_memory_budget_is_explicit_and_bounded(tmp_path):
    source = _text(tmp_path, 'notes.md', '\n\n'.join('Order ORD-123456 event ' + str(i) for i in range(20)))
    report = retrieve_evidence(_target(order_id='ORD-123456'), [source], max_candidate_chars=60)
    assert report['coverage']['scan_complete'] is False
    assert 0 < len(report['evidence']) <= 2
    assert any(i['code'] == 'evidence_candidate_budget' for i in report['issues'])


def test_pdf_quote_has_page_and_extracted_text_location(tmp_path):
    # Minimal real text PDF fixture; no external renderer or remote resource.
    stream = b'BT /F1 12 Tf 72 720 Td (Order ORD-990011 shipped.) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    pdf = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, content in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f'{number} 0 obj\n'.encode() + content + b'\nendobj\n')
    xref = len(pdf)
    pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        pdf.extend(f'{offset:010d} 00000 n \n'.encode())
    pdf.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    source = tmp_path / 'shipment.pdf'
    source.write_bytes(pdf)
    report = retrieve_evidence(_target(order_id='ORD-990011'), [source])
    assert report['coverage']['scan_complete'] is True
    quote = report['evidence'][0]['quotes'][0]
    assert 'ORD-990011' in quote['text']
    assert quote['source_location']['page'] == 1
    assert quote['source_location']['metadata']['offset_scope'] == 'page_extracted_text'

