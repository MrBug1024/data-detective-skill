"""Validate report shape and internal references, not the truth of its conclusions."""
import argparse
import json
from pathlib import Path


def validate_analysis(report):
    from jsonschema import Draft7Validator
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" /
                         "criminal-analysis.schema.json").read_text(encoding="utf-8-sig"))
    errors = [f"{'.'.join(map(str, error.absolute_path))}: {error.message}"
              for error in Draft7Validator(schema).iter_errors(report)]
    if errors:
        return errors
    evidence = [e["evidence_id"] for e in report["evidence"]]
    hypotheses = [h["hypothesis_id"] for h in report["hypotheses"]]
    actions = [a["action_id"] for a in report["investigation_plan"]]
    for label, ids in (("evidence_id", evidence), ("hypothesis_id", hypotheses), ("action_id", actions)):
        if len(set(ids)) != len(ids):
            errors.append(f"重复 {label}")
    def references(label, ids, allowed):
        unknown = set(ids) - set(allowed)
        if unknown:
            errors.append(f"{label}: 引用了未登记ID {sorted(unknown)}")
    references("assessment", report["assessment"]["evidence_ids"], evidence)
    for hypothesis in report["hypotheses"]:
        references(hypothesis["hypothesis_id"], hypothesis["supporting_evidence"] + hypothesis["counter_evidence"], evidence)
    pairs = []
    for cell in report["ach_matrix"]:
        references("ach evidence", [cell["evidence_id"]], evidence)
        references("ach hypothesis", [cell["hypothesis_id"]], hypotheses)
        pairs.append((cell["evidence_id"], cell["hypothesis_id"]))
    if len(set(pairs)) != len(pairs):
        errors.append("ACH 含重复单元格")
    expected = {(eid, hid) for eid in evidence for hid in hypotheses}
    if set(pairs) != expected:
        errors.append("ACH 未覆盖完整证据×假说矩阵（无鉴别力时标neutral/unknown）")
    for action in report["investigation_plan"]:
        references(action["action_id"], action["evidence_ids"], evidence)
        references(action["action_id"], action["hypothesis_ids"], hypotheses)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8-sig"))
    errors = validate_analysis(report)
    print(json.dumps({"structural_validation_pass": not errors,
                      "semantic_review_required": True, "errors": errors}, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
