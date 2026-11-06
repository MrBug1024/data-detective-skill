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
    return {"investigation_type": report.get("investigation_type"),
            "analysis_stage": report.get("analysis_stage"), "coverage": report.get("coverage"),
            "missing": report.get("missing", []), "conflicts": report.get("conflicts", []),
            "evidence_total": len(evidence), "matching_total": len(selected),
            "requested_ids_not_found": sorted(selected_ids - {e.get("evidence_id") for e in evidence}),
            "start": start, "limit": limit,
            "next_start": start + limit if start + limit < len(selected) else None,
            "evidence": selected[start:start + limit]}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8-sig"))
    print(json.dumps(evidence_slice(report, start=args.start, limit=args.limit, ids=args.ids), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
