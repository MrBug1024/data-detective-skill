#!/usr/bin/env python3
"""Reproducible synthetic CSV benchmark; never reads real cases or .env.

Cold means an empty application index cache, not a flushed OS filesystem cache.
Each timing is one sample. This measures deterministic engines, not LLM reasoning,
network latency, HTML rendering or field investigation. No tracemalloc is enabled.
"""
import argparse
import contextlib
import csv
import datetime
import hashlib
import importlib.util
import io
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from indexer import build_index
from investigator import Investigator
from matcher import MatchConfig, MatchEngine


def timed(call):
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        started = time.perf_counter()
        value = call()
        elapsed = time.perf_counter() - started
    return value, elapsed, "Loaded index from cache:" in log.getvalue()


def make_data(folder, rows):
    source, result = folder / "synthetic.csv", folder / "result.csv"
    target_number = rows // 2 + 1
    headers = ["订单ID", "客户ID", "姓名", "金额", "日期", "说明"]
    target = None
    name_matches = 0
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for number in range(1, rows + 1):
            is_named = number % 50 == 0 or number == target_number
            name_matches += is_named
            record = [f"ORD{10000000 + number}", f"P{20000000 + number}",
                      "陈明" if is_named else f"合成员{number}",
                      f"{100000 + number // 100}.{number % 100:02d}",
                      f"2026-01-{number % 28 + 1:02d}",
                      "CASE900001 现场异常记录；待核验" if number == target_number else "例行记录"]
            writer.writerow(record)
            if number == target_number:
                target = record
    with result.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["订单ID", "金额"])
        writer.writerow([target[0], target[3]])
    return source, result, {"order_id": target[0], "person_id": target[1],
                            "amount": target[3], "source_csv_row": target_number + 1,
                            "person_name": "陈明", "name_matches": name_matches,
                            "criminal_query": "CASE900001"}


def size_summary(report):
    data = report.to_dict()
    return {
        "nodes": len(data.get("chain", {}).get("nodes", [])),
        "edges": len(data.get("chain", {}).get("edges", [])),
        "evidence": len(data.get("evidence", [])),
        "entities": len(data.get("entities", [])),
        "timeline": len(data.get("timeline", [])),
        "conflicts": len(data.get("conflicts", [])),
        "serialized_json_bytes": len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")),
        "analysis_stage": data.get("analysis_stage"),
    }


def run_mode(mode, source, result, target, cache, rows):
    inv = Investigator(cache_dir=str(cache))
    counts = []
    original_match = MatchEngine.match_result_unit

    def counted_match(engine, result_unit):
        matches = original_match(engine, result_unit)
        counts.append({"returned_matches_after_cap": len(matches),
                       "distinct_units_after_cap": len({m.unit.unit_id for m in matches}),
                       "max_candidates": engine.config.max_candidates})
        return matches

    def execute():
        if mode == "data":
            return inv.trace_data(str(result), 2, [str(source)])
        if mode == "entity":
            return inv.investigate_entity(target["person_name"], [str(source)])
        return inv.investigate_criminal(target["criminal_query"], [str(source)])

    # Counts the actual DATA matcher return without executing matching twice.
    with patch.object(MatchEngine, "match_result_unit", counted_match):
        report, elapsed, cache_hit = timed(execute)
    data = report.to_dict()
    metrics = {"seconds": round(elapsed, 6), "cache_hit_observed": cache_hit,
               "returned": size_summary(report), "native_coverage": data.get("coverage")}
    if mode == "data":
        returned_rows = [node["row_num"] for node in data["chain"]["nodes"]
                         if node["node_type"] == "source"]
        metrics["candidates"] = counts[0] if counts else None
        metrics["coverage"] = {"indexed_source_units": len(inv._index.id_map),
                               "source_files_requested": 1,
                               "source_files_with_returned_rows": len({n["source_file"] for n in data["chain"]["nodes"] if n["node_type"] == "source"}),
                               "returned_source_rows": returned_rows,
                               "raw_pre_cap_candidate_count": None,
                               "completeness": "native DATA coverage unavailable; capped matching is not exhaustive lineage verification"}
        metrics["checks"] = {"exact_target_row_selected": returned_rows == [target["source_csv_row"]],
                             "target_id_in_selected_node": any(target["order_id"] in n["field_values"] for n in data["chain"]["nodes"] if n["node_type"] == "source")}
    elif mode == "entity":
        metrics["candidates"] = {"name_token_candidates": len(inv._index.lookup_tokens(target["person_name"])),
                                 "exact_name_candidates": len(inv._index.lookup_exact(target["person_name"])),
                                 "unique_person_id_candidates": len(inv._index.lookup_exact(target["person_id"]))}
        metrics["coverage"] = data["coverage"]
        metrics["checks"] = {"all_name_mentions_counted": data["coverage"]["matched_units"] == target["name_matches"],
                             "same_name_identity_conflict_reported": any(c.get("type") == "identity_unresolved" for c in data["conflicts"]) if target["name_matches"] > 1 else True,
                             "bounded_return": len(data["evidence"]) == min(100, target["name_matches"])}
    else:
        metrics["candidates"] = {"query_token_candidates": len(inv._index.lookup_tokens(target["criminal_query"])),
                                 "context_units_considered": len(inv._all_units)}
        metrics["coverage"] = data["coverage"]
        metrics["checks"] = {"query_hit_is_first": bool(data["evidence"]) and target["criminal_query"] in data["evidence"][0]["value"],
                             "context_not_silently_dropped": data["coverage"]["total_units"] == rows and data["coverage"]["omitted_units"] == max(0, rows - 100),
                             "bounded_return": len(data["evidence"]) == min(100, rows),
                             "is_evidence_packet_only": data["analysis_stage"] == "evidence_packet_only"}
    return metrics


def benchmark(rows):
    report = {
        "benchmark": "synthetic_csv_single_sample_v1",
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "platform": platform.platform(), "logical_cpus": os.cpu_count()},
        "method": {"timer": "time.perf_counter", "samples_per_measurement": 1,
                   "cold": "fresh application cache directory; OS cache is not flushed",
                   "warm": "same input and application cache on immediately following call",
                   "timing_scope": "index/engine call only; includes cache I/O and first-use imports; excludes CSV generation, JSON serialization, diagnostics, HTML and LLMs",
                   "instrumentation": "one DATA matcher wrapper counts actual capped candidates; no tracemalloc, profiler or artificial memory estimator",
                   "limitations": ["single machine, fixed schema and distribution; not a general latency guarantee",
                                   "initial index cold may include pandas import; mode cold runs use warm Python imports and OS cache",
                                   "CRIMINAL time measures packet assembly, not completed reasoning",
                                   "no real case files, .env or hidden answers are read"]},
        "configuration": {"source_rows": rows, "entity_max_units": 100, "criminal_max_units": 100,
                          "criminal_max_chars": 1600,
                          "data_max_candidates": MatchConfig().max_candidates},
    }
    with tempfile.TemporaryDirectory(prefix="data_detective_benchmark_") as temporary:
        folder = Path(temporary)
        source, result, target = make_data(folder, rows)
        report["dataset"] = {"rows": rows, "source_csv_bytes": source.stat().st_size,
                             "source_csv_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                             "result_csv_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
                             "target": target,
                             "distribution": "unique order/person IDs and amount per row; repeated target name every 50th row plus target; one criminal query hit; ordinary context in all remaining rows"}
        cache = folder / "index_cache"
        for temperature in ("cold", "warm"):
            index, elapsed, cache_hit = timed(lambda: build_index([str(source)], cache_dir=str(cache)))
            report.setdefault("index", {})[temperature] = {
                "seconds": round(elapsed, 6), "cache_hit_observed": cache_hit,
                "stats": index.stats(), "all_rows_indexed": len(index.id_map) == rows,
                "cache_bytes": sum(p.stat().st_size for p in cache.glob("*.pkl"))}
        report["modes"] = {}
        for mode in ("data", "entity", "criminal"):
            cache = folder / (mode + "_cache")
            report["modes"][mode] = {temperature: run_mode(mode, source, result, target, cache, rows)
                                     for temperature in ("cold", "warm")}
        report["all_checks_passed"] = (
            all(v["all_rows_indexed"] for v in report["index"].values()) and
            all(all(run["checks"].values()) for measurements in report["modes"].values() for run in measurements.values()) and
            not report["index"]["cold"]["cache_hit_observed"] and report["index"]["warm"]["cache_hit_observed"] and
            all(not measurements["cold"]["cache_hit_observed"] and measurements["warm"]["cache_hit_observed"] for measurements in report["modes"].values()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000, help="synthetic source CSV rows (default: 10000)")
    parser.add_argument("--out", type=Path, help="optional JSON output path")
    args = parser.parse_args()
    if not 1 <= args.rows <= 1000000:
        parser.error("--rows must be between 1 and 1000000")
    if importlib.util.find_spec("pandas") is None:
        parser.error("pandas is required; use an environment with scripts/requirements.txt installed")
    result = benchmark(args.rows)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
