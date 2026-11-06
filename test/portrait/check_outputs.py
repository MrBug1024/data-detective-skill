#!/usr/bin/env python3
"""Source-integrity checks for saved portrait Agent outputs; no answer rubric.

Semantic correctness and any rendered image require separate assessment.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from portrait import compile_portrait


def check_case(public, folder):
    cid = public['case_id']
    result = {'case_id': cid, 'integrity_pass': False, 'issues': [], 'semantic_assessment': 'not_assessed'}
    try:
        data = json.loads((folder / f'{cid}.input.json').read_text(encoding='utf-8-sig'))
        output = json.loads((folder / f'{cid}.portrait.json').read_text(encoding='utf-8-sig'))
        if data.get('case_id') != cid:
            result['issues'].append('input case_id mismatch')
        raw = {e['id']: e['statement'] for e in public['evidence']}
        extracted = {e['evidence_id']: e['statement'] for e in data['evidence']}
        if raw != extracted:
            result['issues'].append('input source text/ID set differs from the public case')
        prior_path = folder / f'{cid}.previous.json'
        previous = json.loads(prior_path.read_text(encoding='utf-8-sig')) if prior_path.exists() else None
        if compile_portrait(data, previous) != output:
            result['issues'].append('saved output differs from actual compiler result')
        if not (folder / f'{cid}.notes.md').read_text(encoding='utf-8').strip():
            result['issues'].append('no Agent answer/next actions')
        result['status'] = output.get('status')
        result['render_state'] = output.get('render_state')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['issues'].append(f'{type(exc).__name__}: {exc}')
    result['integrity_pass'] = not result['issues']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('cases.json'))
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8-sig'))
    results = [check_case(case, args.results) for case in cases]
    report = {'scope': 'specification_and_source_integrity_only', 'cases': results,
              'integrity_passed': sum(r['integrity_pass'] for r in results), 'total': len(results),
              'limitations': ['No semantic grading, no image generation or image-fidelity assessment, no real-case accuracy estimate.']}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding='utf-8')
    print(rendered)
    return 0 if all(r['integrity_pass'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
