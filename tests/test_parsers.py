#!/usr/bin/env python3
"""测试文件解析器"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'examples'))

import unittest
import tempfile
import pandas as pd
from parsers import FileParser, ExcelParser, TextParser, AddressableUnit


class TestExcelParser(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # 创建测试Excel
        self.xlsx_path = os.path.join(self.tmpdir, 'test.xlsx')
        df = pd.DataFrame({
            '客户ID': ['C001', 'C002', 'C003'],
            '姓名': ['张三', '李四', '王五'],
            '金额': [1000.0, 2000.0, 3000.0],
        })
        df.to_excel(self.xlsx_path, index=False)

    def test_parse_returns_correct_count(self):
        parser = ExcelParser()
        units = parser.parse(self.xlsx_path)
        self.assertEqual(len(units), 3)

    def test_unit_fields(self):
        parser = ExcelParser()
        units = parser.parse(self.xlsx_path)
        u = units[0]
        self.assertEqual(u.file_type, 'xlsx')
        self.assertEqual(u.row_num, 2)  # header=row1, first data=row2
        self.assertIn('客户ID', u.field_names)

    def test_parse_result_row(self):
        fp = FileParser()
        unit = fp.parse_result_row(self.xlsx_path, 2)
        self.assertIsNotNone(unit)
        self.assertEqual(unit.row_num, 2)

    def test_parse_result_row_notfound(self):
        fp = FileParser()
        unit = fp.parse_result_row(self.xlsx_path, 999)
        self.assertIsNone(unit)


class TestTextParser(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.md_path = os.path.join(self.tmpdir, 'rules.md')
        with open(self.md_path, 'w', encoding='utf-8') as f:
            f.write("# 规则1\n计算公式：金额 = 数量 × 单价\n\n# 规则2\n当金额 > 1000 时触发审核\n")

    def test_parse_md(self):
        parser = TextParser()
        units = parser.parse(self.md_path)
        self.assertGreater(len(units), 0)
        self.assertTrue(any(u.is_rule for u in units))

    def test_file_type(self):
        parser = TextParser()
        units = parser.parse(self.md_path)
        self.assertEqual(units[0].file_type, 'md')


class TestFileParser(unittest.TestCase):
    def test_unsupported_type(self):
        fp = FileParser()
        units = fp.parse('/tmp/nonexistent.xyz')
        self.assertEqual(units, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
