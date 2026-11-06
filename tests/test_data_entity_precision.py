"""Observable source/identity regressions across heterogeneous records."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from parsers import AddressableUnit
from indexer import SourceIndex
from matcher import MatchConfig, MatchEngine
from linker import ChainLinker, validate_chain
from entity_extractor import EntityExtractor


def unit(name, fields, values, row=2, kind='csv', metadata=None):
    return AddressableUnit(f'{name}-{row}', os.path.abspath(name), kind,
                           row, None, None, 0, None, fields, values,
                           ' | '.join(f'{key}={value}' for key, value in zip(fields, values)),
                           metadata=metadata or {})


class CandidatePrecision(unittest.TestCase):
    def index(self, *records):
        index = SourceIndex()
        for record in records:
            index.add_unit(record)
        return index

    def test_different_identifier_roles_are_not_exact_key_matches(self):
        source = unit('source.csv', ['员工ID'], ['00123'])
        query = unit('result.csv', ['客户ID'], ['00123'])
        matches = MatchEngine(self.index(source)).match_result_unit(query)
        self.assertFalse(any(match.match_type == 'exact_id' for match in matches))

    def test_explicit_alias_preserves_zeros_and_checks_conflicting_key(self):
        good = unit('source.csv', ['client_ref', '金额'], ['00123', '100'])
        bad = unit('source.csv', ['client_ref', '金额'], ['00124', '100'], row=3)
        query = unit('result.csv', ['客户ID', '金额'], ['00123', '100'])
        engine = MatchEngine(self.index(bad, good), MatchConfig({'field_aliases': {'客户ID': ['client_ref']}}))
        matches = engine.match_result_unit(query)
        self.assertEqual([match.unit.unit_id for match in matches], [good.unit_id])
        self.assertEqual(matches[0].match_type, 'exact_id')
        self.assertEqual(engine.last_search_stats['excluded_key_conflicts'], 1)

    def test_renamed_chinese_key_conflict_cannot_hide_behind_shared_amount(self):
        source = unit('source.csv', ['客户编号', '金额'], ['C002', '100'])
        query = unit('result.csv', ['客户ID', '金额'], ['C001', '100'])
        self.assertEqual(MatchEngine(self.index(source)).match_result_unit(query), [])

    def test_id_substring_in_ordinary_column_is_not_a_key(self):
        source = unit('source.csv', ['side', '订单ID'], ['left', 'ORD0001'])
        query = unit('result.csv', ['side', '订单ID'], ['right', 'ORD0001'])
        matches = MatchEngine(self.index(source)).match_result_unit(query)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].match_type, 'exact_id')

    def test_narrative_starting_with_an_id_is_not_an_id_field(self):
        source = unit('source.csv', ['说明'], ['客户C0001 记录待核实'])
        query = unit('result.csv', ['说明'], ['客户C0001 记录待核实'])
        self.assertFalse(any(match.match_type == 'exact_id' for match in
                             MatchEngine(self.index(source)).match_result_unit(query)))

    def test_document_id_mention_is_retrieved_with_literal_boundary(self):
        good = unit('record.md', ['content'], ['该文件记录客户 C000123 的服务条款及其他说明。'], kind='md')
        bad = unit('record.md', ['content'], ['该文件记录客户 C0001239 的服务条款。'], row=3, kind='md')
        query = unit('result.csv', ['客户ID'], ['C000123'])
        matches = MatchEngine(self.index(good, bad)).match_result_unit(query)
        self.assertEqual([match.unit.unit_id for match in matches], [good.unit_id])
        self.assertEqual(matches[0].match_type, 'indirect')

    def test_same_file_alternatives_survive_graph_building(self):
        records = [unit('source.csv', ['客户ID', '订单ID'], ['C0001', f'ORD000{i}'], row=i+2) for i in range(3)]
        query = unit('result.csv', ['客户ID'], ['C0001'])
        chain = ChainLinker(self.index(*records)).build_chain(query, [records[0].source_file])
        sources = [node for node in chain.nodes if node.node_type != 'result']
        self.assertEqual({node.row_num for node in sources}, {2, 3, 4})
        self.assertEqual(len(chain.edges), 3)
        self.assertEqual(len(chain.evidence), 3)
        self.assertEqual(chain.coverage['returned_candidates'], 3)
        self.assertTrue(chain.conflicts)

    def test_candidate_cap_is_reported_when_only_one_row_can_be_returned(self):
        records = [unit('source.csv', ['客户ID'], ['C0001'], row=i+2) for i in range(5)]
        query = unit('result.csv', ['客户ID'], ['C0001'])
        chain = ChainLinker(self.index(*records), MatchConfig({'max_candidates': 1})).build_chain(query, [records[0].source_file])
        self.assertEqual(chain.coverage['eligible_candidates'], 5)
        self.assertEqual(chain.coverage['omitted_candidates'], 4)
        self.assertFalse(chain.coverage['search_complete'])
        self.assertTrue(any(item['type'] == 'candidate_truncated' for item in chain.missing))

    def test_file_omitted_by_cap_is_not_reported_as_no_match(self):
        records = [unit(name, ['客户ID'], ['C0001']) for name in ('a.csv', 'b.csv')]
        query = unit('result.csv', ['客户ID'], ['C0001'])
        chain = ChainLinker(self.index(*records), MatchConfig({'max_candidates': 1})).build_chain(query, [u.source_file for u in records])
        self.assertTrue(any(item['type'] == 'source_candidates_omitted' for item in chain.missing))
        self.assertFalse(any(item['type'] == 'unmatched_source' for item in chain.missing))

    def test_token_overlap_does_not_invent_column_correspondence(self):
        source = unit('source.csv', ['订单ID', '内容'], ['ORD0001', 'alpha beta'])
        query = unit('result.csv', ['客户ID', '备注'], ['C0001', 'alpha beta'])
        engine = MatchEngine(self.index(source), MatchConfig({'semantic_threshold': 0}))
        candidates = engine._token_semantic_match(query)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].matched_fields, [])
        self.assertEqual(candidates[0].result_fields, [])

    def test_repeated_retrieval_does_not_increase_hypothesis_confidence(self):
        source = unit('source.csv', ['客户ID'], ['C0001'])
        query = unit('result.csv', ['客户ID'], ['C0001'])
        chain = ChainLinker(self.index(source)).build_chain(query, [source.source_file])
        compared = validate_chain(chain, {'confidence': 0.7, 'supporting_files': [source.source_file]})
        self.assertEqual(compared['new_confidence'], 0.7)
        self.assertEqual(compared['validation_status'], 'requires_review')


class EntityPrecision(unittest.TestCase):
    def test_structured_sql_or_document_rows_use_field_roles(self):
        source = unit('dump.sql', ['customer_id', 'customer_name', 'note'],
                      ['00123', 'Alice Smith', '原始备注'], kind='sql', metadata={'structured': True})
        entities, _ = EntityExtractor().extract_from_unit(source)
        self.assertEqual([(e.entity_type, e.value) for e in entities], [('id', '00123'), ('person', 'Alice Smith')])

    def test_date_or_amount_words_inside_identifier_labels_remain_ids(self):
        source = unit('source.csv', ['update_id', 'payment_amount_id'], ['UPD0001', 'AMT0001'])
        entities, _ = EntityExtractor().extract_from_unit(source)
        self.assertEqual([entity.entity_type for entity in entities], ['id', 'id'])

    def test_phone_is_extracted_with_offset_and_not_embedded_number(self):
        source = unit('record.txt', ['content'], ['电话13800138000；无效附加位138001380009'], kind='txt')
        source.char_offset = 100
        entities, _ = EntityExtractor().extract_from_unit(source)
        phones = [entity for entity in entities if entity.entity_type == 'phone']
        self.assertEqual([entity.value for entity in phones], ['13800138000'])
        self.assertEqual(phones[0].char_offset, 100 + source.raw_text.index('13800138000'))
        self.assertIn('char_offset', phones[0].to_dict())

    def test_row_roles_have_no_uncalibrated_relation_probability(self):
        extractor = EntityExtractor()
        source = unit('source.csv', ['客户ID', '订单ID'], ['C0001', 'ORD0001'])
        entities, _ = extractor.extract_from_unit(source)
        relations = extractor.extract_relationships_from_row(source, entities)
        self.assertTrue(relations)
        self.assertIsNone(relations[0].to_dict()['confidence'])
        self.assertEqual(relations[0].to_dict()['verification'], 'unverified')


if __name__ == '__main__':
    unittest.main()
