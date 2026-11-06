"""Behavioral regressions for independently addressable cross-format evidence."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from parsers import FileParser, CSVParser, ExcelParser, SQLParser, TextParser
from indexer import build_index
from entity_extractor import EntityExtractor
from matcher import MatchEngine


class FormatIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def text(self, name, content, encoding='utf-8'):
        path = self.root / name
        path.write_bytes(content.encode(encoding))
        return str(path)

    def sidecar(self, source, text='客户编号 C0007', digest=None, bbox=None):
        path = Path(source + '.ocr.json')
        path.write_text(json.dumps({'source_sha256': digest or hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                                   'engine': 'manual_fixture', 'pages': [{'page_num': 1, 'width': 400, 'height': 200,
                                   'regions': [{'text': text, 'bbox': bbox or [5, 5, 350, 60], 'confidence': 0.96}]}]}, ensure_ascii=False), encoding='utf-8')
        return path

    def image(self):
        from PIL import Image
        path = str(self.root / 'scan.png')
        Image.new('RGB', (400, 200), 'white').save(path)
        return path

    def test_csv_gb18030_semicolon_and_literal_id(self):
        units = CSVParser().parse(self.text('中文.csv', '客户ID;姓名;金额\r\n0007;张三;10.20\r\n', 'gb18030'))
        self.assertEqual(units[0].get_field('客户ID'), '0007')
        self.assertEqual(units[0].get_field('姓名'), '张三')
        self.assertEqual(units[0].metadata['delimiter'], ';')
        self.assertEqual(units[0].metadata['encoding'], 'gb18030')

    def test_csv_multiline_and_blank_lines_preserve_physical_location(self):
        content = 'ID,note\r\n\r\n0007,"first\r\nsecond"\r\n\r\n0008,last\r\n'
        units = CSVParser().parse(self.text('records.csv', content))
        self.assertEqual([u.row_num for u in units], [3, 6])
        self.assertEqual(units[0].metadata['line_end'], 4)
        self.assertEqual(content[units[0].char_offset:].splitlines()[0], '0007,"first')
        self.assertEqual(units[0].get_field('note'), 'first\r\nsecond')

    def test_utf16_tsv(self):
        units = FileParser().parse(self.text('records.tsv', 'ID\t名称\n001\t公司\n', 'utf-16'))
        self.assertEqual(units[0].get_field('ID'), '001')

    def test_text_gb18030_has_exact_offsets(self):
        content = '第一段\r\n\r\n  第二段'
        units = TextParser().parse(self.text('legacy.txt', content, 'gb18030'))
        self.assertEqual(units[1].char_offset, content.index('第二段'))
        self.assertEqual(units[1].row_num, 3)

    def test_xlsx_retains_formula_missing_cache_and_display_id(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['客户ID', '基数', '金额'])
        sheet.append([7, 10, '=B2*2'])
        sheet['A2'].number_format = '00000'
        path = str(self.root / 'formulas.xlsx')
        workbook.save(path)
        parser = ExcelParser()
        unit = parser.parse(path)[0]
        self.assertEqual(unit.get_field('客户ID'), '00007')
        self.assertEqual(unit.metadata['cells']['客户ID']['underlying_value'], 7)
        self.assertEqual(unit.metadata['cells']['金额']['formula'], '=B2*2')
        self.assertIsNone(unit.get_field('金额'))
        self.assertEqual(unit.metadata['cells']['金额']['address'], 'C2')
        self.assertTrue(any(x['code'] == 'xlsx_missing_formula_cache' for x in parser.diagnostics))

    def test_xlsx_empty_physical_rows_do_not_shift_addresses(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['ID', 'name'])
        sheet.append(['001', None])
        sheet.append([None, None])
        sheet.append(['002', '乙'])
        path = str(self.root / 'blank.xlsx')
        workbook.save(path)
        units = ExcelParser().parse(path)
        self.assertEqual([u.row_num for u in units], [2, 4])
        self.assertEqual(units[0].metadata['cells']['name']['address'], 'B2')

    def test_xlsx_duplicate_headers_reject_ambiguous_fields(self):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(['ID', 'ID'])
        workbook.active.append(['001', '002'])
        path = str(self.root / 'duplicate.xlsx')
        workbook.save(path)
        with self.assertRaises(ValueError):
            ExcelParser().parse(path)

    def test_docx_tables_are_in_document_order_and_locatable(self):
        from docx import Document
        doc = Document()
        doc.add_paragraph('before')
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = '客户ID', '姓名'
        table.cell(1, 0).text, table.cell(1, 1).text = '0007', '张三'
        doc.add_paragraph('after')
        path = str(self.root / 'record.docx')
        doc.save(path)
        units = FileParser().parse(path)
        self.assertEqual(units[0].raw_text, 'before')
        self.assertEqual(units[-1].raw_text, 'after')
        target = next(u for u in units if u.get_field('客户ID') == '0007')
        self.assertEqual(target.metadata['table_row'], 2)
        self.assertEqual(target.metadata['cells']['姓名'], {'table': 1, 'row': 2, 'column': 2})
        self.assertIsNone(target.page_num)

    def test_pdf_blank_page_is_reported_and_other_page_kept(self):
        from reportlab.pdfgen.canvas import Canvas
        path = str(self.root / 'partial.pdf')
        canvas = Canvas(path)
        canvas.showPage()
        canvas.drawString(50, 700, 'Customer C0007')
        canvas.save()
        index = build_index([path])
        unit = next(iter(index.id_map.values()))
        self.assertEqual(unit.page_num, 2)
        status = index.ingestion_status[os.path.normcase(path)]
        self.assertEqual(status['status'], 'partial')
        self.assertTrue(any(i.get('page_num') == 1 and i['code'] == 'pdf_no_text' for i in status['issues']))

    def test_image_without_ocr_is_unavailable(self):
        path = self.image()
        index = build_index([path])
        self.assertEqual(index.ingestion_status[os.path.normcase(path)]['status'], 'unavailable')
        self.assertEqual(len(index.id_map), 0)

    def test_ocr_retains_original_hash_bbox_and_page(self):
        path = self.image()
        self.sidecar(path)
        units = FileParser().parse(path)
        self.assertEqual(units[0].page_num, 1)
        self.assertEqual(units[0].metadata['bbox'], [5, 5, 350, 60])
        self.assertEqual(units[0].metadata['source_sha256'], hashlib.sha256(Path(path).read_bytes()).hexdigest())
        self.assertFalse(units[0].metadata['ocr_verified'])

    def test_stale_ocr_hash_is_rejected(self):
        path = self.image()
        self.sidecar(path, digest='0' * 64)
        with self.assertRaises(RuntimeError):
            FileParser().parse(path)

    def test_ocr_dimensions_are_checked_against_original_image(self):
        path = self.image()
        sidecar = self.sidecar(path)
        data = json.loads(sidecar.read_text(encoding='utf-8'))
        data['pages'][0]['width'] = 800
        sidecar.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            FileParser().parse(path)

    def test_pdf_hybrid_image_content_does_not_disappear_behind_text(self):
        from reportlab.pdfgen.canvas import Canvas
        image = self.image()
        path = str(self.root / 'hybrid.pdf')
        canvas = Canvas(path)
        canvas.drawString(50, 700, 'Digital header')
        canvas.drawImage(image, 50, 300, width=400, height=200)
        canvas.save()
        parser = FileParser()
        self.assertTrue(parser.parse(path))
        self.assertTrue(any(i['code'] == 'pdf_images_not_transcribed' for i in parser.last_diagnostics))
        self.sidecar(path, text='C0007 from scanned body')
        units = parser.parse(path)
        ocr = next(u for u in units if u.metadata.get('extraction') == 'ocr_sidecar')
        self.assertEqual(ocr.raw_text, 'C0007 from scanned body')
        self.assertEqual(ocr.page_num, 1)
        self.assertTrue(any('Digital header' in u.raw_text for u in units))

    def test_bad_ocr_bbox_is_rejected(self):
        path = self.image()
        self.sidecar(path, bbox=[0, 0, 900, 80])
        with self.assertRaises(RuntimeError):
            FileParser().parse(path)

    def test_sidecar_add_change_remove_invalidates_cache(self):
        path = self.image()
        cache = str(self.root / 'cache')
        self.assertEqual(len(build_index([path], cache).id_map), 0)
        sidecar = self.sidecar(path, text='客户 C0007')
        self.assertEqual(next(iter(build_index([path], cache).id_map.values())).raw_text, '客户 C0007')
        self.sidecar(path, text='客户 C0008')
        self.assertEqual(next(iter(build_index([path], cache).id_map.values())).raw_text, '客户 C0008')
        sidecar.unlink()
        self.assertEqual(len(build_index([path], cache).id_map), 0)

    def test_sql_quoted_values_schema_and_multiple_tuples(self):
        content = '-- export\nCREATE TABLE customers (customer_id TEXT, name TEXT, amount DECIMAL(10,2));\nINSERT INTO customers VALUES\n(\'0007\', N\'深\'\'圳;公司\', 10.20),\n(\'0008\', \'other\', NULL);\n'
        units = SQLParser().parse(self.text('dump.sql', content))
        records = [u for u in units if not u.is_rule]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].get_field('customer_id'), '0007')
        self.assertEqual(records[0].get_field('name'), "深'圳;公司")
        self.assertEqual(records[0].get_field('amount'), '10.20')
        self.assertIsNone(records[1].get_field('amount'))
        self.assertEqual([u.row_num for u in records], [4, 5])
        self.assertEqual(content[records[0].char_offset], '(')
        self.assertFalse(records[0].metadata['executed'])

    def test_sql_explicit_columns_override_create_order(self):
        units = SQLParser().parse(self.text('reordered.sql', "CREATE TABLE t (id TEXT,name TEXT); INSERT INTO t (name,id) VALUES ('Alice','0007');"))
        self.assertEqual(units[-1].get_field('id'), '0007')
        self.assertEqual(units[-1].metadata['column_origin'], 'explicit')

    def test_sql_query_is_static_text_not_materialized_result(self):
        parser = SQLParser()
        units = parser.parse(self.text('query.sql', 'SELECT * FROM customers; DROP TABLE customers;'))
        self.assertEqual(len(units), 2)
        self.assertTrue(all(u.is_rule and not u.metadata['executed'] for u in units))
        self.assertTrue(any(i['code'] == 'sql_static_only' for i in parser.diagnostics))

    def test_sql_expression_not_invented_as_value(self):
        parser = SQLParser()
        units = parser.parse(self.text('expression.sql', "INSERT INTO t (id,ts) VALUES ('0007', NOW());"))
        self.assertEqual(units[0].get_field('id'), '0007')
        self.assertIsNone(units[0].get_field('ts'))
        self.assertEqual(units[0].metadata['unparsed_fields'], ['ts'])
        self.assertIn('NOW()', units[0].raw_text)

    def test_sql_malformed_literal_fails_instead_of_partial_invention(self):
        with self.assertRaises(RuntimeError):
            FileParser().parse(self.text('bad.sql', "INSERT INTO t (id) VALUES ('unterminated);"))

    def test_doc_conversion_requirement_is_machine_readable(self):
        path = self.text('legacy.doc', 'not really a binary doc')
        index = build_index([path])
        status = index.ingestion_status[os.path.normcase(path)]
        self.assertEqual(status['status'], 'unavailable')
        self.assertEqual(status['issues'][0]['code'], 'legacy_word_requires_conversion')

    def test_sql_and_docx_structured_extraction_and_key_conflicts(self):
        from docx import Document
        sql = self.text('customers.sql', "CREATE TABLE customers(customer_id TEXT,name TEXT,product_code TEXT); INSERT INTO customers VALUES ('0007','Alice Smith','P0042'),('0008','Alice Smith','P0042');")
        doc = Document()
        table = doc.add_table(rows=3, cols=3)
        for row, values in zip(table.rows, [('customer_id', 'name', 'product_code'), ('0007', 'Alice Smith', 'P0042'), ('0008', 'Alice Smith', 'P0042')]):
            for cell, value in zip(row.cells, values):
                cell.text = value
        docx = str(self.root / 'customers.docx')
        doc.save(docx)
        query = FileParser().parse(self.text('result.csv', 'customer_id,product_code\n0007,P0042\n'))[0]
        for path in (sql, docx):
            with self.subTest(path=path):
                index = build_index([path])
                good = next(u for u in index.id_map.values() if u.get_field('customer_id') == '0007')
                bad = next(u for u in index.id_map.values() if u.get_field('customer_id') == '0008')
                entities, _ = EntityExtractor().extract_from_unit(good)
                self.assertTrue(any(e.entity_type == 'id' and e.name == '0007' and e.attributes.get('field') == 'customer_id' for e in entities))
                self.assertTrue(any(e.entity_type == 'person' and e.name == 'Alice Smith' for e in entities))
                found = [match.unit.unit_id for match in MatchEngine(index).match_result_unit(query)]
                self.assertIn(good.unit_id, found)
                self.assertNotIn(bad.unit_id, found)


if __name__ == '__main__':
    unittest.main()
