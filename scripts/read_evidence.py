"""Page a report's evidence without loading the entire report into an LLM prompt."""
import argparse
import json
import sys
from pathlib import Path


def evidence_slice(report, *, start=0, limit=10, ids=None):
    if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("start>=0，limit=1..100")
    evidence = report.get("evidence", [])
    selected_ids = set(ids or [])
    selected = [item for item in evidence if not selected_ids or item.get("evidence_id") in selected_ids]
    return {"source_localization": report.get("source_localization"),
            "analysis_stage": report.get("analysis_stage"), "coverage": report.get("coverage"),
            "missing": report.get("missing", []), "conflicts": report.get("conflicts", []),
            "evidence_total": len(evidence), "matching_total": len(selected),
            "requested_ids_not_found": sorted(selected_ids - {e.get("evidence_id") for e in evidence}),
            "start": start, "limit": limit,
            "next_start": start + limit if start + limit < len(selected) else None,
            "evidence": selected[start:start + limit]}


def report_slice(report, *, section='evidence', start=0, limit=10, ids=None, row=None, field=None):
    """Return a bounded section; summaries avoid embedding all candidate queries."""
    if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('start>=0, limit=1..100')
    if section == 'summary':
        from run_contract import assess
        coverage = report.get('coverage', {})
        rows = report.get('result_rows', [])
        return {'analysis_stage': report.get('analysis_stage'), 'assessment': assess(report),
                'coverage': {k: v for k, v in coverage.items() if not isinstance(v, (dict, list))},
                'requested_physical_rows': coverage.get('requested_physical_rows'),
                'counts': {'result_rows': len(rows), 'source_matches': sum(len(r.get('source_matches', [])) for r in rows),
                           'field_mappings': len(report.get('field_mappings', [])), 'evidence': len(report.get('evidence', [])),
                           'issues': len(report.get('issues', []))},
                'next_actions': report.get('next_actions', [])[:limit],
                'next_actions_truncated': len(report.get('next_actions', [])) > limit}
    if section == 'evidence' and row is None and field is None:
        return evidence_slice(report, start=start, limit=limit, ids=ids)
    sections = {'rows': report.get('result_rows', []), 'fields': report.get('field_mappings', []),
                'rules': report.get('related_evidence', report.get('rule_evidence', [])),
                'issues': report.get('issues', []), 'entities': report.get('entity_mentions', []),
                'dependencies': report.get('lineage_dependencies', {}).get('key_dependencies', [])}
    if section not in sections:
        raise ValueError('Use --section rows/fields for --row/--field filtering')
    values = sections[section]
    selected = [item for item in values if
                (row is None or item.get('result_row', item.get('row_num')) == row) and
                (field is None or item.get('result_field') == field)]
    return {'section': section, 'total': len(values), 'matching_total': len(selected),
            'start': start, 'limit': limit, 'next_start': start + limit if start + limit < len(selected) else None,
            'items': selected[start:start + limit]}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument('--section', choices=('summary', 'evidence', 'rows', 'fields', 'rules', 'issues', 'entities', 'dependencies'), default='evidence')
    parser.add_argument('--row', type=int, help='Physical result row; use rows/fields/dependencies')
    parser.add_argument('--field', help='Result field name for the fields section')
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8-sig"))
    print(json.dumps(report_slice(report, section=args.section, start=args.start, limit=args.limit,
                                  ids=args.ids, row=args.row, field=args.field), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
