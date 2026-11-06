"""Run the real CLI on synthetic multi-format evidence, then score locally.

This evaluates deterministic ingestion/retrieval behavior. It is not an LLM
reasoning benchmark or a real-world accuracy estimate. Raw CLI reports and
failure output are retained independently of the expected results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from build_fixtures import build

REPO = Path(__file__).resolve().parents[2]


def flatten(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from flatten(item)
    elif isinstance(value, list):
        for item in value:
            yield from flatten(item)


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def source_nodes(report):
    return [n for n in report.get("chain", {}).get("nodes", []) if n.get("node_type") != "result"]


def source_named(value, filename):
    return isinstance(value, str) and Path(value).name == filename


def has_locator(report, key, expected):
    aliases = {"row": {"row", "row_num", "line_start"}, "page": {"page", "page_num"},
               "sheet": {"sheet", "sheet_name"}, "table": {"table", "table_index", "table_num"},
               "bbox": {"bbox"}}[key]
    for name, value in flatten([report.get("source_locations", []), report.get("evidence", []), source_nodes(report)]):
        if name not in aliases:
            continue
        if key == "table" and type(value) is int and value >= 1:
            return True
        if key == "bbox" and value == [20, 20, 760, 100]:
            return True
        if key not in ("table", "bbox") and value == expected:
            return True
    return False


class Evaluation:
    def __init__(self, root, python, selected=()):
        self.root = Path(root).resolve()
        self.public = self.root / "public"
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.python = python
        self.selected = set(selected)
        self.records = []
        self.manifest = build(self.public)
        self.expected = json.loads((Path(__file__).parent / "private" / "expected.json").read_text(encoding="utf-8"))
        self.started = time.monotonic()

    def cli(self, case, target="ORD000417", sources=(), result=None, config=None):
        output = self.raw / f"{case}.json"
        args = [self.python, str(REPO / "scripts" / "detective.py")]
        if result:
            args += ["trace", "--result", str(self.public / result), "--row", "2"]
        else:
            args += ["investigate", "--mode", "entity", "--target", target]
        args += ["--sources"] + [str(self.public / source) for source in sources]
        args += ["--out", str(output), "--no-html", "--cache-dir", str(self.root / "cache")]
        if config:
            config_path = self.raw / f"{case}.config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            args += ["--config", str(config_path)]
        completed = subprocess.run(args, cwd=REPO, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
        (self.raw / f"{case}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (self.raw / f"{case}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
        report = json.loads(output.read_text(encoding="utf-8")) if completed.returncode == 0 and output.is_file() else None
        return completed, report

    def entity(self, case, filename, target="ORD000417", config=None):
        completed, report = self.cli(case, target=target, sources=[filename], config=config)
        expect(completed.returncode == 0, f"CLI exited {completed.returncode}; inspect raw stderr")
        expect(report is not None, "successful CLI did not produce JSON report")
        return report

    def case(self, name, action, required=()):
        if self.selected and name not in self.selected:
            return
        started = time.monotonic()
        missing = [f for f in required if f in self.manifest["unavailable"]]
        entry = {"case_id": name}
        if missing:
            entry.update(status="unavailable", detail="fixture dependency unavailable: " + ", ".join(missing))
        else:
            try:
                entry["observed"] = action()
                entry["status"] = "passed"
            except Exception as exc:
                entry.update(status="failed", detail=f"{type(exc).__name__}: {exc}")
                (self.raw / f"{name}.failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        entry["elapsed_seconds"] = round(time.monotonic() - started, 4)
        self.records.append(entry)
        self.save()
        print(f"{name}: {entry['status']}" + ("; " + entry["detail"] if entry.get("detail") else ""), flush=True)

    def format_case(self, case):
        report = self.entity(case["id"], case["file"])
        evidence = report.get("evidence", [])
        expect(bool(evidence), "target mention not retrieved")
        expect(all(source_named(e.get("source_location", {}).get("file"), case["file"]) for e in evidence), "evidence location does not point to the original input file")
        expect(all(e.get("verification") != "verified" for e in evidence), "copied mention was represented as verified identity evidence")
        for key, value in case["locator"].items():
            expect(has_locator(report, key, value), f"original {key} locator missing or wrong; expected {value!r}")
        if case["file"] in ("records.csv", "records.xlsx", "dump.sql"):
            expect(any("000417" in n.get("field_values", []) for n in source_nodes(report)), "literal identifier with leading zeroes was lost")
        if case["file"] == "receipt.png":
            expect(report.get("coverage", {}).get("ingestion_complete") is False, "unverified OCR was represented as complete original evidence")
            expect(any(key == "ocr_verified" and value is False for key, value in flatten(report)), "OCR provenance lacks its unverified transcription state")
        return {"evidence_count": len(evidence), "source_locations": report.get("source_locations", [])}

    def data_case(self, name, source, result="result.csv"):
        completed, report = self.cli(name, sources=[source], result=result)
        expect(completed.returncode == 0 and report is not None, "trace CLI did not produce a report")
        expect(report.get("analysis_stage") == "candidate_links", "candidate search unexpectedly represented as completed verified lineage")
        return report

    def run(self):
        for case in self.expected["format_cases"]:
            self.case(case["id"], lambda c=case: self.format_case(c), [case["file"]])

        def d01():
            report = self.data_case("D01_same_amount_keys", "records.csv")
            rows = [n.get("row_num") for n in source_nodes(report)]
            expect(rows == [2], f"same amount/name overrode a contradictory stable key: rows={rows}")
            expect(all(c.get("status") != "confirmed" for c in report.get("conclusions", [])), "candidate match became confirmed lineage")
            return {"source_rows": rows}
        self.case("D01_same_amount_keys", d01)

        def d02():
            report = self.data_case("D02_literal_zero_id", "zero_sources.csv", "zero_result.csv")
            rows = [n.get("row_num") for n in source_nodes(report)]
            expect(rows == [3], f"literal IDs 417 and 000417 were conflated: rows={rows}")
            return {"source_rows": rows}
        self.case("D02_literal_zero_id", d02)

        def d03():
            report = self.data_case("D03_duplicate_key_conflict", "duplicate_key.csv")
            expect(bool(report.get("conflicts")), "duplicate stable key with different values was silently collapsed")
            values = [v for _, v in flatten(report["conflicts"])]
            expect(3 in values and 2 in values, "conflict does not preserve both source row addresses")
            return {"conflicts": report["conflicts"]}
        self.case("D03_duplicate_key_conflict", d03)

        def d04():
            inputs = [c["file"] for c in self.expected["format_cases"]]
            completed, report = self.cli("D04_mixed_format_trace", sources=inputs, result="result.csv")
            expect(completed.returncode == 0 and report is not None, "mixed-format trace CLI failed")
            included = {Path(n["source_file"]).name for n in source_nodes(report)}
            expect(included == set(inputs), f"mixed-format candidate trace omitted sources: {sorted(set(inputs) - included)}")
            expect(all(c.get("status") != "confirmed" for c in report.get("conclusions", [])), "copied cross-format records became confirmed lineage")
            return {"included_sources": sorted(included), "evidence_count": len(report.get("evidence", []))}
        self.case("D04_mixed_format_trace", d04, [c["file"] for c in self.expected["format_cases"]])

        def e01():
            report = self.entity("E01_same_name_shared_phone", "people.csv", "陈宁")
            expect(len(report.get("evidence", [])) == 2, "same-name mentions lost or shared-phone nonmatch included")
            conflicts = [c for c in report.get("conflicts", []) if c.get("type") == "identity_unresolved"]
            expect(any(set(c.get("candidates", [])) == {"P0001", "P0002"} for c in conflicts), "different person IDs were not preserved as identity ambiguity")
            targets = [e for e in report.get("entities", []) if e.get("entity_id") == "target_entity"]
            expect(targets and targets[0].get("identity_status") == "unresolved", "name/shared phone wrongly resolved identity")
            claims = targets[0].get("attribute_claims", {})
            expect({c["value"] for c in claims.get("城市", [])} == {"北京", "上海"}, "later same-name attribute overwrote earlier source claim")
            return {"evidence_count": 2, "identity_conflicts": conflicts, "candidate_entities": report.get("candidate_entities", [])}
        self.case("E01_same_name_shared_phone", e01)

        def e02():
            report = self.entity("E02_negated_relationship", "denial.txt", "陈宁")
            expect(len(report.get("evidence", [])) == 1, "denial was dropped rather than preserved as evidence")
            for relation in report.get("relationships", []):
                expect(relation.get("verification") != "verified" and relation.get("status") != "confirmed", "negated ownership claim became a confirmed relationship")
                expect(relation.get("relation_type") not in ("owns", "owner", "所有权", "持股", "拥有"), "negated ownership text became a positive ownership edge")
            return {"relationships": report.get("relationships", []), "evidence_count": len(report["evidence"])}
        self.case("E02_negated_relationship", e02)

        def e03():
            completed, report = self.cli("E03_bounded_coverage", "陈宁", ["many.csv", "many.csv"], config={"entity_max_units": 3})
            expect(completed.returncode == 0 and report is not None, "bounded entity search failed")
            coverage = report.get("coverage", {})
            expect((coverage.get("matched_units"), coverage.get("included_units"), coverage.get("omitted_units")) == (7, 3, 4), "duplicate path or truncation produced inaccurate coverage")
            expect(len(report.get("evidence", [])) == 3, "bounded output size is incorrect")
            expect(any(m.get("type") == "context_truncated" for m in report.get("missing", [])), "omitted records are not actionable in missing items")
            return {"coverage": coverage}
        self.case("E03_bounded_coverage", e03)

        def i01():
            completed, report = self.cli("I01_missing_source", sources=["records.csv", "does-not-exist.csv"])
            expect(completed.returncode != 0 or any(m.get("type") == "missing_source" for m in (report or {}).get("missing", [])), "missing source silently treated as a fully searched source")
            if report is not None:
                expect(report.get("coverage", {}).get("ingestion_complete") is False, "missing source did not lower ingestion coverage")
            return {"returncode": completed.returncode, "missing": (report or {}).get("missing", [])}
        self.case("I01_missing_source", i01)

        def i02():
            completed, report = self.cli("I02_malformed_source", target="陈宁", sources=["malformed.csv"])
            if completed.returncode == 0:
                expect(report is not None and bool(report.get("missing")), "malformed CSV silently accepted as a successful empty source")
                expect(not report.get("evidence"), "malformed columns became misattributed evidence")
                expect(any(m.get("type") in ("parse_error", "ingestion_failed", "source_parse_error") for m in report["missing"]), "malformed source has no explicit parse-failure diagnosis")
            else:
                expect("malformed.csv" in completed.stderr and ("ValueError" in completed.stderr or "RuntimeError" in completed.stderr), "negative case failed for an unrelated reason rather than rejecting its malformed source")
            return {"returncode": completed.returncode, "missing": (report or {}).get("missing", [])}
        self.case("I02_malformed_source", i02)

        def i03():
            completed, report = self.cli("I03_image_without_ocr", sources=["without_ocr.png"])
            if completed.returncode == 0:
                expect(report.get("coverage", {}).get("ingestion_complete") is False, "image without OCR silently treated as fully searched")
                expect(any(key == "code" and value == "image_needs_ocr" for key, value in flatten(report.get("ingestion", {}))), "image without OCR has no actionable extraction diagnosis")
            else:
                expect("without_ocr.png" in completed.stderr, "unprocessed image case failed for an unrelated reason")
            expect(not (report or {}).get("evidence"), "unread image invented evidence")
            return {"returncode": completed.returncode, "missing": (report or {}).get("missing", [])}
        self.case("I03_image_without_ocr", i03, ["receipt.png"])

        def c01():
            first = self.entity("C01_before", "cache.txt", "OLD000417")
            expect(bool(first.get("evidence")), "initial cache target not retrieved")
            path = self.public / "cache.txt"
            before = path.stat()
            path.write_text("NEW000417 陈宁\n", encoding="utf-8", newline="")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            expect(path.stat().st_size == before.st_size, "cache fixture did not preserve size")
            second = self.entity("C01_after", "cache.txt", "NEW000417")
            expect(len(second.get("evidence", [])) == 1, "same-stat changed source reused stale parsed content")
            expect(second["evidence"][0]["matched_value"] == "NEW000417 陈宁", "cached text does not match current source snapshot")
            return {"same_mtime_ns": path.stat().st_mtime_ns == before.st_mtime_ns, "same_size": True}
        self.case("C01_same_stat_content_change", c01)

        def c02():
            first = self.entity("C02_before", "receipt.png")
            expect(bool(first.get("evidence")), "initial image sidecar target not retrieved")
            sidecar_path = self.public / "receipt.png.ocr.json"
            original = sidecar_path.read_text(encoding="utf-8")
            try:
                sidecar = json.loads(original)
                sidecar["pages"][0]["regions"][0]["text"] = "order_id=ORD000418 customer_id=000417"
                sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
                second = self.entity("C02_after", "receipt.png", "ORD000418")
                expect(len(second.get("evidence", [])) == 1, "changed OCR sidecar reused stale cached transcription")
                expect(second["evidence"][0]["matched_value"] == "order_id=ORD000418 customer_id=000417", "new OCR transcription not represented faithfully")
            finally:
                sidecar_path.write_text(original, encoding="utf-8")
            return {"transcription_updated": True, "original_image_unchanged": True}
        self.case("C02_sidecar_change", c02, ["receipt.png"])

        def c03():
            sidecar_path = self.public / "receipt.png.ocr.json"
            original = sidecar_path.read_text(encoding="utf-8")
            try:
                sidecar = json.loads(original)
                sidecar["source_sha256"] = "0" * 64
                sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
                completed, report = self.cli("C03_sidecar_source_mismatch", sources=["receipt.png"])
                expect(completed.returncode != 0, "OCR transcription bound to another image was silently trusted")
                expect(report is None and "receipt.png" in completed.stderr, "source-hash mismatch did not fail at the actual input")
            finally:
                sidecar_path.write_text(original, encoding="utf-8")
            return {"mismatched_source_hash_rejected": True}
        self.case("C03_sidecar_source_mismatch", c03, ["receipt.png"])

        def f09():
            report = self.entity("F09_multiline_csv", "multiline.csv", "陈宁")
            rows = sorted(loc.get("row") for loc in report.get("source_locations", []))
            expect(rows == [2, 4], f"multiline CSV logical record numbers were mistaken for physical rows: {rows}")
            return {"physical_rows": rows}
        self.case("F09_multiline_csv", f09)

        def f10():
            report = self.entity("F10_gb18030_csv", "gb18030.csv", "陈宁")
            expect(len(report.get("evidence", [])) == 1, "GB18030 source not retrieved")
            expect(has_locator(report, "row", 2), "GB18030 source physical row missing")
            return {"evidence_count": len(report["evidence"])}
        self.case("F10_gb18030_csv", f10)

        def reconcile(case, result_file, price_file):
            plan = {
                "version": 1,
                "result": {"file": result_file, "row": 2},
                "sources": {"line": {"file": "billing.sql", "table": "billing_lines"},
                            "price": {"file": price_file, "sheet": "Prices"}},
                "select": {"source": "line", "where": [{"field": "invoice_id", "result_field": "invoice_id"}]},
                "joins": [{"source": "price", "left": {"source": "line", "field": "sku"},
                           "right_field": "sku", "cardinality": "many_to_one"}],
                "outputs": [
                    {"result_field": "total", "aggregate": "sum", "expression": {"sub": [
                        {"mul": [{"source": "line", "field": "qty"}, {"source": "price", "field": "unit_price"}]},
                        {"source": "line", "field": "discount"}]},
                     "quantize": "0.01", "rounding": "ROUND_HALF_UP", "tolerance": "0"},
                    {"result_field": "line_count", "aggregate": "count", "tolerance": "0"}],
                "rule_evidence": [{"file": "billing_rules.md", "quote": "按sku连接唯一价格"},
                                  {"file": "billing_rules.md", "quote": "汇总后按ROUND_HALF_UP保留两位小数"}],
            }
            plan_path = self.public / f"{case}.plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            output = self.raw / f"{case}.json"
            completed = subprocess.run([self.python, str(REPO / "scripts" / "reconcile.py"),
                                        "--plan", str(plan_path), "--out", str(output)],
                                       cwd=REPO, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
            (self.raw / f"{case}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (self.raw / f"{case}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            expect(output.is_file(), "reconciliation CLI did not save a report")
            return completed, json.loads(output.read_text(encoding="utf-8"))

        def r01():
            completed, report = reconcile("R01_decimal_recompute", "bill_correct.csv", "prices.xlsx")
            expect(completed.returncode == 0, "normal reconciliation CLI returned an error")
            expect(report.get("status") == "reconciled_not_proven_lineage", "arithmetic agreement did not retain historical-lineage boundary")
            checks = {c["result_field"]: c for c in report["checks"]}
            expect(checks["total"]["calculated"] == "29.23" and checks["total"]["difference"] == "0.00", "end-of-aggregation HALF_UP decimal result differs")
            expect(checks["line_count"]["calculated"] == "2", "order filtering/count selected the wrong invoice lines")
            locations = [(r["source"], Path(r["location"]["file"]).name, r["location"]["row"]) for r in report["source_rows"]]
            expect(set(locations) == {("line", "billing.sql", 3), ("line", "billing.sql", 4),
                                      ("price", "prices.xlsx", 2), ("price", "prices.xlsx", 3)}, "recompute source citations are incomplete or contain an unrelated invoice")
            return {"status": report["status"], "checks": report["checks"], "source_locations": locations}
        self.case("R01_decimal_recompute", r01, ["prices.xlsx"])

        def r02():
            completed, report = reconcile("R02_wrong_total", "bill_wrong.csv", "prices.xlsx")
            expect(completed.returncode == 0 and report.get("status") == "mismatch", "incorrect total was not distinguished from arithmetic agreement")
            check = next(c for c in report["checks"] if c["result_field"] == "total")
            expect(check["calculated"] == "29.23" and check["difference"] == "0.01", "wrong-total delta is inaccurate")
            return {"status": report["status"], "total_check": check}
        self.case("R02_wrong_total", r02, ["prices.xlsx"])

        def r03():
            completed, report = reconcile("R03_duplicate_price_key", "bill_correct.csv", "prices_duplicate.xlsx")
            expect(completed.returncode != 0 and report.get("status") == "invalid_or_unavailable", "duplicate price key inflated or silently deduplicated a many-to-one join")
            expect(not report.get("checks"), "duplicate-key join produced apparently valid amount checks")
            return {"returncode": completed.returncode, "status": report["status"], "error": report.get("error")}
        self.case("R03_duplicate_price_key", r03, ["prices_duplicate.xlsx"])

        self.save(completed=True)
        return all(r["status"] == "passed" for r in self.records) and bool(self.records)

    def save(self, completed=False):
        summary = {status: sum(r["status"] == status for r in self.records) for status in ("passed", "failed", "unavailable")}
        report = {"assessment": "deterministic_cli_behavior_on_synthetic_sources", "completed": completed,
                  "python": self.python, "duration_seconds": round(time.monotonic() - self.started, 3),
                  "manifest": self.manifest, "summary": summary, "cases": self.records,
                  "limitations": ["Not an LLM reasoning benchmark or real-world accuracy estimate.",
                                  "OCR fixture is an explicit manual transcription; no OCR engine accuracy is tested.",
                                  "SQL files are static evidence and are never executed.",
                                  "Expected results are local to this scorer; independent solving agents must receive only public/ and the skill."]}
        (self.root / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "outputs" / "data_entity_eval")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    evaluation = Evaluation(args.out_dir, args.python, args.case)
    return 0 if evaluation.run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
