import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from decimal import Inexact

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from investigator import Investigator
from reconcile import verify_plan, PlanError


class DataEntityCompletion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, name, text):
        p = self.root / name
        p.write_text(text, encoding='utf-8')
        return str(p)

    def test_groups_person_ids_preserve_claims_not_order_conflicts(self):
        p = self.file('people.csv', '姓名,客户ID,订单ID,电话\n张三,0001,O1,12345\n张三,0001,O2,12345\n张三,0002,O3,12345\n')
        with contextlib.redirect_stdout(io.StringIO()):
            data = Investigator(cache_dir=str(self.root/'cache')).investigate_entity('张三', [p]).to_dict()
        self.assertEqual(len(data['candidate_entities']), 2)
        self.assertEqual(sorted(len(g['evidence_ids']) for g in data['candidate_entities']), [1, 2])
        self.assertEqual({c['field'] for c in data['conflicts']}, {'客户ID'})
        self.assertTrue(all(g['identity_status'] != 'verified' for g in data['candidate_entities']))
        self.assertEqual(len(data['provenance']['source_sha256'][p]), 64)

    def test_entity_retrievable_paging_and_text_truncation(self):
        p = self.file('mentions.txt', '\n\n'.join('张三' + '说明'*120 for _ in range(3)))
        with contextlib.redirect_stdout(io.StringIO()):
            data = Investigator({'entity_max_units': 1, 'entity_start': 1, 'entity_max_chars': 100},
                cache_dir=str(self.root/'cache')).investigate_entity('张三', [p]).to_dict()
        self.assertEqual(data['coverage']['matched_units'], 3)
        self.assertEqual(data['coverage']['next_start'], 2)
        self.assertEqual(data['coverage']['selection_start'], 1)
        self.assertEqual(len(data['coverage']['truncated_evidence_ids']), 1)
        self.assertEqual(len(data['evidence'][0]['matched_value']), 100)

    def test_structured_longer_name_is_not_same_name_mention(self):
        p = self.file('names.csv', '姓名,客户ID\n张三丰,0001\n张三,0002\n')
        with contextlib.redirect_stdout(io.StringIO()):
            data = Investigator(cache_dir=str(self.root/'cache')).investigate_entity('张三', [p]).to_dict()
        self.assertEqual(data['coverage']['matched_units'], 1)
        self.assertEqual(data['evidence'][0]['source_location']['row'], 3)

    def test_document_type_is_not_identity_identifier(self):
        p = self.file('names.csv', '姓名,证件类型,电话\n张三,身份证,111\n张三,身份证,222\n')
        with contextlib.redirect_stdout(io.StringIO()):
            data = Investigator(cache_dir=str(self.root/'cache')).investigate_entity('张三', [p]).to_dict()
        self.assertEqual(data['identity_grouping']['identifier_group_count'], 0)
        self.assertEqual(data['identity_grouping']['unresolved_record_count'], 2)

    def make_plan(self):
        self.file('result.csv', '订单ID,总额,名称\n0001,16.55,示例\n')
        self.file('detail.csv', '订单ID,产品ID,数量\n0001,S1,2\n0001,S2,3\n9999,S1,100\n')
        self.file('prices.csv', '产品ID,单价\nS1,2.125\nS2,4.10\n')
        self.file('rules.md', '规则：按订单ID筛选明细，按产品ID连接唯一单价，总额为数量乘单价之和，最后保留两位小数四舍五入。')
        return {'version': 1, 'result': {'file': 'result.csv', 'row': 2},
            'sources': {'d': {'file': 'detail.csv'}, 'p': {'file': 'prices.csv'}},
            'select': {'source': 'd', 'where': [{'field': '订单ID', 'result_field': '订单ID'}]},
            'joins': [{'source': 'p', 'left': {'source': 'd', 'field': '产品ID'},
                       'right_field': '产品ID', 'cardinality': 'many_to_one'}],
            'outputs': [{'result_field': '总额', 'aggregate': 'sum', 'expression': {'mul': [
                {'source': 'd', 'field': '数量'}, {'source': 'p', 'field': '单价'}]},
                'quantize': '0.01', 'rounding': 'ROUND_HALF_UP', 'tolerance': '0'}],
            'rule_evidence': [{'file': 'rules.md', 'quote': '按产品ID连接唯一单价'}]}

    def verify(self, plan):
        with contextlib.redirect_stdout(io.StringIO()):
            return verify_plan(plan, self.root)

    def test_join_aggregate_decimal_and_locations(self):
        report = self.verify(self.make_plan())
        self.assertEqual(report['status'], 'reconciled_not_proven_lineage')
        self.assertEqual(report['checks'][0]['calculated'], '16.55')
        self.assertEqual(report['checks'][0]['difference'], '0.00')
        self.assertEqual(len(report['source_rows']), 4)
        self.assertEqual({r['location']['row'] for r in report['source_rows']}, {2, 3})

    def test_ruleless_equal_is_incomplete_not_verified(self):
        plan = self.make_plan()
        plan.pop('rule_evidence')
        report = self.verify(plan)
        self.assertEqual(report['checks'][0]['status'], 'equal')
        self.assertEqual(report['status'], 'incomplete')

    def test_mismatch_is_reported_with_delta(self):
        plan = self.make_plan()
        self.file('result.csv', '订单ID,总额\n0001,16.54\n')
        report = self.verify(plan)
        self.assertEqual(report['status'], 'mismatch')
        self.assertEqual(report['checks'][0]['difference'], '0.01')

    def test_duplicate_or_missing_join_cannot_inflate_or_drop_rows(self):
        plan = self.make_plan()
        for text in ('产品ID,单价\nS1,2.125\nS1,2.125\nS2,4.10\n', '产品ID,单价\nS1,2.125\n'):
            self.file('prices.csv', text)
            with self.assertRaises(PlanError): self.verify(plan)

    def test_empty_ambiguous_and_fabricated_rules(self):
        plan = self.make_plan()
        plan['outputs'][0]['aggregate'] = 'scalar'
        self.assertEqual(self.verify(plan)['checks'][0]['status'], 'ambiguous_source_rows')
        plan['select']['where'][0] = {'field': '订单ID', 'literal': '1'}
        self.assertEqual(self.verify(plan)['checks'][0]['status'], 'no_source_rows')
        plan['rule_evidence'][0]['quote'] = '不存在的依据'
        with self.assertRaises(PlanError): self.verify(plan)

    def test_budget_and_no_executable_expressions(self):
        plan = self.make_plan()
        plan['max_join_rows'] = 1
        with self.assertRaises(PlanError): self.verify(plan)

    def test_numeric_precision_loss_cannot_become_equal(self):
        plan = self.make_plan()
        plan['outputs'][0]['expression'] = {'div': [{'literal': '1'}, {'literal': '3'}]}
        with self.assertRaises(Inexact): self.verify(plan)

    def test_explicit_rounding_and_equivalent_quantum(self):
        plan = self.make_plan()
        self.file('result.csv', '订单ID,总额\n0001,1.23\n')
        output = plan['outputs'][0]
        output['expression'] = {'literal': '0.617'}
        output['quantize'] = '0.010'
        report = self.verify(plan)
        self.assertEqual(report['status'], 'reconciled_not_proven_lineage')
        self.assertEqual(report['checks'][0]['calculated'], '1.23')

    def test_cli_json_decimal_literal_preserves_all_digits(self):
        plan = self.make_plan()
        plan['outputs'] = [{'result_field': '总额', 'aggregate': 'scalar',
                            'expression': {'literal': '0.1234567890123456789'}}]
        plan['select']['where'].append({'field': '产品ID', 'literal': 'S1'})
        self.file('result.csv', '订单ID,总额\n0001,0.12345678901234568\n')
        raw = json.dumps(plan, ensure_ascii=False).replace('"0.1234567890123456789"', '0.1234567890123456789')
        path = Path(self.file('plan.json', raw))
        out = self.root/'check.json'
        run = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/reconcile.py'),
                              '--plan', str(path), '--out', str(out)], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'mismatch')
        self.assertEqual(report['checks'][0]['calculated'], '0.1234567890123456789')

    def test_invalid_numeric_source_has_actionable_failure(self):
        plan = self.make_plan()
        self.file('prices.csv', '产品ID,单价\nS1,unknown\nS2,4.10\n')
        with self.assertRaisesRegex(PlanError, '十进制'):
            self.verify(plan)
        plan['max_join_rows'] = 100
        plan['outputs'][0]['expression'] = {'eval': '__import__("os").system("echo NO")'}
        with self.assertRaises(PlanError): self.verify(plan)


if __name__ == '__main__':
    unittest.main()
