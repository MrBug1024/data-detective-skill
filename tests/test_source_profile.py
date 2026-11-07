"""Profile real layouts without treating samples as full-key proof."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import source_profile
from source_profile import profile_source


class SourceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def workbook(self, rows, name='source.xlsx', extra=None):
        from openpyxl import Workbook
        book = Workbook()
        book.active.title = 'Records'
        for row in rows:
            book.active.append(row)
        if extra:
            sheet = book.create_sheet('Other')
            for row in extra:
                sheet.append(row)
        path = self.root / name
        book.save(path)
        book.close()
        return path

    def test_title_and_blank_before_header_keep_physical_positions(self):
        path = self.workbook([['Attachment'], [], ['record_id', 'amount', 'description'],
                              ['0001', 1.25, 'entry one'], ['0002', 2.5, 'entry two']])
        result = profile_source(path)
        sheet = result['sheets'][0]
        self.assertEqual(sheet['header_status'], 'selected')
        self.assertEqual(sheet['header_row'], 3)
        self.assertEqual(sheet['fields'][0]['cell'], 'A3')
        self.assertEqual(sheet['sample_rows'][0]['row_num'], 4)
        self.assertEqual(sheet['sample_rows'][0]['values']['record_id'], '0001')
        self.assertEqual(sheet['sample_rows'][0]['cells']['amount']['address'], 'B4')
        self.assertIn('identifier_candidate', sheet['fields'][0]['role_hints'])
        self.assertEqual(sheet['key_uniqueness_scope'], 'sample_only_not_full_source')
        self.assertFalse(result['coverage']['scan_complete'])

    def test_sparse_wide_records_still_support_header(self):
        path = self.workbook([['record_id', 'amount', 'code', 'date', 'status', 'name', 'description', 'total'],
                              ['0001', 4.5], ['0002', 5.25]])
        sheet = profile_source(path)['sheets'][0]
        self.assertEqual(sheet['header_status'], 'selected')
        self.assertEqual(sheet['header_row'], 1)
        self.assertEqual(len(sheet['fields']), 8)
        self.assertIsNone(sheet['sample_rows'][0]['values']['code'])

    def test_ambiguous_header_requires_explicit_selection(self):
        path = self.workbook([['group one', 'group two'], ['name', 'amount'], ['Alice', 4], ['Bob', 8]])
        sheet = profile_source(path)['sheets'][0]
        self.assertEqual(sheet['header_status'], 'ambiguous')
        self.assertIsNone(sheet['header_row'])
        self.assertEqual(sheet['fields'], [])
        explicit = profile_source(path, header_row=2)['sheets'][0]
        self.assertEqual(explicit['header_selection'], 'explicit')
        self.assertEqual(explicit['sample_rows'][0]['row_num'], 3)

    def test_multiple_sheets_and_explicit_sheet_are_preserved(self):
        path = self.workbook([['id', 'amount'], ['A', 1]], extra=[['Title'], [], ['id', 'name'], ['A', 'One']])
        all_sheets = profile_source(path)['sheets']
        self.assertEqual([(s['sheet'], s['header_row']) for s in all_sheets], [('Records', 1), ('Other', 3)])
        single = profile_source(path, sheet='Other')['sheets']
        self.assertEqual([(s['sheet'], s['header_row']) for s in single], [('Other', 3)])

    def test_cache_binds_source_bytes_and_selection_without_reparsing(self):
        path = self.workbook([['id', 'amount'], ['A', 1], ['B', 2]])
        cache = self.root / 'profile-cache'
        cold = profile_source(path, cache_dir=cache)
        with mock.patch.object(source_profile, 'inspect_xlsx', side_effect=AssertionError('warm profile parsed XML')):
            warm = profile_source(path, cache_dir=cache)
        self.assertTrue(warm['cache']['hit'])
        self.assertEqual(cold['sheets'], warm['sheets'])
        self.workbook([['id', 'amount'], ['A', 3], ['B', 2]])
        revised = profile_source(path, cache_dir=cache)
        self.assertFalse(revised['cache']['hit'])
        self.assertNotEqual(cold['source_sha256'], revised['source_sha256'])

    def test_jsonl_and_csv_profiles_preserve_types_and_record_grain(self):
        csv = self.root / 'records.csv'
        csv.write_text('record_id,amount\n0001,1.25\n0002,2.50\n', encoding='utf-8')
        sheet = profile_source(csv)['sheets'][0]
        self.assertEqual(sheet['header_status'], 'selected')
        self.assertEqual(sheet['sample_rows'][0]['values']['record_id'], '0001')
        jsonl = self.root / 'records.jsonl'
        jsonl.write_text('{"id":"0001","items":[{"value":1.25}]}\n\n{"id":"0002"}\n', encoding='utf-8')
        sheet = profile_source(jsonl)['sheets'][0]
        self.assertIsNone(sheet['header_row'])
        self.assertEqual([r['row_num'] for r in sheet['sample_rows']], [1, 3])
        self.assertEqual(sheet['sample_rows'][0]['field_pointers']['/items/0/value'], '/items/0/value')
        self.assertEqual(len(sheet['sample_rows']), 2)

    def test_csv_encoding_delimiter_and_cache_follow_the_declared_reader(self):
        cache = self.root / 'profiles'
        for encoding in ('gb18030', 'utf-16'):
            with self.subTest(encoding=encoding):
                path = self.root / (encoding + '.csv')
                path.write_text('记录编号;金额\n0001;1.25\n0002;2.50\n', encoding=encoding)
                result = profile_source(path, encoding=encoding, delimiter=';', cache_dir=cache)
                sheet = result['sheets'][0]
                self.assertEqual([field['name'] for field in sheet['fields']], ['记录编号', '金额'])
                self.assertEqual(sheet['sample_rows'][0]['values']['记录编号'], '0001')
                with mock.patch.object(source_profile, '_text_profile', side_effect=AssertionError('matching profile reparsed')):
                    warm = profile_source(path, encoding=encoding, delimiter=';', cache_dir=cache)
                self.assertTrue(warm['cache']['hit'])
                with self.assertRaises(UnicodeError):
                    profile_source(path, cache_dir=cache, delimiter=';')
        path = self.root / 'delimited.csv'
        path.write_text('id;amount\nA;1\nB;2\n', encoding='utf-8')
        semicolon = profile_source(path, delimiter=';', cache_dir=cache)
        comma = profile_source(path, delimiter=',', cache_dir=cache)
        self.assertNotEqual(semicolon['cache']['key'], comma['cache']['key'])
        self.assertEqual(len(semicolon['sheets'][0]['fields']), 2)
        self.assertEqual(len(comma['sheets'][0]['fields']), 1)


if __name__ == '__main__':
    unittest.main()
