"""Run raw multi-domain lineage scenarios; outputs contain only synthetic data."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from trace_pipeline import trace_sources
from build_scenarios import build_cases, evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.perf_counter()
    for case in build_cases(args.out / 'raw'):
        t = time.perf_counter()
        try:
            report = trace_sources(case['result'], case['sources'], rows=case['rows'],
                                   config=case['config'], cache_dir=args.out / 'cache' / case['name'])
            checked = evaluate(case, report)
            (args.out / (case['name'] + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as exc:
            checked = {'case': case['name'], 'domain': case['domain'], 'passed': False,
                       'error_type': type(exc).__name__, 'error': str(exc)}
        checked['seconds'] = round(time.perf_counter() - t, 6)
        results.append(checked)
    metrics = {key: sum(r.get(key, 0) for r in results)
               for key in ('expected_links', 'predicted_links', 'true_positive', 'false_positive', 'false_negative')}
    summary = {'synthetic_only': True, 'cases': len(results), 'passed': sum(r['passed'] for r in results),
               'metrics': metrics, 'results': results, 'seconds': round(time.perf_counter() - started, 6)}
    (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
