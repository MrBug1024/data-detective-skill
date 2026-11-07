"""Behavioral regressions for retrieval, evidence mapping and extraction."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from parsers import AddressableUnit, TextParser, ExcelParser, CSVParser, FileParser
from indexer import SourceIndex, build_index
from matcher import MatchEngine, MatchConfig
from entity_extractor import EntityExtractor


def unit(name, names, values, kind='xlsx', row=2):
    return AddressableUnit(name + str(row), os.path.abspath(name), kind,
                           row, None, None, 0, 'Sheet1', names, values,
                           ' | '.join(f'{k}={v}' for k, v in zip(names, values)))


class TestRetrievalPrecision(unittest.TestCase):
    def test_config_rejects_bool_nonfinite_and_out_of_range_limits(self):
        for configuration in ({'max_candidates': True}, {'max_candidates': 201},
                              {'fuzzy_threshold': True}, {'semantic_threshold': float('nan')},
                              {'field_weights': {'客户ID': float('inf')}}, {'max_hops': False}):
            with self.subTest(configuration=configuration), self.assertRaises(ValueError):
                MatchConfig(configuration)
        self.assertEqual(MatchConfig().max_candidates, 20)

    def test_same_token_in_multiple_fields_is_one_hit(self):
        index = SourceIndex()
        source = unit('source.xlsx', ['a', 'b'], ['ORD000750', 'ORD000750'])
        index.add_unit(source)
        index.add_unit(source)
        self.assertEqual(index.lookup_tokens('ORD000750'), {source.unit_id: 1})
        self.assertEqual(index.stats()['total_units'], 1)

    def test_identifier_suffix_is_not_a_separate_token(self):
        index = SourceIndex()
        source = unit('source.xlsx', ['id'], ['C000750'])
        index.add_unit(source)
        self.assertEqual(index.lookup_tokens('ORD000750'), {})

    def test_embedded_chinese_name_retrievable(self):
        index = SourceIndex()
        source = unit('source.txt', ['content'], ['证人张伟华于门口发现文件'], 'txt')
        index.add_unit(source)
        self.assertIn(source.unit_id, index.lookup_tokens('张伟华'))

    def test_contradictory_key_cannot_be_overridden_by_shared_product(self):
        index = SourceIndex()
        bad = unit('orders.xlsx', ['客户ID', '产品代码'], ['C0999', 'P0042'])
        good = unit('orders.xlsx', ['客户ID', '产品代码'], ['C0001', 'P0042'], row=3)
        for source in (bad, good):
            index.add_unit(source)
        query = unit('result.xlsx', ['客户ID', '产品代码'], ['C0001', 'P0042'])
        matches = MatchEngine(index).match_result_unit(query)
        self.assertEqual([m.unit.unit_id for m in matches], [good.unit_id])

    def test_joint_mapping_preserves_actual_source_columns(self):
        index = SourceIndex()
        index.add_unit(unit('source.xlsx', ['姓名', '成交日'], ['张伟华', '2024-06-12']))
        query = unit('result.xlsx', ['客户名称', '订单日期'], ['张伟华', '2024-06-12'])
        engine = MatchEngine(index, MatchConfig({'joint_fields': [['客户名称', '订单日期']]}))
        match = engine._joint_field_match(query)[0]
        self.assertEqual(match.matched_fields, ['姓名', '成交日'])
        self.assertEqual(match.result_fields, ['客户名称', '订单日期'])

    def test_joint_requires_all_configured_fields_and_distinct_source_cells(self):
        index = SourceIndex()
        index.add_unit(unit('source.xlsx', ['only'], ['北京']))
        query = unit('result.xlsx', ['城市', '地址'], ['北京', '北京'])
        engine = MatchEngine(index, MatchConfig({'joint_fields': [['城市', '地址'], ['城市', '地址', '缺失字段']]}))
        self.assertEqual(engine._joint_field_match(query), [])
        self.assertEqual(len(engine._direct_field_match(query)[0].matched_fields), 1)

    def test_global_candidate_cap_and_stable_order(self):
        index = SourceIndex()
        for name in ('c.xlsx', 'a.xlsx', 'b.xlsx'):
            index.add_unit(unit(name, ['客户ID'], ['C0001']))
        engine = MatchEngine(index, MatchConfig({'max_candidates': 2}))
        query = unit('result.xlsx', ['客户ID'], ['C0001'])
        first = engine.match_result_unit(query)
        self.assertEqual(len(first), 2)
        self.assertEqual([m.unit.unit_id for m in first], [m.unit.unit_id for m in engine.match_result_unit(query)])

    def test_fuzzy_threshold_100_and_integral_float(self):
        index = SourceIndex()
        index.add_unit(unit('source.xlsx', ['产品', '金额'], ['高端商务笔记本', 6800.0]))
        query = unit('result.xlsx', ['产品', '金额'], ['高端商务笔记本', '6800'])
        engine = MatchEngine(index, MatchConfig({'fuzzy_threshold': 100}))
        matches = engine.match_result_unit(query)
        self.assertTrue(matches)
        self.assertIn('金额', matches[0].matched_fields)
        self.assertTrue(all(0 <= m.score <= 1 for m in engine._fuzzy_match(query)))

    def test_cached_paths_do_not_depend_on_input_spelling(self):
        from detective import DataDetective
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, result = root / 's.csv', root / 'r.csv'
            source.write_text('客户ID\nC0001\n', encoding='utf-8')
            result.write_text('客户ID\nC0001\n', encoding='utf-8')
            detective = DataDetective(cache_dir=str(root / 'cache'))
            detective.trace(str(result), 2, [str(source)])
            alias = str(root / '.' / 's.csv')
            chain = detective.trace(str(result), 2, [alias])
            self.assertFalse(any(m['type'] == 'unmatched_source' for m in chain.missing))
            self.assertEqual(len(chain.edges), 1)

    def test_same_size_same_timestamp_content_change_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text('original account', encoding='utf-8')
            before = source.stat()
            first = build_index([str(source)], str(root / 'cache'))
            source.write_text('modified account', encoding='utf-8')
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(source.stat().st_size, before.st_size)
            second = build_index([str(source)], str(root / 'cache'))
            self.assertEqual(next(iter(first.id_map.values())).raw_text, 'original account')
            self.assertEqual(next(iter(second.id_map.values())).raw_text, 'modified account')


class TestExtractionPrecision(unittest.TestCase):
    def test_amounts_are_not_dates_ids_or_phone_numbers(self):
        extractor = EntityExtractor()
        source = unit('case.txt', ['content'], ['2024-07-20 支付 ORD00000750，电话13800138000，金额2.5万元。'], 'txt')
        entities, events = extractor.extract_from_unit(source)
        amounts = [e for e in entities if e.entity_type == 'amount']
        self.assertEqual(len(amounts), 1)
        self.assertEqual(amounts[0].value, 25000)
        self.assertEqual(len([e for e in entities if e.entity_type == 'date']), 1)
        self.assertEqual(len([e for e in entities if e.entity_type == 'id']), 1)
        self.assertEqual(len(events), 1)

    def test_date_precision_clock_and_invalid_calendar(self):
        extractor = EntityExtractor()
        self.assertEqual(extractor._normalize_date('2024年7月'), '2024-07')
        self.assertEqual(extractor._normalize_date('2024-07-20 21:30:15'), '2024-07-20T21:30:15')
        self.assertIsNone(extractor._normalize_date('2024-02-31'))
        source = unit('case.txt', ['content'], ['2024-02-31 支付。'], 'txt')
        entities, events = extractor.extract_from_unit(source)
        self.assertFalse(any(e.entity_type == 'date' for e in entities))
        self.assertEqual(events, [])

    def test_payment_id_is_an_identifier_and_relation_direction_is_stable(self):
        extractor = EntityExtractor()
        source = unit('source.xlsx', ['订单ID', '客户ID', '回款ID', '回款金额', '客户姓名'],
                      ['ORD000750', 'C0001', 'PAY000076', '1.5万元', '张伟华'])
        entities, _ = extractor.extract_from_unit(source)
        ids = {e.name: e.entity_id for e in entities if e.entity_type == 'id'}
        self.assertIn('PAY000076', ids)
        self.assertEqual([e.value for e in entities if e.entity_type == 'amount'], [15000])
        relations = extractor.extract_relationships_from_row(source, entities)
        order = next(r for r in relations if r.relation_type == '下单')
        self.assertEqual((order.from_entity, order.to_entity), (ids['C0001'], ids['ORD000750']))
        person_links = [r for r in relations if r.relation_type == 'identified_by']
        self.assertEqual([r.to_entity for r in person_links], [ids['C0001']])

    def test_text_offsets_with_multiple_blank_lines_crlf_and_indent(self):
        text = '  第一段\r\n\r\n\r\n  第二段\r\n下一行\r\n\r\n第三段'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'case.txt'
            path.write_bytes(text.encode('utf-8'))
            units = TextParser().parse(str(path))
            self.assertEqual([u.row_num for u in units], [1, 4, 7])
            for source in units:
                self.assertEqual(text[source.char_offset:source.char_offset + len(source.raw_text)], source.raw_text)

    def test_missing_cells_and_literal_na_preserve_identity(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'source.xlsx'
            pd.DataFrame({'id': ['001', 'NA'], 'value': [None, 4]}).to_excel(path, index=False)
            units = ExcelParser().parse(str(path))
            self.assertEqual(units[0].field_values, ['001', None])
            self.assertEqual(units[1].field_values, ['NA', 4])
            csv = root / 'source.csv'
            csv.write_text('id,value\n001,\nNA,4\n', encoding='utf-8')
            units = CSVParser().parse(str(csv))
            self.assertEqual(units[0].field_values, ['001', None])
            self.assertEqual(units[1].field_values, ['NA', '4'])

    def test_result_sheet_must_resolve_ambiguous_rows(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'multi.xlsx'
            with pd.ExcelWriter(path) as writer:
                pd.DataFrame({'id': ['C0001']}).to_excel(writer, sheet_name='Old', index=False)
                pd.DataFrame({'id': ['C0002']}).to_excel(writer, sheet_name='Current', index=False)
            parser = FileParser()
            with self.assertRaisesRegex(ValueError, 'sheet'):
                parser.parse_result_row(str(path), 2)
            self.assertEqual(parser.parse_result_row(str(path), 2, sheet_name='Current').get_field('id'), 'C0002')




if __name__ == '__main__':
    unittest.main()
