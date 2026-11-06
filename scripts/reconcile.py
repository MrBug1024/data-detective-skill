"""Recompute an explicit, source-cited lineage plan without executing code or SQL.

Matching finds candidates. This tool checks an agent/user supplied transformation
against source rows; arithmetic agreement does not prove historical execution.
"""
import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal, DecimalException, Inexact, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN
from pathlib import Path

from parsers import FileParser
from indexer import value_text
from ingestion_support import sha256_file


class PlanError(ValueError):
    pass


def number(value):
    if value is None or isinstance(value, bool):
        raise PlanError("缺失值/布尔值不能作为数值")
    try:
        result = Decimal(str(value))
    except (DecimalException, ValueError) as exc:
        raise PlanError(f"不能无损解释为十进制数值: {value!r}") from exc
    if not result.is_finite():
        raise PlanError("数值必须有限")
    return result


def location(unit):
    return {"file": unit.source_file, "sheet": unit.sheet_name, "row": unit.row_num,
            "page": unit.page_num, "paragraph": unit.paragraph_num,
            "char_offset": unit.char_offset, "metadata": unit.metadata}


def get_value(unit, field):
    if field not in unit.field_names:
        raise PlanError(f"来源缺字段 {field!r}: {location(unit)}")
    return unit.field_values[unit.field_names.index(field)]


def validate_plan_shape(plan):
    """Strict standard-library validation of the supported plan language."""
    def obj(value, allowed, required):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            raise PlanError('计划对象有未知或缺失字段')
    def text(value):
        if not isinstance(value, str) or not value.strip():
            raise PlanError('字段/来源名必须为非空字符串')
    def seq(value):
        if not isinstance(value, list): raise PlanError('计划集合必须为JSON数组')
        return value
    def ref(value, required=('file',), extra=()):
        obj(value, ('file', 'sheet', 'table') + tuple(extra), required)
        for k in ('file', 'sheet', 'table'):
            if k in value: text(value[k])
    def field(value):
        obj(value, ('source', 'field'), ('source', 'field'))
        text(value['source']); text(value['field'])
    def expr(value, depth=0):
        if depth > 20 or not isinstance(value, dict): raise PlanError('表达式对象/深度无效')
        if set(value) == {'source', 'field'}: field(value)
        elif set(value) == {'literal'}:
            if not isinstance(value['literal'], (str, int, Decimal, bool, type(None))):
                raise PlanError('literal必须是精确标量；小数请用字符串或Decimal，不能用已损失精度的float')
        elif len(value) == 1 and next(iter(value)) in ('add', 'sub', 'mul', 'div'):
            args = seq(next(iter(value.values())))
            if not 2 <= len(args) <= 20: raise PlanError('算术参数数目无效')
            for arg in args: expr(arg, depth + 1)
        else: raise PlanError('未支持的表达式，不能执行代码')
    obj(plan, ('version', 'description', 'result', 'sources', 'select', 'joins', 'outputs', 'rule_evidence', 'max_join_rows'),
        ('version', 'result', 'sources', 'select', 'outputs'))
    ref(plan['result'], ('file', 'row'), ('row',))
    if type(plan['result']['row']) is not int or plan['result']['row'] < 1: raise PlanError('结果行号无效')
    if not isinstance(plan['sources'], dict) or not plan['sources']: raise PlanError('sources不能为空')
    for alias, source in plan['sources'].items(): text(alias); ref(source)
    obj(plan['select'], ('source', 'where'), ('source',)); text(plan['select']['source'])
    for predicate in seq(plan['select'].get('where', [])):
        obj(predicate, ('field', 'result_field', 'literal'), ('field',)); text(predicate['field'])
        if ('literal' in predicate) == ('result_field' in predicate): raise PlanError('过滤须明确一种比较值')
        if 'result_field' in predicate: text(predicate['result_field'])
        elif not isinstance(predicate['literal'], (str, int, Decimal, bool, type(None))):
            raise PlanError('过滤literal必须是精确标量')
    for join in seq(plan.get('joins', [])):
        obj(join, ('source', 'left', 'right_field', 'cardinality'), ('source', 'left', 'right_field', 'cardinality'))
        text(join['source']); field(join['left']); text(join['right_field'])
    for output in seq(plan['outputs']):
        obj(output, ('result_field', 'aggregate', 'expression', 'type', 'tolerance', 'quantize', 'rounding'), ('result_field',))
        text(output['result_field'])
        for name in ('tolerance', 'quantize', 'rounding'):
            if name in output: text(output[name])
        if output.get('aggregate') != 'count':
            if 'expression' not in output: raise PlanError('缺少输出表达式')
            expr(output['expression'])
        elif 'expression' in output: expr(output['expression'])
    for rule in seq(plan.get('rule_evidence', [])):
        ref(rule, ('file', 'quote'), ('quote',)); text(rule['quote'])


def expression(expr, row, depth=0):
    if depth > 20 or not isinstance(expr, dict):
        raise PlanError("表达式必须为深度不超过20的JSON对象")
    if set(expr) == {"source", "field"}:
        if expr["source"] not in row:
            raise PlanError("表达式引用未连接的来源")
        return get_value(row[expr["source"]], expr["field"])
    if set(expr) == {"literal"}:
        return expr["literal"]
    if len(expr) != 1:
        raise PlanError("表达式只允许字段、literal或一个算术操作")
    op, args = next(iter(expr.items()))
    if op not in ("add", "sub", "mul", "div") or not isinstance(args, list) or not 2 <= len(args) <= 20:
        raise PlanError("算术仅支持add/sub/mul/div及2..20个参数")
    values = [number(expression(arg, row, depth + 1)) for arg in args]
    result = values[0]
    for value in values[1:]:
        if op == "add": result += value
        elif op == "sub": result -= value
        elif op == "mul": result *= value
        elif op == "div":
            if value == 0:
                raise PlanError("除数为零")
            result /= value
    return result


def verify_plan(plan, base_dir=None):
    """All paths resolve relative to the plan file, never to a source instruction."""
    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan.get("version") != 1:
        raise PlanError("计划version必须为1")
    validate_plan_shape(plan)
    base = Path(base_dir or ".").resolve()
    resolve = lambda value: str((base / value).resolve())
    fp = FileParser()
    parsed, ingestion, hashes = {}, {}, {}

    def load(ref):
        path = resolve(ref["file"])
        if path not in parsed:
            parsed[path] = fp.parse(path)
            ingestion[path] = list(getattr(fp, "last_diagnostics", []))
            hashes[path] = sha256_file(path)
            sidecar = Path(path + '.ocr.json')
            if sidecar.is_file():
                hashes[str(sidecar)] = sha256_file(sidecar)
        units = parsed[path]
        if ref.get("sheet") is not None:
            units = [u for u in units if u.sheet_name == ref["sheet"]]
        if ref.get("table") is not None:
            units = [u for u in units if u.metadata.get("table", u.sheet_name) == ref["table"]]
        return units

    result_ref = plan["result"]
    results = [u for u in load(result_ref) if u.row_num == result_ref["row"]]
    if len(results) != 1:
        raise PlanError("结果行必须唯一；明确工作表/表名及物理行号")
    result = results[0]
    rule_evidence = []
    for anchor in plan.get("rule_evidence", []):
        quote = anchor.get("quote", "")
        if not isinstance(quote, str) or not quote.strip():
            raise PlanError("规则引文不能为空")
        matches = [u for u in load(anchor) if quote in u.raw_text]
        if not matches:
            raise PlanError("规则引文未在指定来源中找到")
        rule_evidence.append({"quote": quote, "locations": [location(u) for u in matches],
                              "validation": "substring_only_semantics_requires_review"})
    sources = {alias: [u for u in load(ref) if not u.is_rule and not u.metadata.get('is_header_row')]
               for alias, ref in plan["sources"].items()}
    selection = plan["select"]
    alias = selection["source"]
    if alias not in sources:
        raise PlanError("select引用未知来源")
    rows = []
    for unit in sources[alias]:
        keep = True
        for predicate in selection.get("where", []):
            if set(predicate) == {"field", "result_field"}:
                expected = get_value(result, predicate["result_field"])
            elif set(predicate) == {"field", "literal"}:
                expected = predicate["literal"]
            else:
                raise PlanError("过滤只允许field+result_field或field+literal的精确等值")
            actual = get_value(unit, predicate["field"])
            if expected is None or actual is None or value_text(expected) != value_text(actual):
                keep = False
                break
        if keep: rows.append({alias: unit})
    limit = plan.get("max_join_rows", 100000)
    if type(limit) is not int or not 1 <= limit <= 1000000:
        raise PlanError("max_join_rows必须为1..1000000")
    if len(rows) > limit:
        raise PlanError("过滤结果超过max_join_rows；请缩小范围")
    joins = []
    for join in plan.get("joins", []):
        right = join["source"]
        left = join["left"]
        cardinality = join["cardinality"]
        if right not in sources or (rows and right in rows[0]):
            raise PlanError("join来源未知或别名已使用")
        if cardinality not in ("one_to_one", "many_to_one", "one_to_many", "many_to_many"):
            raise PlanError("必须显式声明连接基数")
        lookup = defaultdict(list)
        for unit in sources[right]:
            key = get_value(unit, join["right_field"])
            if key is not None: lookup[value_text(key)].append(unit)
        new_rows, seen_left, missing = [], set(), 0
        for row in rows:
            if left["source"] not in row:
                raise PlanError("连接左侧来源未出现")
            key = get_value(row[left["source"]], left["field"])
            key = None if key is None else value_text(key)
            matches = lookup.get(key, [])
            if cardinality in ("one_to_one", "many_to_one") and len(matches) > 1:
                raise PlanError("连接右键不唯一，违反声明基数；不能重复计入金额")
            if cardinality in ("one_to_one", "one_to_many") and key in seen_left:
                raise PlanError("连接左键不唯一，违反声明基数")
            seen_left.add(key)
            if not matches: missing += 1
            if len(new_rows) + len(matches) > limit:
                raise PlanError("连接结果超过max_join_rows；未静默截断")
            new_rows.extend({**row, right: unit} for unit in matches)
        joins.append({"source": right, "cardinality": cardinality,
                      "input_rows": len(rows), "output_rows": len(new_rows), "unmatched_rows": missing})
        if missing:
            raise PlanError(f"{missing}条记录缺连接对象；不能以内连接静默丢失")
        rows = new_rows
    checks = []
    outputs = plan["outputs"]
    if not isinstance(outputs, list) or not outputs:
        raise PlanError("outputs不能为空")
    if len({o["result_field"] for o in outputs}) != len(outputs):
        raise PlanError("结果字段不能重复核验")
    with localcontext() as ctx:
        ctx.prec = 50
        # Reject silent precision loss. Repeating divisions need an explicit
        # separately reviewed calculation/rounding rule, not an implicit float.
        ctx.traps[Inexact] = True
        for output in outputs:
            field = output["result_field"]
            aggregate = output.get("aggregate", "scalar")
            if aggregate not in ("scalar", "sum", "count", "min", "max"):
                raise PlanError("aggregate仅支持scalar/sum/count/min/max")
            if not rows:
                checks.append({"result_field": field, "status": "no_source_rows"})
                continue
            if aggregate == "scalar" and len(rows) != 1:
                checks.append({"result_field": field, "status": "ambiguous_source_rows", "rows": len(rows)})
                continue
            values = [] if aggregate == "count" else [expression(output["expression"], row) for row in rows]
            if aggregate == "scalar": calculated = values[0]
            elif aggregate == "count": calculated = Decimal(len(rows))
            else:
                numeric = [number(v) for v in values]
                calculated = sum(numeric, Decimal(0)) if aggregate == "sum" else (min(numeric) if aggregate == "min" else max(numeric))
            actual = get_value(result, field)
            numeric_check = output.get("type", "number") == "number"
            if output.get("type", "number") not in ("number", "text"):
                raise PlanError("type仅支持number/text")
            tolerance = number(output.get("tolerance", "0"))
            if tolerance < 0:
                raise PlanError("容差不能为负")
            if numeric_check:
                calculated, actual = number(calculated), number(actual)
                quantum = output.get("quantize")
                if quantum is not None:
                    rounding = {"ROUND_HALF_UP": ROUND_HALF_UP, "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
                                "ROUND_DOWN": ROUND_DOWN}.get(output.get("rounding"))
                    if rounding is None: raise PlanError("quantize必须提供明确rounding")
                    quantum_value = number(quantum)
                    if quantum_value <= 0 or quantum_value.normalize().as_tuple().digits != (1,):
                        raise PlanError("quantize只接受10的整数次幂，如0.01；不支持0.05步进舍入")
                    with localcontext() as rounding_ctx:
                        # This is the one precision loss explicitly requested
                        # by the cited plan; arithmetic elsewhere still traps.
                        rounding_ctx.traps[Inexact] = False
                        calculated = calculated.quantize(quantum_value.normalize(), rounding=rounding)
                difference = calculated - actual
                equal = abs(difference) <= tolerance
            else:
                if tolerance or "quantize" in output: raise PlanError("文本核验不支持容差/舍入")
                equal = actual is not None and calculated is not None and value_text(calculated) == value_text(actual)
                difference = None
            checks.append({"result_field": field, "status": "equal" if equal else "mismatch",
                           "calculated": str(calculated), "actual": str(actual),
                           "difference": str(difference) if difference is not None else None,
                           "difference_definition": "calculated_minus_actual",
                           "tolerance": str(tolerance), "aggregate": aggregate, "input_rows": len(rows)})
    citations = {}
    for row in rows:
        for source, unit in row.items():
            key = (source, unit.unit_id)
            citations[key] = {"source": source, "unit_id": unit.unit_id, "location": location(unit),
                              "fields": dict(zip(unit.field_names, unit.to_dict()["field_values"]))}
    incomplete = {p: issues for p, issues in ingestion.items()
                  if any(i.get("severity") in ("warning", "error") for i in issues)}
    equal = all(c["status"] == "equal" for c in checks)
    status = "reconciled_not_proven_lineage" if equal and rule_evidence and not incomplete else "incomplete"
    if any(c["status"] == "mismatch" for c in checks): status = "mismatch"
    return {"status": status, "analysis_stage": "deterministic_reconciliation", "confidence": None,
            "semantics": "explicit_plan_recomputed; not_proof_of_historical_lineage_or_rule_semantics",
            "rule_status": "cited_requires_semantic_review" if rule_evidence else "uncited_plan_only",
            "result": location(result), "source_sha256": hashes, "rule_evidence": rule_evidence,
            "checks": checks, "joins": joins, "source_rows": list(citations.values()),
            "coverage": {"joined_rows": len(rows), "input_units": {k: len(v) for k,v in sources.items()},
                         "ingestion_complete": not incomplete}, "ingestion_issues": incomplete}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_plan(json.loads(args.plan.read_text(encoding="utf-8-sig"), parse_float=Decimal), args.plan.resolve().parent)
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, DecimalException) as exc:
        report = {"status": "invalid_or_unavailable", "error": str(exc), "confidence": None}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(report["status"])
    if report["status"] == "invalid_or_unavailable":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
