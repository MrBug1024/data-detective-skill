"""Exact source locations, mixed-format fidelity and bounded streaming queries."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from parsers import FileParser, JSONParser, TextParser
from record_query import query_records
from ingestion_support import read_text
from indexer import build_index


class RecordQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source(self, name, content, encoding='utf-8'):
        path = self.root / name
        path.write_bytes(content.encode(encoding))
        return path

    def test_json_numeric_tokens_nested_pointers_and_no_grain_guess(self):
        path = self.source('data.json', '[{"id":"0007","amount":1.2300e2,"note":"规则：金额核验","x/y":{"~tag":true},"items":[{"id":"line1"}]}]')
        units = FileParser().parse(str(path))
        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit.get_field('id'), '0007')
        self.assertEqual(unit.get_field('amount'), '1.2300e2')
        self.assertEqual(unit.get_field('/x~1y/~0tag'), True)
        self.assertEqual(unit.metadata['field_pointers']['/x~1y/~0tag'], '/0/x~1y/~0tag')
        self.assertEqual(unit.metadata['value_types']['amount'], 'number')
        self.assertFalse(unit.metadata['raw_text_is_source_quote'])
        self.assertIsNone(unit.row_num)

    def test_jsonl_offsets_blank_lines_and_source_hash(self):
        text = '\ufeff{"id":"001","note":"中文"}\r\n\r\n{"id":"002","amount":12.3000}\r\n'
        path = self.source('data.jsonl', text)
        result = query_records(path, where={'id': '002'}, columns=['id', 'amount'])
        record = result['records'][0]
        self.assertEqual(record['row_num'], 3)
        self.assertEqual(record['values'], {'id': '002', 'amount': '12.3000'})
        self.assertTrue(path.read_bytes()[record['byte_offset']:record['byte_end']].startswith(b'{"id":"002"'))
        self.assertEqual(result['source_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result['coverage']['scanned_rows'], 2)

    def test_compound_tuples_do_not_create_cross_pairs(self):
        path = self.source('data.csv', 'id,day,amount\nA,1,10\nA,2,20\nB,1,30\nB,2,40\n')
        result = query_records(path, key_rows=[{'id': 'A', 'day': '1'}, {'id': 'B', 'day': '2'}], columns=['amount'])
        self.assertEqual([row['values']['amount'] for row in result['records']], ['10', '40'])
        self.assertEqual([row['row_num'] for row in result['records']], [2, 5])

    def test_csv_multiline_physical_locations_and_exact_empty(self):
        text = 'id,note\r\n0001,"line one\r\nline two"\r\n0002,\r\n'
        path = self.source('data.csv', text)
        result = query_records(path)
        self.assertEqual([row['row_num'] for row in result['records']], [2, 4])
        self.assertEqual(result['records'][0]['line_end'], 3)
        self.assertEqual(text[result['records'][1]['char_offset']:], '0002,\r\n')
        self.assertEqual(query_records(path, where={'note': ''})['coverage']['matched_rows'], 1)
        self.assertEqual(query_records(path, where={'note': None})['coverage']['matched_rows'], 0)

    def test_limit_retains_scan_totals_and_finds_invalid_trailer(self):
        path = self.source('data.ndjson', '{"id":"1"}\n{"id":"2"}\n{"id":"3"}\n')
        result = query_records(path, limit=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage']['matched_rows'], 3)
        self.assertTrue(result['coverage']['scan_complete'])
        path.write_text(path.read_text() + '{broken\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '第4行'):
            query_records(path, limit=1)

    def test_null_missing_and_unevaluated_filter_are_not_conflated(self):
        path = self.source('data.jsonl', '{"id":"1","x":null}\n{"id":"2"}\n')
        result = query_records(path, where={'x': None})
        self.assertEqual(result['coverage']['matched_rows'], 1)
        self.assertFalse(result['coverage']['filter_evaluation_complete'])
        self.assertEqual(result['status'], 'partial')
        projected = query_records(path, columns=['id', 'x'])
        self.assertIsNone(projected['records'][0]['values']['x'])
        self.assertEqual(projected['records'][1]['missing_fields'], ['x'])
        self.assertNotIn('x', projected['records'][1]['values'])

    def test_empty_csv_known_schema_is_evaluated(self):
        path = self.source('empty.csv', 'id,amount\n')
        result = query_records(path, where={'id': 'x'})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['coverage']['matched_rows'], 0)
        self.assertEqual(query_records(path, where={'typo': 'x'})['status'], 'partial')

    def test_invalid_json_is_rejected_without_silent_field_loss(self):
        for text in ('{"id":"1","id":"2"}', '{"amount":NaN}', '{"amount":Infinity}', '[1,2]'):
            with self.subTest(text=text):
                with self.assertRaises(RuntimeError):
                    FileParser().parse(str(self.source('bad.jsonl', text + '\n')))

    def test_budget_rejects_without_partial_result(self):
        path = self.source('data.jsonl', '{"id":"1","amount":2}\n{"id":"2","amount":3}\n')
        with self.assertRaisesRegex(ValueError, '返回预算'):
            query_records(path, max_return_cells=3)
        with self.assertRaisesRegex(ValueError, '单记录'):
            query_records(path, max_record_bytes=10)
        with mock.patch.object(JSONParser, 'MAX_CELLS', 1):
            with self.assertRaisesRegex(RuntimeError, '预算'):
                FileParser().parse(str(path))

    def test_huge_full_text_is_rejected_before_read(self):
        path = self.source('large.txt', 'abcdef')
        with mock.patch('ingestion_support.MAX_TEXT_INPUT_BYTES', 3):
            with self.assertRaisesRegex(ValueError, '预算'):
                read_text(path)

    def test_text_chunks_keep_exact_offsets_and_boundary_identifier(self):
        text = '开头\r\n' + '甲' * 7990 + 'CUSTOMER-00017' + '\r\n' + '乙' * 9000
        units = TextParser().parse(str(self.source('long.txt', text)))
        self.assertGreater(len(units), 1)
        self.assertTrue(all(len(unit.raw_text) <= 8000 for unit in units))
        self.assertTrue(any('CUSTOMER-00017' in unit.raw_text for unit in units))
        for unit in units:
            self.assertEqual(text[unit.char_offset:unit.char_offset + len(unit.raw_text)], unit.raw_text)
            self.assertEqual(unit.row_num, text[:unit.char_offset].count('\n') + 1)
        self.assertEqual(len({unit.unit_id for unit in units}), len(units))

    def test_snapshot_change_rejected(self):
        path = self.source('data.jsonl', '{"id":"1"}\n')
        with mock.patch('record_query.sha256_file', side_effect=['a' * 64, 'b' * 64]):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                query_records(path)

    def test_schema_cardinality_is_bounded_even_when_no_rows_match(self):
        path = self.source('drifting.jsonl', '{"a":1}\n{"b":2}\n{"c":3}\n')
        with mock.patch('record_query.MAX_SCHEMA_FIELDS', 2):
            with self.assertRaisesRegex(ValueError, '字段总数'):
                query_records(path, where={'id': 'not-present'})

    def test_stream_csv_rejects_short_rows_and_bad_encoding(self):
        short = self.source('short.csv', 'id,amount\n0001\n')
        with self.assertRaisesRegex(ValueError, '字段数'):
            query_records(short)
        gb = self.source('legacy.csv', 'id,note\n0001,中文\n', 'gb18030')
        with self.assertRaises(UnicodeDecodeError):
            query_records(gb)
        self.assertEqual(query_records(gb, encoding='gb18030')['records'][0]['values']['note'], '中文')

    def test_json_sources_participate_in_structured_index(self):
        path = self.source('data.jsonl', '{"customer_id":"0007","amount":10.25}\n')
        index = build_index([str(path)])
        self.assertEqual(index.lookup_exact('0007')[0].get_field('amount'), '10.25')

    def test_changed_source_does_not_create_a_stale_index_cache(self):
        path = self.source('data.jsonl', '{"id":"1"}\n')
        parse = FileParser.parse
        def changed(parser, filename):
            units = parse(parser, filename)
            path.write_text('{"id":"2"}\n', encoding='utf-8')
            return units
        cache = self.root / 'cache'
        with mock.patch.object(FileParser, 'parse', changed):
            with self.assertRaisesRegex(RuntimeError, '发生变化'):
                build_index([str(path)], str(cache))
        self.assertFalse(list(cache.glob('*.pkl')))

    def test_docx_hidden_revision_is_explicit_partial_coverage(self):
        from docx import Document
        from docx.oxml import OxmlElement
        doc = Document()
        paragraph = doc.add_paragraph('visible content')
        inserted = OxmlElement('w:ins')
        run, text = OxmlElement('w:r'), OxmlElement('w:t')
        text.text = 'inserted amount 123.45'
        run.append(text)
        inserted.append(run)
        paragraph._p.append(inserted)
        path = self.root / 'revisions.docx'
        doc.save(path)
        index = build_index([str(path)])
        status = next(iter(index.ingestion_status.values()))
        self.assertEqual(status['status'], 'partial')
        self.assertTrue(any(issue['code'] == 'docx_tracked_changes' for issue in status['issues']))

    def test_cli_failure_preserves_previous_output_and_reports_failed(self):
        source = self.source('bad.jsonl', '{"id":"x"}\ninvalid\n')
        destination = self.source('report.json', '{"previous":true}')
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'record_query.py'
        result = subprocess.run([sys.executable, str(script), '--file', str(source), '--out', str(destination)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2)
        self.assertIn('"status": "failed"', result.stderr)
        self.assertEqual(destination.read_text(), '{"previous":true}')


if __name__ == '__main__':
    unittest.main()
