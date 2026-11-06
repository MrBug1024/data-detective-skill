#!/usr/bin/env python3
"""测试匹配引擎"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import unittest
import tempfile
import pandas as pd
from parsers import FileParser, AddressableUnit
from indexer import build_index
from matcher import MatchEngine, MatchConfig


def make_unit(source_file, row_num, field_names, field_values, is_rule=False):
    import hashlib
    uid = hashlib.md5(f"{source_file}|{row_num}".encode()).hexdigest()[:12]
    return AddressableUnit(
        unit_id=uid,
        source_file=source_file,
        file_type='xlsx',
        row_num=row_num,
        page_num=None,
        paragraph_num=None,
        char_offset=None,
        sheet_name='Sheet1',
        field_names=field_names,
        field_values=field_values,
        raw_text=' | '.join(f"{k}={v}" for k, v in zip(field_names, field_values)),
        is_rule=is_rule,
    )


class TestMatchEngine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

        # 创建源文件
        src_path = os.path.join(self.tmpdir, 'src.xlsx')
        pd.DataFrame({
            '客户ID': ['C000001', 'C000500', 'C000999'],
            '姓名': ['张三', '张伟华', '李明'],
            '城市': ['上海', '北京', '广州'],
        }).to_excel(src_path, index=False)

        self.index = build_index([src_path])
        self.config = MatchConfig()
        self.engine = MatchEngine(self.index, self.config)

        # 结果单元
        self.result_unit = make_unit(
            os.path.join(self.tmpdir, 'R1.xlsx'),
            2,
            ['客户ID', '客户姓名', '城市'],
            ['C000500', '张伟华', '北京'],
        )

    def test_exact_id_match(self):
        matches = self.engine.match_result_unit(self.result_unit)
        exact_matches = [m for m in matches if m.match_type == 'exact_id']
        self.assertGreater(len(exact_matches), 0)

    def test_direct_match(self):
        matches = self.engine.match_result_unit(self.result_unit)
        # 应该找到包含C000500的行
        found = any('C000500' in str(m.unit.field_values) for m in matches)
        self.assertTrue(found)

    def test_match_score_range(self):
        matches = self.engine.match_result_unit(self.result_unit)
        for m in matches:
            self.assertGreaterEqual(m.score, 0.0)
            self.assertLessEqual(m.score, 1.0)

    def test_no_self_match(self):
        """不应该匹配自身文件"""
        result_path = os.path.join(self.tmpdir, 'R1.xlsx')
        result_unit = make_unit(
            result_path, 2,
            ['客户ID'], ['C000500']
        )
        # 添加结果文件到索引
        pd.DataFrame({'客户ID': ['C000500']}).to_excel(result_path, index=False)
        from indexer import SourceIndex
        index2 = build_index([result_path, os.path.join(self.tmpdir, 'src.xlsx')])
        engine2 = MatchEngine(index2, self.config)
        matches = engine2.match_result_unit(result_unit)
        # 不应该有来自result_path的匹配
        self_matches = [m for m in matches if m.unit.source_file == result_path]
        self.assertEqual(len(self_matches), 0)


class TestFuzzyMatch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        src_path = os.path.join(self.tmpdir, 'src.xlsx')
        pd.DataFrame({
            '产品名称': ['高端商务笔记本', '普通笔记本电脑', '平板电脑'],
            '价格': [6800, 3200, 2500],
        }).to_excel(src_path, index=False)
        self.index = build_index([src_path])
        self.config = MatchConfig({'fuzzy_threshold': 70})
        self.engine = MatchEngine(self.index, self.config)

    def test_fuzzy_match_similar_name(self):
        result_unit = make_unit(
            os.path.join(self.tmpdir, 'R.xlsx'), 2,
            ['产品名称', '金额'],
            ['高端商务笔记本', 6800],
        )
        matches = self.engine.match_result_unit(result_unit)
        self.assertGreater(len(matches), 0)
        top = matches[0]
        self.assertGreater(top.score, 0.5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
