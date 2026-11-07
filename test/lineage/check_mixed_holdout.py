"""Re-evaluate immutable external holdout inputs through public Python APIs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from trace_pipeline import trace_sources
from evidence_retrieval import explore_evidence


def rooted_hops(report):
    """Count only actual key origins and retained bridge steps to each source."""
    located, seeds = {}, {}
    for row in report.get('result_rows', []):
        located.update({m['source_ref']: m for m in row.get('source_matches', []) if m['status'] == 'located'})
        seeds.update({s['seed_ref']: s for s in row.get('document_lookup_seeds', []) if s.get('seed_ref')})
    memo, errors = {'result': 0}, []
    def depth(reference, visiting=()):
        if reference in memo:
            return memo[reference]
        if reference in visiting:
            errors.append({'cyclic_origin': reference})
            return None
        node = located.get(reference) or seeds.get(reference)
        if node is None:
            errors.append({'unresolved_origin': reference})
            return None
        keys = node.get('key_provenance', []) if reference in located else node.get('anchor_key_provenance', [])
        predecessors = [origin['origin'] for key in keys for origin in key.get('origins', [])]
        if not predecessors and reference in seeds:
            predecessors = ['result']
        prior = [depth(origin, (*visiting, reference)) for origin in predecessors]
        if not prior or any(d is None for d in prior):
            return None
        edges = 1 if reference in located else sum(bool(step.get('bridge_id')) for step in node.get('provenance', []))
        memo[reference] = max(prior) + edges
        return memo[reference]
    values = {reference: depth(reference) for reference in located}
    return {'source_depths': values, 'maximum': max((v for v in values.values() if v is not None), default=0),
            'every_selected_source_is_rooted': all(v is not None for v in values.values()), 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    golden_bytes = (args.input / 'golden.json').read_bytes()
    golden = json.loads(golden_bytes)
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for case in golden['cases']:
        folder = args.input / case['case']
        sources = [p for p in folder.iterdir() if p.is_file() and p.name != 'result.csv']
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
        started = time.perf_counter()
        if 'target' in case:
            report = explore_evidence(case['target'], sources, cache_dir=args.out / 'cache' / case['case'])
        else:
            report = trace_sources(folder / 'result.csv', sources, rows='all', cache_dir=args.out / 'cache' / case['case'])
        actual = {(Path(m['file']).name, m['row_num']) for r in report.get('result_rows', [])
                  for m in r.get('source_matches', []) if m['status'] == 'located'}
        required = {tuple(r) for r in case['required_positive_rows']}
        forbidden = {tuple(r) for r in case['forbidden_positive_rows']}
        checks = {'required_locations_present': required <= actual,
                  'no_forbidden_locations_selected': not (actual & forbidden)}
        text = json.dumps(report, ensure_ascii=False)
        for name in ('required_text_identifiers', 'required_negative_identifiers'):
            checks[name + '_retained'] = all(value in text for value in case.get(name, []))
        if case.get('must_not_be_sources_located'):
            checks['not_marked_sources_located'] = report['source_localization']['status'] != 'located'
        if case.get('must_be_unique'):
            checks['one_unique_source'] = len(actual) == len(required)
        hops = rooted_hops(report)
        checks['all_selected_sources_have_traceable_origins'] = hops['every_selected_source_is_rooted']
        if 'minimum_relation_hops' in case:
            checks['minimum_relation_hops'] = hops['maximum'] >= case['minimum_relation_hops']
        result = {'case': case['case'], 'location_checks_passed': all(checks.values()), 'checks': checks,
                  'required': sorted(required), 'selected': sorted(actual), 'forbidden_selected': sorted(actual & forbidden),
                  'source_localization': report['source_localization']['status'],
                  'input_sha256': hashes, 'seconds': round(time.perf_counter() - started, 6),
                  'rooted_hops': hops, 'unassessed_golden_constraints': []}
        (args.out / (case['case'] + '.json')).write_text(text, encoding='utf-8')
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    summary = {'golden_sha256': hashlib.sha256(golden_bytes).hexdigest(), 'cases': len(results),
               'location_checks_passed': sum(r['location_checks_passed'] for r in results), 'results': results}
    (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    assert (args.input / 'golden.json').read_bytes() == golden_bytes


if __name__ == '__main__':
    main()
