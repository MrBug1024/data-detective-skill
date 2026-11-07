"""Behavioral checks for bounded, exact XLSX extraction without a global index."""
import hashlib
import json
import os
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock
from xml.sax.saxutils import escape
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import xlsx_query
from xlsx_query import query_xlsx, inspect_xlsx

M = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def text_cell(address, value):
    return f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def number_cell(address, value, style=0, formula=None):
    f = '' if formula is None else '<f>' + escape(formula) + '</f>'
    v = '' if value is None else '<v>' + str(value) + '</v>'
    return f'<c r="{address}" s="{style}">{f}{v}</c>'


def row(number, *cells):
    return f'<row r="{number}">' + ''.join(cells) + '</row>'


class StreamingQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def workbook(self, rows, strings=None, extra_sheet=False, epoch_1904=False):
        path = self.root / 'source.xlsx'
        with ZipFile(path, 'w') as archive:
            props = '<workbookPr date1904="1"/>' if epoch_1904 else ''
            extra = '<sheet name="Other" sheetId="2" r:id="rId2"/>' if extra_sheet else ''
            archive.writestr('xl/workbook.xml', f'<workbook xmlns="{M}" xmlns:r="{R}">{props}<sheets><sheet name="Records" sheetId="1" r:id="rId1"/>{extra}</sheets></workbook>')
            extra_rel = '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>' if extra_sheet else ''
            archive.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/>{extra_rel}</Relationships>')
            sheet = f'<worksheet xmlns="{M}"><sheetData>' + ''.join(rows) + '</sheetData></worksheet>'
            archive.writestr('xl/worksheets/sheet1.xml', sheet)
            if extra_sheet:
                archive.writestr('xl/worksheets/sheet2.xml', sheet)
            archive.writestr('xl/styles.xml', f'<styleSheet xmlns="{M}"><numFmts count="2"><numFmt numFmtId="164" formatCode="0000"/><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="165"/></cellXfs></styleSheet>')
            if strings is not None:
                archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="{M}">' + strings + '</sst>')
        return path

    def simple(self):
        return self.workbook([
            row(1, text_cell('A1', 'ID'), text_cell('B1', 'Amount'), text_cell('C1', 'Type')),
            row(2, text_cell('A2', '0007'), number_cell('B2', '123.450000000000001'), text_cell('C2', 'fee')),
            row(8, text_cell('A8', '7'), number_cell('B8', '9.00'), text_cell('C8', 'fee')),
            row(12, text_cell('A12', '0007'), number_cell('B12', '-2.50'), text_cell('C12', 'refund')),
        ])

    def test_exact_projection_retains_decimal_bytes_and_physical_cells(self):
        path = self.simple()
        before = path.read_bytes()
        result = query_xlsx(path, where={'ID': ['0007'], 'Type': ['fee']}, columns=['Amount', 'ID'])
        self.assertEqual(result['coverage']['scanned_rows'], 3)
        self.assertEqual(result['coverage']['matched_rows'], 1)
        record = result['rows'][0]
        self.assertEqual(record['row_num'], 2)
        self.assertEqual(record['values'], {'Amount': '123.450000000000001', 'ID': '0007'})
        self.assertEqual(record['cells']['Amount']['address'], 'B2')
        self.assertEqual(record['cells']['Amount']['raw_value'], '123.450000000000001')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result['source_sha256'], hashlib.sha256(before).hexdigest())

    def test_return_limit_does_not_stop_complete_scan(self):
        result = query_xlsx(self.simple(), where={'ID': ['0007', '7']}, limit=1)
        self.assertEqual(result['coverage']['scanned_rows'], 3)
        self.assertEqual(result['coverage']['matched_rows'], 3)
        self.assertEqual(result['coverage']['returned_rows'], 1)
        self.assertTrue(result['coverage']['scan_complete'])
        self.assertTrue(result['coverage']['truncated'])
        self.assertEqual(len(query_xlsx(self.simple(), limit=0)['rows']), 0)

    def test_explicit_header_after_title_preserves_original_addresses(self):
        path = self.workbook([
            row(1, text_cell('A1', 'Attachment title')),
            row(3, text_cell('A3', 'Rule'), text_cell('B3', 'Description')),
            row(7, text_cell('A7', 'R1'), text_cell('B7', 'same-day charges')),
        ])
        result = query_xlsx(path, header_row=3, where={'Rule': ['R1']})
        self.assertEqual(result['rows'][0]['cells']['Description']['address'], 'B7')
        self.assertEqual(result['coverage']['scanned_rows'], 1)
        with self.assertRaisesRegex(ValueError, 'without a header'):
            query_xlsx(path)

    def test_date_epoch_and_displayed_zero_identifier_keep_raw_value(self):
        path = self.workbook([
            row(1, text_cell('A1', 'ID'), text_cell('B1', 'Date')),
            row(2, number_cell('A2', '7', 1), number_cell('B2', '1', 2)),
        ], epoch_1904=True)
        record = query_xlsx(path, where={'ID': ['0007']})['rows'][0]
        self.assertEqual(record['values']['ID'], '0007')
        self.assertEqual(record['cells']['ID']['raw_value'], '7')
        self.assertEqual(record['cells']['Date']['value'], '1')
        self.assertTrue(record['cells']['Date']['iso_value'].startswith('1904-01-02'))
        self.assertEqual(record['cells']['Date']['excel_epoch'], '1904')

    def test_uncached_formula_in_filter_is_unavailable_not_a_null_match(self):
        path = self.workbook([
            row(1, text_cell('A1', 'ID'), text_cell('B1', 'Amount')),
            row(2, text_cell('A2', 'x'), number_cell('B2', None, formula='1+1')),
            row(3, text_cell('A3', 'y'), number_cell('B3', '2', formula='1+1')),
        ])
        whole = query_xlsx(path)
        self.assertEqual(whole['rows'][0]['cells']['Amount']['formula'], '=1+1')
        self.assertFalse(whole['rows'][0]['cells']['Amount']['formula_cached'])
        self.assertEqual(whole['rows'][1]['values']['Amount'], '2')
        filtered = query_xlsx(path, where={'Amount': [None]})
        self.assertFalse(filtered['coverage']['filter_evaluation_complete'])
        self.assertEqual(filtered['coverage']['unavailable_filter_rows'], 1)
        self.assertEqual(filtered['coverage']['matched_rows'], 0)

    def test_rich_shared_text_excludes_phonetic_annotations(self):
        strings = '<si><t>ID</t></si><si><r><t>00</t></r><r><t>07</t></r><rPh><t>phonetic</t></rPh></si>'
        path = self.workbook([
            row(1, '<c r="A1" t="s"><v>0</v></c>'),
            row(2, '<c r="A2" t="s"><v>1</v></c>'),
        ], strings=strings)
        self.assertEqual(query_xlsx(path, where={'ID': ['0007']})['rows'][0]['values']['ID'], '0007')

    def test_multiple_sheets_require_explicit_selection(self):
        path = self.workbook([row(1, text_cell('A1', 'ID')), row(2, text_cell('A2', 'x'))], extra_sheet=True)
        with self.assertRaisesRegex(ValueError, 'Multiple worksheets'):
            query_xlsx(path)
        self.assertEqual(query_xlsx(path, sheet='Other')['rows'][0]['values']['ID'], 'x')

    def test_shared_underscore_escape_preserves_literal_key_without_recursive_decoding(self):
        path = self.workbook([
            row(1, '<c r="A1" t="s"><v>0</v></c>'),
            row(2, '<c r="A2" t="s"><v>1</v></c>'),
        ], strings='<si><t>ID</t></si><si><t>ABC_x005F_x0041_</t></si>')
        for reader in ('fast', 'compatible'):
            result = query_xlsx(path, where={'ID': ['ABC_x0041_']}, reader=reader)
            self.assertEqual(result['coverage']['matched_rows'], 1)
            self.assertEqual(result['rows'][0]['values']['ID'], 'ABC_x0041_')
            self.assertEqual(result['rows'][0]['cells']['ID']['raw_value'], '1')
            self.assertEqual(query_xlsx(path, where={'ID': ['ABCA']}, reader=reader)['rows'], [])
        sample = inspect_xlsx(path, header_rows=2)
        self.assertEqual(sample['sheets'][0]['rows'][1]['values']['A'], 'ABC_x0041_')

    def test_duplicate_empty_and_unlabelled_headers_fail(self):
        for rows, error in [
            ([row(1, text_cell('A1', 'ID'), text_cell('B1', 'ID'))], 'Duplicate header'),
            ([row(1, text_cell('A1', ''))], 'Empty header'),
            ([row(1, text_cell('A1', 'ID')), row(2, text_cell('B2', 'x'))], 'without a header'),
        ]:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    query_xlsx(self.workbook(rows))

    def test_invalid_shared_index_cannot_select_from_the_end(self):
        path = self.workbook([row(1, text_cell('A1', 'ID')), row(2, '<c r="A2" t="s"><v>-1</v></c>')], strings='<si><t>last string</t></si>')
        with self.assertRaisesRegex(ValueError, 'Invalid shared-string'):
            query_xlsx(path)

    def test_stdlib_reader_has_the_same_query_result(self):
        path = self.simple()
        normal = query_xlsx(path, where={'ID': ['0007']})
        with mock.patch.object(xlsx_query, '_fast_xml', None):
            fallback = query_xlsx(path, where={'ID': ['0007']})
        self.assertEqual(normal['rows'], fallback['rows'])
        self.assertEqual(normal['coverage'], fallback['coverage'])

    def test_invalid_query_does_not_become_unfiltered_scan(self):
        path = self.simple()
        for where in ([], {'ID': []}, {'ID': '0007'}, {'ID': [{'nested': 'x'}]}, {'Unknown': ['x']}):
            with self.subTest(where=where):
                with self.assertRaises(ValueError):
                    query_xlsx(path, where=where)

    def test_cli_jsonl_has_metadata_and_source_cell_rows(self):
        source = self.simple()
        filters = self.root / 'filters.json'
        filters.write_text(json.dumps({'ID': ['0007']}), encoding='utf-8')
        output = self.root / 'extracted.jsonl'
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/xlsx_query.py'), '--file', str(source), '--where-json', str(filters), '--columns', 'ID', '--out', str(output)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        lines = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(lines[0]['metadata']['coverage']['matched_rows'], 2)
        self.assertEqual([line['row_num'] for line in lines[1:]], [2, 12])
        self.assertEqual(lines[2]['cells']['ID']['address'], 'A12')

    def test_inspect_only_samples_requested_physical_prefix_and_needed_strings(self):
        path = self.workbook([
            row(1, '<c r="A1" t="s"><v>0</v></c>'),
            row(2, '<c r="A2" t="s"><v>1</v></c>'),
            # A later cell is deliberately invalid: inspection must not claim
            # that it validated the rest of the sheet, or visit this cell.
            row(50, '<c r="A50" t="s"><v>9999</v></c>'),
        ], strings='<si><t>ID</t></si><si><t>0007</t></si>' + '<si><t>unused</t></si>' * 1000)
        result = inspect_xlsx(path, header_rows=1)
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['coverage']['shared_strings_retained'], 1)
        self.assertEqual(result['sheets'][0]['rows'][0]['values']['A'], 'ID')
        self.assertEqual(len(result['sheets'][0]['rows']), 1)
        with self.assertRaisesRegex(ValueError, 'Invalid shared-string'):
            query_xlsx(path)

    def test_inspect_cli_succeeds_without_printing_sample_values(self):
        source = self.simple()
        output = self.root / 'structure.json'
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/xlsx_query.py'), '--file', str(source), '--inspect', '--header-rows', '1', '--out', str(output)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn('0007', run.stdout)
        metadata = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(metadata['operation'], 'inspect')
        self.assertEqual(metadata['sheets'][0]['rows'][0]['values']['A'], 'ID')

    def shared_fixture(self):
        return self.workbook([
            row(1, '<c r="A1" t="s"><v>0</v></c>', text_cell('B1', 'Amount')),
            row(2, '<c r="A2" t="s"><v>1</v></c>', number_cell('B2', '10.00')),
            row(7, '<c r="A7" t="s"><v>2</v></c>', number_cell('B7', '20.00')),
        ], strings='<si><t>ID</t></si><si><t>skip</t></si><si><t>keep</t></si>')

    def rewrite_sheet(self, path, transform):
        with ZipFile(path) as archive:
            entries = [(entry, archive.read(entry.filename)) for entry in archive.infolist()]
        with ZipFile(path, 'w') as archive:
            for info, content in entries:
                if info.filename == 'xl/worksheets/sheet1.xml':
                    content = transform(content)
                archive.writestr(info, content)

    def test_fast_filter_matches_compatible_rows_cells_issues_and_coverage(self):
        source = self.shared_fixture()
        fast = query_xlsx(source, where={'ID': ['keep']}, reader='fast')
        compatible = query_xlsx(source, where={'ID': ['keep']}, reader='compatible')
        self.assertEqual(fast['reader'], 'validated_row_frames')
        self.assertEqual(fast['rows'], compatible['rows'])
        self.assertEqual(fast['issues'], compatible['issues'])
        self.assertEqual(fast['coverage'], compatible['coverage'])
        self.assertEqual(fast['coverage']['scanned_rows'], 2)
        self.assertEqual([record['row_num'] for record in fast['rows']], [7])
        missing = query_xlsx(source, where={'ID': ['absent']}, reader='fast')
        self.assertEqual(missing['coverage']['scanned_rows'], 2)
        self.assertEqual(missing['coverage']['matched_rows'], 0)
        self.assertTrue(missing['coverage']['scan_complete'])

    def test_content_cache_warm_call_does_not_open_or_parse_workbook(self):
        source = self.shared_fixture()
        cache = self.root / 'cache'
        cold = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache)
        self.assertFalse(cold['cache']['hit'])
        with mock.patch.object(xlsx_query, '_query_xlsx_core', side_effect=AssertionError('warm cache must not parse XML')):
            warm = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache)
        self.assertTrue(warm['cache']['hit'])
        self.assertEqual(warm['rows'], cold['rows'])
        self.assertEqual(warm['source_sha256'], cold['source_sha256'])
        self.assertEqual(warm['cold_scan_elapsed_seconds'], cold['cold_scan_elapsed_seconds'])
        self.assertEqual(len(list(cache.glob('*.json'))), 1)
        self.assertFalse(list(cache.glob('*.tmp')))

    def test_cache_invalidates_changed_content_with_unchanged_size_and_mtime(self):
        source = self.shared_fixture()
        cache = self.root / 'cache'
        cold = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache)
        stat = source.stat()
        self.rewrite_sheet(source, lambda content: content.replace(b'<v>2</v>', b'<v>1</v>'))
        self.assertEqual(source.stat().st_size, stat.st_size)
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        changed = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache)
        self.assertFalse(changed['cache']['hit'])
        self.assertNotEqual(changed['source_sha256'], cold['source_sha256'])
        self.assertEqual(changed['coverage']['matched_rows'], 0)

    def test_reordered_and_repeated_or_keys_reuse_the_same_source_query(self):
        source = self.shared_fixture()
        cache = self.root / 'cache'
        cold = query_xlsx(source, where={'ID': ['keep', 'skip', 'keep']}, cache_dir=cache)
        with mock.patch.object(xlsx_query, '_query_xlsx_core', side_effect=AssertionError('equivalent OR keys must reuse cache')):
            warm = query_xlsx(source, where={'ID': ['skip', 'keep']}, cache_dir=cache)
        self.assertTrue(warm['cache']['hit'])
        self.assertEqual(cold['rows'], warm['rows'])
        self.assertEqual(cold['cache']['key'], warm['cache']['key'])
        self.assertEqual(warm['where'], {'ID': ['skip', 'keep']})

    def test_cache_source_path_and_projection_are_part_of_identity(self):
        source = self.shared_fixture()
        other = self.root / 'other.xlsx'
        other.write_bytes(source.read_bytes())
        cache = self.root / 'cache'
        one = query_xlsx(source, where={'ID': ['keep']}, columns=['ID'], cache_dir=cache)
        two = query_xlsx(other, where={'ID': ['keep']}, columns=['Amount'], cache_dir=cache)
        self.assertFalse(two['cache']['hit'])
        self.assertNotEqual(one['cache']['key'], two['cache']['key'])
        self.assertEqual(two['file'], str(other.resolve()))
        self.assertEqual(two['rows'][0]['values'], {'Amount': '20.00'})

    def test_prefixed_elements_fall_back_without_changing_evidence(self):
        source = self.shared_fixture()
        def prefixed(content):
            content = content.replace(f'xmlns="{M}"'.encode(), f'xmlns:m="{M}"'.encode())
            for tag in ('worksheet', 'sheetData', 'row', 'c', 'v', 'is', 't'):
                content = content.replace(('<' + tag).encode(), ('<m:' + tag).encode())
                content = content.replace(('</' + tag).encode(), ('</m:' + tag).encode())
            return content
        self.rewrite_sheet(source, prefixed)
        auto = query_xlsx(source, where={'ID': ['keep']})
        compatible = query_xlsx(source, where={'ID': ['keep']}, reader='compatible')
        self.assertEqual(auto['reader'], 'compatible_xml')
        self.assertIn('reader_fallback_reason', auto)
        self.assertEqual(auto['rows'], compatible['rows'])
        self.assertEqual(auto['coverage'], compatible['coverage'])

    def test_single_quotes_self_closing_and_entity_encoded_shared_index(self):
        source = self.shared_fixture()
        def rewrite(content):
            content = content.replace(b'r="A7" t="s"><v>2</v>', b"t='s' r='A7'><v>&#50;</v>")
            content = content.replace(b'<c r="B7" s="0"><v>20.00</v></c>', b"<c r='B7' s='0'/>")
            content = content.replace(b'r="A2" t="s"', b"t='s' r='A2'")
            return content
        self.rewrite_sheet(source, rewrite)
        fast = query_xlsx(source, where={'ID': ['keep']}, reader='fast')
        compatible = query_xlsx(source, where={'ID': ['keep']}, reader='compatible')
        self.assertEqual(fast['rows'], compatible['rows'])
        self.assertEqual(fast['rows'][0]['row_num'], 7)
        self.assertIsNone(fast['rows'][0]['values']['Amount'])

    def test_dtd_and_invalid_unmatched_xml_cannot_be_cached_as_complete(self):
        for mode in ('auto', 'compatible'):
            source = self.shared_fixture()
            self.rewrite_sheet(source, lambda data: b'<!DOCTYPE worksheet [<!ENTITY e "keep">]>' + data)
            cache = self.root / ('cache-' + mode)
            with self.assertRaisesRegex(ValueError, 'DTD'):
                query_xlsx(source, where={'ID': ['absent']}, reader=mode, cache_dir=cache)
            self.assertFalse(cache.exists())
        source = self.shared_fixture()
        self.rewrite_sheet(source, lambda data: data.replace(b'</sheetData>', b'<bad></sheetData>'))
        with self.assertRaises(ValueError):
            query_xlsx(source, where={'ID': ['absent']}, reader='fast')

    def test_duplicate_filter_address_is_not_hidden_by_first_nonmatch(self):
        for address in (b'A2', b'&#65;2'):
            for mode in ('fast', 'compatible'):
                source = self.shared_fixture()
                self.rewrite_sheet(source, lambda data: data.replace(
                    b'<c r="A2" t="s"><v>1</v></c>',
                    b'<c r="A2" t="s"><v>1</v></c><c r="' + address + b'" t="s"><v>2</v></c>'))
                with self.subTest(address=address, mode=mode):
                    with self.assertRaisesRegex(ValueError, 'Duplicate cell address'):
                        query_xlsx(source, where={'ID': ['keep']}, reader=mode)

    def test_tail_extensions_are_validated_without_a_late_compatible_rescan(self):
        source = self.shared_fixture()
        tail = b'<!-- harmless tail --><extLst><x:ext xmlns:x="urn:extension">metadata</x:ext></extLst>'
        self.rewrite_sheet(source, lambda data: data.replace(b'</worksheet>', tail + b'</worksheet>'))
        fast = query_xlsx(source, where={'ID': ['keep']}, reader='auto')
        compatible = query_xlsx(source, where={'ID': ['keep']}, reader='compatible')
        self.assertEqual(fast['reader'], 'validated_row_frames')
        self.assertNotIn('reader_fallback_reason', fast)
        self.assertEqual(fast['rows'], compatible['rows'])

    def test_row_frames_cross_arbitrary_stream_chunk_boundaries(self):
        content = (f'<worksheet xmlns="{M}"><sheetData>' + row(1, text_cell('A1', 'ID'))
                   + row(2, text_cell('A2', 'keep')) + '</sheetData></worksheet>').encode()
        class TinyChunks(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 17))
        frames = list(xlsx_query._framed_rows(TinyChunks(content)))
        self.assertEqual([number for number, _, _ in frames], [1, 2])
        self.assertEqual(xlsx_query._parse_frame(frames[1][1], frames[1][2]).attrib['r'], '2')

    def compound_fixture(self):
        return self.workbook([
            row(1, text_cell('A1', 'tenant'), text_cell('B1', 'sku'), text_cell('C1', 'status'), text_cell('D1', 'amount')),
            row(2, text_cell('A2', 'A'), text_cell('B2', 'X'), text_cell('C2', 'open'), number_cell('D2', '10.00')),
            row(3, text_cell('A3', 'A'), text_cell('B3', 'Y'), text_cell('C3', 'open'), number_cell('D3', '20.00')),
            row(4, text_cell('A4', 'B'), text_cell('B4', 'X'), text_cell('C4', 'open'), number_cell('D4', '30.00')),
            row(5, text_cell('A5', 'B'), text_cell('B5', 'Y'), text_cell('C5', 'closed'), number_cell('D5', '40.00')),
        ])

    def test_complete_compound_keys_exclude_cross_pairs_and_combine_with_where(self):
        source = self.compound_fixture()
        keys = [{'tenant': 'A', 'sku': 'X'}, {'tenant': 'B', 'sku': 'Y'}]
        fast = query_xlsx(source, key_rows=keys, columns=['amount'], reader='fast')
        compatible = query_xlsx(source, key_rows=keys, columns=['amount'], reader='compatible')
        self.assertEqual(fast['rows'], compatible['rows'])
        self.assertEqual(fast['coverage'], compatible['coverage'])
        self.assertEqual([record['row_num'] for record in fast['rows']], [2, 5])
        self.assertEqual([record['values']['amount'] for record in fast['rows']], ['10.00', '40.00'])
        constrained = query_xlsx(source, key_rows=keys, where={'status': ['open']}, columns=['amount'])
        self.assertEqual([record['row_num'] for record in constrained['rows']], [2])
        # A same-named where field is an additional AND condition.
        same_field = query_xlsx(source, key_rows=keys, where={'tenant': ['B']})
        self.assertEqual([record['row_num'] for record in same_field['rows']], [5])

    def test_compound_key_order_duplicates_and_field_order_share_cache(self):
        source = self.compound_fixture()
        cache = self.root / 'cache'
        keys = [{'tenant': 'A', 'sku': 'X'}, {'tenant': 'B', 'sku': 'Y'}]
        cold = query_xlsx(source, key_rows=keys, cache_dir=cache)
        with mock.patch.object(xlsx_query, '_query_xlsx_core', side_effect=AssertionError('tuple reorder must reuse cache')):
            warm = query_xlsx(source, key_rows=[{'sku': 'Y', 'tenant': 'B'}, {'sku': 'X', 'tenant': 'A'}, keys[0]], cache_dir=cache)
        self.assertTrue(warm['cache']['hit'])
        self.assertEqual(warm['cache']['key'], cold['cache']['key'])
        self.assertEqual(warm['rows'], cold['rows'])
        ordinary = query_xlsx(source, where={'tenant': ['A']}, cache_dir=cache)
        with mock.patch.object(xlsx_query, '_query_xlsx_core', side_effect=AssertionError('None must not change ordinary signature')):
            ordinary_again = query_xlsx(source, where={'tenant': ['A']}, key_rows=None, cache_dir=cache)
        self.assertTrue(ordinary_again['cache']['hit'])
        self.assertEqual(ordinary_again['cache']['key'], ordinary['cache']['key'])

    def test_compound_keys_validate_shape_and_fields(self):
        source = self.compound_fixture()
        invalid = ([], {}, {'tenant': 'A'}, [None], [{}], [{'tenant': 'A'}, {'sku': 'X'}], [{'tenant': ['A']}])
        for keys in invalid:
            with self.subTest(keys=keys):
                with self.assertRaisesRegex(ValueError, 'key_rows'):
                    query_xlsx(source, key_rows=keys)
        with self.assertRaisesRegex(ValueError, 'Unknown column'):
            query_xlsx(source, key_rows=[{'unknown': 'A'}])

    def test_compound_key_cli_takes_a_json_array(self):
        source = self.compound_fixture()
        keys = self.root / 'keys.json'
        keys.write_text(json.dumps([{'tenant': 'A', 'sku': 'X'}]), encoding='utf-8')
        output = self.root / 'tuple-result.json'
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/xlsx_query.py'), '--file', str(source), '--keys-json', str(keys), '--columns', 'amount', '--no-cache', '--out', str(output)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual([record['row_num'] for record in result['rows']], [2])

    def test_shared_formula_attributes_and_unresolved_expression_remain_visible(self):
        source = self.workbook([
            row(1, text_cell('A1', 'ID'), text_cell('B1', 'Total')),
            row(2, text_cell('A2', 'root'), '<c r="B2"><f t="shared" ref="B2:B4" si="0">A2*2</f><v>20</v></c>'),
            row(3, text_cell('A3', 'cached'), '<c r="B3"><f t="shared" si="0"/><v>40</v></c>'),
            row(4, text_cell('A4', 'missing'), '<c r="B4"><f t="shared" si="0"/></c>'),
        ])
        fast = query_xlsx(source, reader='fast')
        compatible = query_xlsx(source, reader='compatible')
        self.assertEqual(fast['rows'], compatible['rows'])
        self.assertEqual(fast['coverage'], compatible['coverage'])
        root, cached, missing = [record['cells']['Total'] for record in fast['rows']]
        self.assertEqual(root['formula'], '=A2*2')
        self.assertEqual(root['formula_attributes'], {'t': 'shared', 'ref': 'B2:B4', 'si': '0'})
        self.assertTrue(root['formula_expression_complete'])
        self.assertTrue(cached['formula_present'])
        self.assertIsNone(cached['formula'])
        self.assertEqual(cached['formula_state'], 'shared_formula_unresolved')
        self.assertFalse(cached['formula_expression_complete'])
        self.assertEqual(cached['value'], '40')
        self.assertTrue(cached['formula_cached'])
        self.assertFalse(missing['formula_cached'])
        self.assertFalse(fast['coverage']['expression_provenance_complete'])
        self.assertEqual(fast['coverage']['unresolved_formula_expressions'], 2)
        self.assertEqual(fast['coverage']['issue_counts']['formula_missing_cache'], 1)
        unknown = query_xlsx(source, where={'Total': [None]})
        self.assertEqual(unknown['coverage']['matched_rows'], 0)
        self.assertFalse(unknown['coverage']['filter_evaluation_complete'])
        self.assertEqual(unknown['coverage']['unavailable_filter_rows'], 1)
        tuple_unknown = query_xlsx(source, key_rows=[{'ID': 'missing', 'Total': None}])
        self.assertEqual(tuple_unknown['coverage']['matched_rows'], 0)
        self.assertFalse(tuple_unknown['coverage']['filter_evaluation_complete'])

    def test_typed_filter_fast_path_matches_numeric_inline_zero_formats_and_blank_cells(self):
        source = self.workbook([
            row(1, text_cell('A1', 'ID'), text_cell('B1', 'Amount')),
            row(2, number_cell('A2', '7', 1), number_cell('B2', '12.3000000000001')),
            row(3, '<c r="A3" s="&#49;"><v>8</v></c>', number_cell('B3', '20')),
            row(4, '<c r="A4" t="inlineStr"><is><r><t>00</t></r><r><t>09</t></r></is></c>', number_cell('B4', '30')),
            row(5, '<c r="A5" t="b"><v>1</v></c>', number_cell('B5', '40')),
            row(6, '<c r="A6"><v></v></c>', number_cell('B6', '50')),
        ])
        for values in (['0007', '0008', '0009', True], [None]):
            with self.subTest(values=values):
                fast = query_xlsx(source, where={'ID': values}, reader='fast')
                compatible = query_xlsx(source, where={'ID': values}, reader='compatible')
                self.assertEqual(fast['rows'], compatible['rows'])
                self.assertEqual(fast['coverage'], compatible['coverage'])
        selected = query_xlsx(source, key_rows=[{'ID': '0008', 'Amount': '20'}], columns=['Amount'])
        self.assertEqual(selected['rows'][0]['row_num'], 3)

    def test_other_quoted_attribute_values_cannot_impersonate_cell_type_or_row_address(self):
        source = self.workbook([
            row(1, text_cell('A1', 'ID')),
            row(2, number_cell('A2', '7')),
        ])
        def add_attributes(data):
            data = data.replace(f'xmlns="{M}"'.encode(), f'xmlns="{M}" xmlns:x="urn:attributes"'.encode())
            data = data.replace(b'<row r="2">', b'<row x:note=" r=\'9\' " r="2">')
            return data.replace(b'<c r="A2" s="0">', b'<c x:note=" t=\'s\' r=\'A9\' " r="A2" s="0">')
        self.rewrite_sheet(source, add_attributes)
        fast = query_xlsx(source, where={'ID': ['7']}, reader='fast')
        compatible = query_xlsx(source, where={'ID': ['7']}, reader='compatible')
        self.assertEqual(fast['rows'], compatible['rows'])
        self.assertEqual(fast['coverage'], compatible['coverage'])
        self.assertEqual(fast['rows'][0]['row_num'], 2)

    def test_return_cell_budget_refuses_partial_output_and_cache_bypass(self):
        source = self.simple()  # Three source rows and three columns.
        cache = self.root / 'budget-cache'
        with self.assertRaisesRegex(ValueError, 'Returned cell budget exceeded'):
            query_xlsx(source, max_return_cells=8, cache_dir=cache)
        self.assertFalse(cache.exists())
        complete = query_xlsx(source, max_return_cells=9, cache_dir=cache)
        self.assertEqual(complete['coverage']['returned_cells'], 9)
        cache_file = Path(complete['cache']['path'])
        payload = json.loads(cache_file.read_text(encoding='utf-8'))
        payload['result']['rows'].append(payload['result']['rows'][0])
        cache_file.write_text(json.dumps(payload), encoding='utf-8')
        with mock.patch.object(xlsx_query, '_query_xlsx_core', side_effect=AssertionError('invalid warm cache must not trigger an unbounded rescan')):
            with self.assertRaisesRegex(ValueError, 'Returned cell budget exceeded'):
                query_xlsx(source, max_return_cells=9, cache_dir=cache)

    def test_projection_and_return_limit_fit_budget_without_stopping_scan(self):
        source = self.simple()
        projected = query_xlsx(source, columns=['ID'], max_return_cells=3)
        self.assertEqual(projected['coverage']['scanned_rows'], 3)
        self.assertEqual(projected['coverage']['matched_rows'], 3)
        self.assertEqual(projected['coverage']['returned_cells'], 3)
        self.assertFalse(projected['coverage']['truncated'])
        limited = query_xlsx(source, limit=1, max_return_cells=3)
        self.assertEqual(limited['coverage']['scanned_rows'], 3)
        self.assertEqual(limited['coverage']['matched_rows'], 3)
        self.assertEqual(limited['coverage']['returned_rows'], 1)
        self.assertEqual(limited['coverage']['returned_cells'], 3)
        self.assertTrue(limited['coverage']['scan_complete'])
        self.assertTrue(limited['coverage']['truncated'])
        zero = query_xlsx(source, limit=0, max_return_cells=1)
        self.assertEqual(zero['coverage']['returned_cells'], 0)
        self.assertEqual(zero['coverage']['matched_rows'], 3)
        cache = self.root / 'different-budgets'
        one = query_xlsx(source, columns=['ID'], max_return_cells=3, cache_dir=cache)
        two = query_xlsx(source, columns=['ID'], max_return_cells=4, cache_dir=cache)
        self.assertNotEqual(one['cache']['key'], two['cache']['key'])
        self.assertFalse(two['cache']['hit'])

    def test_return_cell_budget_requires_a_positive_integer(self):
        source = self.simple()
        for budget in (None, 0, -1, True, False, 1.5, '100'):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(ValueError, 'max_return_cells must be a positive integer'):
                    query_xlsx(source, max_return_cells=budget)
        output = self.root / 'must-not-exist.json'
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/xlsx_query.py'), '--file', str(source), '--max-return-cells', '0', '--no-cache', '--out', str(output)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertFalse(output.exists())

    def test_spilled_shared_strings_match_memory_reader_and_preserve_exact_values(self):
        source = self.workbook([
            row(1, '<c r="A1" t="s"><v>0</v></c>', text_cell('B1', 'Amount')),
            row(2, '<c r="A2" t="s"><v>1</v></c>', number_cell('B2', '10.0000000000001')),
            row(8, '<c r="A8" t="s"><v>2</v></c>', number_cell('B8', '20')),
            row(12, '<c r="A12" t="s"><v>3</v></c>', number_cell('B12', '30')),
        ], strings='<si><t>ID</t></si><si><t>0007</t></si><si><t>7</t></si>'
                   '<si><r><t>00</t></r><r><t>07</t></r><rPh><t>exclude</t></rPh></si>')
        expected = query_xlsx(source, where={'ID': ['0007']}, shared_string_memory_bytes=65536)
        for reader in ('fast', 'compatible'):
            with self.subTest(reader=reader):
                actual = query_xlsx(source, where={'ID': ['0007']}, reader=reader,
                                    shared_string_memory_bytes=1)
                self.assertEqual(actual['rows'], expected['rows'])
                self.assertEqual(actual['coverage'], expected['coverage'])
                self.assertEqual([r['row_num'] for r in actual['rows']], [2, 12])
                self.assertEqual(actual['shared_strings']['backend'], 'sqlite')
                self.assertEqual(actual['shared_strings']['count'], 4)
                self.assertLessEqual(actual['shared_strings']['peak_retained_value_bytes_estimate'], 1)

    def test_shared_string_spill_cleans_temporary_evidence_on_success_and_failure(self):
        source = self.shared_fixture()
        paths = []
        original = tempfile.TemporaryDirectory

        def temporary(*args, **kwargs):
            directory = original(*args, **kwargs)
            paths.append(Path(directory.name))
            return directory

        with mock.patch.object(xlsx_query.tempfile, 'TemporaryDirectory', side_effect=temporary):
            query_xlsx(source, shared_string_memory_bytes=1)
            self.rewrite_sheet(source, lambda data: data.replace(b'<v>2</v>', b'<v>9999</v>'))
            with self.assertRaisesRegex(ValueError, 'Invalid shared-string'):
                query_xlsx(source, shared_string_memory_bytes=1)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_spill_bounds_python_retention_for_large_high_cardinality_string_table(self):
        # The result needs one value out of 8 MiB of unrelated shared strings.
        # Checking Python allocation catches accidental materialization of the
        # full table. This deliberately does not claim to measure native RSS.
        source = self.workbook([
            row(1, '<c r="A1" t="s"><v>0</v></c>'),
            row(2, '<c r="A2" t="s"><v>8000</v></c>'),
        ], strings='<si><t>ID</t></si>' + ''.join(
            '<si><t>' + str(index).zfill(8) + 'x' * 1024 + '</t></si>'
            for index in range(8000)))
        tracemalloc.start()
        try:
            result = query_xlsx(source, shared_string_memory_bytes=32768)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result['rows'][0]['values']['ID'], '00007999' + 'x' * 1024)
        self.assertEqual(result['shared_strings']['backend'], 'sqlite')
        self.assertLessEqual(result['shared_strings']['peak_retained_value_bytes_estimate'], 32768)
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_shared_string_cache_hit_reports_original_storage_without_loading(self):
        source = self.shared_fixture()
        cache = self.root / 'strings-cache'
        cold = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache, shared_string_memory_bytes=1)
        with mock.patch.object(xlsx_query, '_shared_strings', side_effect=AssertionError('warm query loaded SST')):
            warm = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache, shared_string_memory_bytes=1)
        self.assertEqual(warm['rows'], cold['rows'])
        self.assertTrue(warm['cache']['hit'])
        self.assertEqual(warm['shared_strings']['backend'], 'sqlite')
        self.assertEqual(warm['shared_strings']['metrics_scope'], 'original_scan; not loaded on cache hit')
        self.assertEqual(cold['shared_strings']['metrics_scope'], 'current_scan')

    def test_legacy_cache_and_changed_memory_budget_do_not_rescan_identical_evidence(self):
        source = self.shared_fixture()
        cache = self.root / 'legacy-strings-cache'
        cold = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache, shared_string_memory_bytes=65536)
        path = Path(cold['cache']['path'])
        saved = json.loads(path.read_text(encoding='utf-8'))
        del saved['result']['shared_strings']  # Validated pre-spill reader output.
        path.write_text(json.dumps(saved), encoding='utf-8')
        with mock.patch.object(xlsx_query, '_query_xlsx_core', side_effect=AssertionError('storage budget invalidated evidence')):
            warm = query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache, shared_string_memory_bytes=1)
        self.assertEqual(warm['rows'], cold['rows'])
        self.assertEqual(warm['cache']['key'], cold['cache']['key'])
        self.assertEqual(warm['shared_strings']['backend'], 'not_recorded')
        self.assertIn('not loaded on cache hit', warm['shared_strings']['metrics_scope'])

    def test_shared_string_budget_validation_and_cli(self):
        source = self.shared_fixture()
        for value in (None, 0, -1, True, 1.5, '100'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'shared_string_memory_bytes must be a positive integer'):
                    query_xlsx(source, shared_string_memory_bytes=value)
        output = self.root / 'spilled.json'
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/xlsx_query.py'), '--file', str(source),
                              '--shared-string-memory-bytes', '1', '--no-cache', '--out', str(output)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(result['shared_strings']['backend'], 'sqlite')
        self.assertEqual(result['coverage']['returned_rows'], 2)

    def test_parallel_validation_preserves_evidence_and_reaps_worker(self):
        source = self.shared_fixture()
        normal = query_xlsx(source, where={'ID': ['keep']})
        processes = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(xlsx_query, 'PARALLEL_XML_THRESHOLD_BYTES', 0), \
                mock.patch.object(xlsx_query.subprocess, 'Popen', side_effect=launch):
            parallel = query_xlsx(source, where={'ID': ['keep']})
        self.assertEqual(parallel['rows'], normal['rows'])
        self.assertEqual(parallel['coverage'], normal['coverage'])
        self.assertEqual(parallel['xml_validation']['mode'], 'parallel_process')
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stderr.closed)

    def test_parallel_validation_rejects_invalid_trailing_xml_without_cache(self):
        source = self.shared_fixture()
        self.rewrite_sheet(source, lambda data: data + b'<unexpected/>')
        cache = self.root / 'bad-parallel-cache'
        with mock.patch.object(xlsx_query, 'PARALLEL_XML_THRESHOLD_BYTES', 0):
            with self.assertRaisesRegex(ValueError, 'Complete worksheet XML validation failed'):
                query_xlsx(source, where={'ID': ['keep']}, cache_dir=cache)
        self.assertFalse(cache.exists())

    def test_parallel_worker_cleanup_on_query_failure_and_safe_startup_fallback(self):
        source = self.shared_fixture()
        original = subprocess.Popen
        processes = []

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(xlsx_query, 'PARALLEL_XML_THRESHOLD_BYTES', 0), \
                mock.patch.object(xlsx_query.subprocess, 'Popen', side_effect=launch):
            with self.assertRaisesRegex(ValueError, 'Returned cell budget'):
                query_xlsx(source, where={'ID': ['keep', 'skip']}, max_return_cells=1)
            # Unsupported comments switch to the compatible fully validating reader.
            self.rewrite_sheet(source, lambda data: data.replace(b'<sheetData>', b'<sheetData><!--comment-->'))
            fallback = query_xlsx(source, where={'ID': ['keep']})
        self.assertTrue(all(process.poll() is not None and process.stderr.closed for process in processes))
        self.assertEqual(fallback['reader'], 'compatible_xml')
        with mock.patch.object(xlsx_query, 'PARALLEL_XML_THRESHOLD_BYTES', 0), \
                mock.patch.object(xlsx_query.subprocess, 'Popen', side_effect=OSError('worker unavailable')):
            self.assertEqual(query_xlsx(self.shared_fixture(), where={'ID': ['keep']})['xml_validation'],
                             {'mode': 'in_process', 'complete': True, 'worker_start_fallback': 'OSError'})

    def test_sample_string_lookup_skips_unneeded_trees_and_retains_rich_text(self):
        source = self.workbook([row(1, '<c r="A1" t="s"><v>5000</v></c>')],
                              strings='<si><t>unused</t></si>' * 5000 +
                              '<si><r><t>record</t></r><r><t>_id</t></r><rPh><t>exclude</t></rPh></si>')
        with mock.patch.object(xlsx_query, '_elements', side_effect=AssertionError('sample built shared string trees')):
            result = inspect_xlsx(source, header_rows=1)
        self.assertEqual(result['sheets'][0]['rows'][0]['values']['A'], 'record_id')
        self.assertEqual(result['coverage']['shared_strings_retained'], 1)
        self.assertEqual(result['coverage']['shared_string_entries_scanned'], 5001)
        self.assertFalse(result['scan_complete'])


if __name__ == '__main__':
    unittest.main()
