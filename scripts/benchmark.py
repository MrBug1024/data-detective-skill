#!/usr/bin/env python3
"""Reproducible synthetic CSV benchmark; never reads real cases or .env.

Cold means an empty application index cache, not a flushed OS filesystem cache.
Each timing is one sample. This measures deterministic engines, not LLM reasoning,
network latency, HTML rendering or human validation. No tracemalloc is enabled.
"""
import argparse
import contextlib
import csv
import datetime
import io
import json
import os
import platform
import tempfile
import time
from pathlib import Path
from evidence_retrieval import explore_evidence
from ingestion_support import sha256_file
from trace_pipeline import trace_sources


def timed(call):
    with contextlib.redirect_stdout(io.StringIO()):
        started = time.perf_counter()
        value = call()
    return value, time.perf_counter() - started


def make_data(folder, rows):
    orders, customers, result, note = [folder / name for name in ('orders.csv', 'customers.csv', 'result.csv', 'handoff.txt')]
    target_number = rows // 2 + 1
    target = {'order_id': f'ORD-{target_number:09d}', 'customer_id': f'C-{target_number:09d}',
              'contract_id': f'CT-{target_number:09d}', 'source_row': target_number + 1,
              'amount': f'{100000 + target_number // 100}.{target_number % 100:02d}'}
    with orders.open('w', encoding='utf-8', newline='') as out, customers.open('w', encoding='utf-8', newline='') as people:
        ow, cw = csv.writer(out), csv.writer(people)
        ow.writerow(['order_id', 'customer_id', 'amount', 'order_date'])
        cw.writerow(['customer_id', 'customer_name', 'city'])
        for number in range(1, rows + 1):
            ow.writerow([f'ORD-{number:09d}', f'C-{number:09d}',
                         f'{100000 + number // 100}.{number % 100:02d}', f'2026-01-{number % 28 + 1:02d}'])
            cw.writerow([f'C-{number:09d}', 'Repeated Name' if number % 50 == 0 else f'Customer {number}', 'City A'])
    with result.open('w', encoding='utf-8', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['order_id', 'amount'])
        writer.writerow([target['order_id'], target['amount']])
    note.write_text(f"Tenant: benchmark; Order ID {target['order_id']} references Contract ID {target['contract_id']}.", encoding='utf-8')
    return orders, customers, result, note, target


def source_rows(report):
    registry = report.get('source_records', {})
    return [{**registry.get(match.get('source_ref'), {}), **match}
            for row in report.get('result_rows', []) for match in row.get('source_matches', [])]


def benchmark(rows):
    output = {'benchmark': 'synthetic_data_lineage_v2',
              'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'environment': {'python': platform.python_version(), 'platform': platform.platform(), 'logical_cpus': os.cpu_count()},
              'method': {'samples_per_measurement': 1, 'timer': 'time.perf_counter',
                         'cold': 'new application cache; operating-system cache not flushed',
                         'warm': 'immediate repeat with unchanged source bytes and cache',
                         'timing_scope': 'unified trace/exploration engine, including hashes and source parsing; excludes generation and final rendering',
                         'memory': 'not measured; no inferred RAM or RSS claim',
                         'limitations': ['Synthetic fixed-schema fixtures, not general accuracy or latency guarantees.',
                                         'CSV exact-query results are scanned again; only profile cache hits are expected.',
                                         'No LLM reasoning, external services or hidden evaluation data.']}}
    with tempfile.TemporaryDirectory(prefix='data_lineage_benchmark_') as temporary:
        folder = Path(temporary)
        orders, customers, result, note, target = make_data(folder, rows)
        output['dataset'] = {'source_rows_per_table': rows, 'target': target,
                             'files': {p.name: {'bytes': p.stat().st_size, 'sha256': sha256_file(p)}
                                       for p in (orders, customers, result, note)}}
        output['measurements'] = {'structured_trace': {}, 'target_search': {}}
        for temperature in ('cold', 'warm'):
            report, seconds = timed(lambda: trace_sources(result, [customers, orders], row=2, cache_dir=folder / 'trace-cache'))
            selected = source_rows(report)
            selected_rows = {Path(item['file']).name: item['row_num'] for item in selected}
            checks = {'source_chain_located': report['source_localization']['status'] == 'located',
                      'exact_rows': selected_rows == {'orders.csv': target['source_row'], 'customers.csv': target['source_row']},
                      'exact_order': any(item.get('values', {}).get('order_id') == target['order_id'] for item in selected),
                      'exact_customer': any(item.get('values', {}).get('customer_id') == target['customer_id'] for item in selected),
                      'whole_source_scanned': all(q['coverage']['scanned_rows'] == rows for q in report['coverage']['source_queries']),
                      'historical_boundary_preserved': report['historical_lineage']['status'] == 'not_proven'}
            profile_hits = {Path(path).name: profile.get('cache', {}).get('hit') for path, profile in report['source_profiles'].items()}
            checks['profile_cache_temperature'] = all(value is (temperature == 'warm') for value in profile_hits.values())
            output['measurements']['structured_trace'][temperature] = {'seconds': round(seconds, 6),
                'checks': checks, 'profile_cache_hits': profile_hits, 'source_rows': selected_rows,
                'coverage': report['coverage'], 'serialized_report_bytes': len(json.dumps(report, ensure_ascii=False).encode('utf-8'))}
            report, seconds = timed(lambda: explore_evidence('Order ID ' + target['order_id'], [note], cache_dir=folder / 'evidence-cache'))
            checks = {'one_original_passage': len(report['related_evidence']) == 1,
                      'target_id_preserved': any(m['raw_value'] == target['order_id'] for m in report['entity_mentions']),
                      'contract_lookup_bridge': any(seed['identifier_namespace'] == 'contract' and seed['value'] == target['contract_id'] for seed in report['lookup_seeds']),
                      'retrieval_complete': report['coverage']['retrieval_complete'] and not report['coverage']['truncated'],
                      'no_verified_identity_merge': all(not bridge['identity_merge_allowed'] for bridge in report['bridge_candidates'])}
            output['measurements']['target_search'][temperature] = {'seconds': round(seconds, 6), 'checks': checks,
                'coverage': report['coverage'], 'mentions': len(report['entity_mentions']), 'bridges': len(report['bridge_candidates'])}
    output['all_checks_passed'] = all(all(run['checks'].values()) for timings in output['measurements'].values() for run in timings.values())
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.rows <= 1000000:
        parser.error('--rows must be between 1 and 1000000')
    report = benchmark(args.rows)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0 if report['all_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
