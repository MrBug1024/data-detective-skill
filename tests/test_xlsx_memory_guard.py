"""Keep full-memory XLSX parsing bounded and surface useful recovery steps."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from parsers import ExcelParser, FileParser


class XlsxMemoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def oversized_archive(self):
        path = self.root / 'large.xlsx'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('xl/worksheets/sheet1.xml', b'a' * 40)
            archive.writestr('xl/worksheets/sheet2.xml', b'b' * 40)
        return path

    def small_workbook(self):
        from openpyxl import Workbook
        path = self.root / 'small.xlsx'
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Charges'
        sheet.append(['visit_id', 'amount', 'calculated'])
        sheet.append(['0007', 10, '=B2*2'])
        workbook.save(path)
        workbook.close()
        return path

    def test_uncompressed_sheet_total_is_rejected_before_workbooks_open(self):
        path = self.oversized_archive()
        with zipfile.ZipFile(path) as archive:
            self.assertLess(sum(entry.compress_size for entry in archive.infolist()), 64)
            self.assertEqual(sum(entry.file_size for entry in archive.infolist()), 80)
        with mock.patch('parsers.MAX_XLSX_WORKSHEET_XML_BYTES', 64):
            with mock.patch('openpyxl.load_workbook') as load:
                with self.assertRaisesRegex(ValueError, 'xlsx_query.py.*稳定键') as caught:
                    ExcelParser().parse(path)
                load.assert_not_called()
        self.assertIn('80字节', str(caught.exception))
        self.assertIn('64字节', str(caught.exception))

    def test_file_parser_preserves_guard_reason_and_recovery_command(self):
        path = self.oversized_archive()
        with mock.patch('parsers.MAX_XLSX_WORKSHEET_XML_BYTES', 64):
            with self.assertRaisesRegex(RuntimeError, 'xlsx_query.py.*稳定键') as caught:
                FileParser().parse(str(path))
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertIn('80字节', str(caught.exception))

    def test_small_workbook_retains_values_formulas_and_physical_addresses(self):
        parser = ExcelParser()
        units = parser.parse(self.small_workbook())
        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual((unit.sheet_name, unit.row_num), ('Charges', 2))
        self.assertEqual(unit.get_field('visit_id'), '0007')
        self.assertEqual(unit.get_field('amount'), 10)
        self.assertIsNone(unit.get_field('calculated'))
        self.assertEqual(unit.metadata['cells']['calculated']['formula'], '=B2*2')
        self.assertEqual(unit.metadata['cells']['amount']['address'], 'B2')
        self.assertTrue(any(issue['code'] == 'xlsx_missing_formula_cache'
                            for issue in parser.diagnostics))

    def test_budget_measures_worksheet_xml_instead_of_other_zip_members(self):
        path = self.small_workbook()
        with zipfile.ZipFile(path, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
            worksheet_bytes = sum(entry.file_size for entry in archive.infolist()
                                  if entry.filename.startswith('xl/worksheets/')
                                  and entry.filename.endswith('.xml'))
            archive.writestr('xl/media/large.bin', b'x' * (worksheet_bytes * 10))
        with mock.patch('parsers.MAX_XLSX_WORKSHEET_XML_BYTES', worksheet_bytes):
            self.assertEqual(ExcelParser().parse(path)[0].get_field('visit_id'), '0007')


if __name__ == '__main__':
    unittest.main()
