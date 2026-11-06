"""Behavioral checks for bounded, exact XLSX extraction without a global index."""
import hashlib
import json
import os
import io
from pathlib import Path
import subprocess
import sys
import tempfile
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


if __name__ == '__main__':
    unittest.main()
