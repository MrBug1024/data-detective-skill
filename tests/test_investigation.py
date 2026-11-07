"""Evidence invariants: no invented certainty, no lost provenance or causal edges."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from investigator import Investigator, Investigation
from read_evidence import evidence_slice
from parsers import FileParser, CSVParser


class InvestigationInvariants(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.inv = Investigator(cache_dir=str(self.folder / "cache"))

    def file(self, name, content):
        path = self.folder / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def run_quiet(self, method, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return method(*args).to_dict()

    def test_empty_mentions_are_not_half_certain(self):
        path = self.file("names.csv", "姓名,客户ID\n李四,C001\n")
        report = self.run_quiet(self.inv.investigate_entity, "张三", [path])
        self.assertIsNone(report["confidence"])
        self.assertEqual(report["evidence"], [])
        self.assertEqual(report["entities"], [])

    def test_same_name_different_identifiers_not_merged(self):
        path = self.file("names.csv", "姓名,客户ID,城市\n张三,C001,北京\n张三,C002,上海\n")
        report = self.run_quiet(self.inv.investigate_entity, "张三", [path])
        self.assertEqual(len(report["evidence"]), 2)
        self.assertEqual(report["entities"][0]["identity_status"], "unresolved")
        self.assertEqual(report["conflicts"][0]["candidates"], ["C001", "C002"])
        self.assertEqual(len(report["entities"][0]["attribute_claims"]["城市"]), 2)

    def test_id_substrings_not_mentions(self):
        path = self.file("ids.csv", "客户ID,说明\nC0019,关于C0019记录\n")
        report = self.run_quiet(self.inv.investigate_entity, "C001", [path])
        self.assertEqual(report["evidence"], [])

    def test_row_relationships_are_cited_claims(self):
        path = self.file("orders.csv", "姓名,客户ID,订单ID\n张三,C000001,ORD00001\n")
        report = self.run_quiet(self.inv.investigate_entity, "张三", [path])
        self.assertTrue(report["relationships"])
        ids = {e["evidence_id"] for e in report["evidence"]}
        for relation in report["relationships"]:
            self.assertIsNone(relation["confidence"])
            self.assertEqual(relation["verification"], "unverified")
            self.assertTrue(set(relation["evidence_ids"]) <= ids)

    def test_source_changed_between_calls_is_not_stale(self):
        path = self.file("names.csv", "姓名\n李四\n")
        self.run_quiet(self.inv.investigate_entity, "张三", [path])
        Path(path).write_text("姓名\n张三\n赵六\n", encoding="utf-8")
        report = self.run_quiet(self.inv.investigate_entity, "张三", [path])
        self.assertEqual(len(report["evidence"]), 1)



    def test_same_timestamp_different_source_events_preserved(self):
        items = [{"time": "2025-01-01", "event": "进入", "source": "a"},
                 {"time": "2025-01-01", "event": "进入", "source": "b"},
                 {"time": "2025-01-01", "event": "离开", "source": "a"}]
        self.assertEqual(len(Investigator._timeline(items)), 3)

    def test_missing_source_does_not_disappear(self):
        report = self.run_quiet(self.inv.investigate_entity, "ORDER-123", [str(self.folder / "missing.txt")])
        self.assertTrue(any(m["type"] == "missing_source" for m in report["missing"]))

    def test_parse_failure_cannot_become_empty_negative_result(self):
        path = self.file("rows.csv", "姓名\n张三\n")
        with patch.object(CSVParser, "parse", side_effect=ImportError("missing test dependency")):
            with self.assertRaises(RuntimeError):
                self.run_quiet(self.inv.investigate_entity, "张三", [path])
        self.assertEqual(list((self.folder / "cache").glob("*.pkl")), [])

    def test_csv_literal_ids_quotes_and_ambiguous_columns(self):
        path = self.file("literal.csv", '\ufeff客户ID,说明,备注\n00023,"逗号,与换行\n均在同格",NA\n00024,,nan\n')
        units = CSVParser().parse(path)
        source_newline = "\r\n" if b"\r\n" in Path(path).read_bytes() else "\n"
        self.assertEqual(units[0].field_values, ["00023", "逗号,与换行" + source_newline + "均在同格", "NA"])
        self.assertEqual(units[1].field_values, ["00024", None, "nan"])
        for name, content in (("dup.csv", "ID,ID\n1,2\n"), ("wide.csv", "ID\n1,2\n")):
            with self.assertRaises(RuntimeError):
                FileParser().parse(self.file(name, content))

    def test_report_timestamp_stable_and_sources_roundtrip(self):
        report = Investigation()
        report._source_files = ["a.csv"]
        path = self.folder / "report.json"
        report.save_json(path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["timestamp"], report.to_dict()["timestamp"])
        self.assertTrue(saved["timestamp"].endswith("+00:00"))
        self.assertEqual(saved["_source_files"], ["a.csv"])




class EvidencePaging(unittest.TestCase):
    def test_paging_exposes_coverage_and_missing_ids(self):
        report = {"coverage": {"omitted_units": 9},
                  "evidence": [{"evidence_id": f"e{i}"} for i in range(4)]}
        page = evidence_slice(report, limit=2)
        self.assertEqual(page["next_start"], 2)
        self.assertEqual(page["evidence_total"], 4)
        self.assertEqual(page["coverage"]["omitted_units"], 9)
        last = evidence_slice(report, start=2, limit=2)
        self.assertIsNone(last["next_start"])
        target = evidence_slice(report, ids=["e3", "invented"])
        self.assertEqual(target["evidence"], [{"evidence_id": "e3"}])
        self.assertEqual(target["requested_ids_not_found"], ["invented"])


if __name__ == "__main__":
    unittest.main()
