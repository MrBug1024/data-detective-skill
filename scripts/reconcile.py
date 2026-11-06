"""Recompute an explicit, source-cited lineage plan without executing code or SQL.

Matching finds candidates. This tool checks an agent/user supplied transformation
against source rows; arithmetic agreement does not prove historical execution.
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal, DecimalException, Inexact, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN
from pathlib import Path

from parsers import AddressableUnit, FileParser
from indexer import value_text
from ingestion_support import sha256_file


class PlanError(ValueError):
    pass


class _CachedSourceLoadError(PlanError):
    """Re-raise a batch-local failure without retaining an exception/traceback."""
    def __init__(self, error_type, message):
        super().__init__(message)
        self.original_error_type = error_type


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
    def exact_scalar(value):
        if not isinstance(value, (str, int, Decimal, bool, type(None))):
            raise PlanError('query比较值必须是精确JSON标量；小数请用字符串或Decimal')
        if isinstance(value, Decimal) and not value.is_finite():
            raise PlanError('query数值必须有限')
    def query(value):
        obj(value, ('where', 'key_rows', 'columns', 'limit', 'reader', 'max_return_cells'), ())
        if 'where' in value:
            if not isinstance(value['where'], dict): raise PlanError('query.where必须为对象')
            for name, allowed in value['where'].items():
                text(name)
                if not seq(allowed): raise PlanError('query.where的精确值数组不能为空')
                for item in allowed: exact_scalar(item)
        if 'key_rows' in value:
            keys = seq(value['key_rows'])
            if not keys: raise PlanError('query.key_rows不能为空')
            names = None
            for row in keys:
                if not isinstance(row, dict) or not row: raise PlanError('query.key_rows每项须为非空字段对象')
                for name, item in row.items(): text(name); exact_scalar(item)
                if names is None: names = set(row)
                elif set(row) != names: raise PlanError('query.key_rows每项必须使用相同字段组')
        if 'columns' in value:
            columns = seq(value['columns'])
            if not columns: raise PlanError('query.columns不能为空')
            for name in columns: text(name)
            if len(set(columns)) != len(columns): raise PlanError('query.columns字段不能重复')
        if 'limit' in value and (type(value['limit']) is not int or value['limit'] < 0):
            raise PlanError('query.limit必须为非负整数')
        if 'max_return_cells' in value and (type(value['max_return_cells']) is not int or value['max_return_cells'] < 1):
            raise PlanError('query.max_return_cells必须为正整数')
        if value.get('reader', 'auto') not in ('auto', 'fast', 'compatible'):
            raise PlanError('query.reader须为auto/fast/compatible')
    def ref(value, required=('file',), extra=()):
        obj(value, ('file', 'sheet', 'table', 'header_row', 'query') + tuple(extra), required)
        for k in ('file', 'sheet', 'table'):
            if k in value: text(value[k])
        if 'header_row' in value and (type(value['header_row']) is not int or value['header_row'] < 1):
            raise PlanError('header_row必须为正整数物理行号')
        if 'query' in value: query(value['query'])
        if 'header_row' in value or 'query' in value:
            if Path(value['file']).suffix.lower() != '.xlsx' or 'table' in value:
                raise PlanError('header_row/query仅支持XLSX工作表来源，不能同时指定table')
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
        obj(join, ('source', 'left', 'right_field', 'keys', 'cardinality'), ('source', 'cardinality'))
        text(join['source'])
        if join['cardinality'] not in ('one_to_one', 'many_to_one', 'one_to_many', 'many_to_many'):
            raise PlanError('必须显式声明连接基数')
        if 'keys' in join:
            if 'left' in join or 'right_field' in join: raise PlanError('keys与旧单键left/right_field不能混用')
            pairs = seq(join['keys'])
            if not pairs: raise PlanError('复合连接keys不能为空')
            for pair in pairs:
                obj(pair, ('left', 'right_field'), ('left', 'right_field'))
                field(pair['left']); text(pair['right_field'])
        else:
            if 'left' not in join or 'right_field' not in join: raise PlanError('缺少连接键')
            field(join['left']); text(join['right_field'])
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


def _verify_plan(plan, base_dir=None, context=None):
    """All paths resolve relative to the plan file, never to a source instruction."""
    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan.get("version") != 1:
        raise PlanError("计划version必须为1")
    validate_plan_shape(plan)
    base = Path(base_dir or ".").resolve()
    resolve = lambda value: str((base / value).resolve())
    context = {} if context is None else context
    if context.get('base', base) != base:
        raise PlanError('批量核验上下文的计划目录不能变化')
    context['base'] = base
    if 'parser' not in context: context['parser'] = FileParser()
    fp = context['parser']
    parsed = context.setdefault('parsed', {})
    ingestion = context.setdefault('ingestion', {})
    hashes = context.setdefault('hashes', {})
    source_queries = context.setdefault('source_queries', {})

    def normalized_query(value):
        if isinstance(value, Decimal): return str(value)
        if isinstance(value, list): return [normalized_query(v) for v in value]
        if isinstance(value, dict): return {k: normalized_query(v) for k, v in value.items()}
        return value

    def retain_hash(path, digest):
        if path in hashes and hashes[path] != digest:
            raise PlanError('同次核验中来源文件发生变化，不能合并不同快照')
        hashes[path] = digest

    def read_source(ref, role, path, streamed, query, key):
        if key not in parsed:
            if streamed:
                from xlsx_query import query_xlsx
                packet = query_xlsx(path, sheet=ref.get('sheet'), header_row=ref.get('header_row', 1),
                                    **query, cache_dir=base / '.dd_cache' / 'xlsx_query')
                retain_hash(path, packet['source_sha256'])
                issues = [{**issue, 'severity': 'error' if issue.get('code') == 'excel_error_cell' else 'warning'}
                          for issue in packet.get('issues', [])]
                coverage = packet['coverage']
                for flag, required_value, code in (
                    ('scan_complete', True, 'query_scan_incomplete'),
                    ('truncated', False, 'query_rows_truncated'),
                    ('filter_evaluation_complete', True, 'query_filter_evaluation_incomplete'),
                ):
                    if coverage.get(flag) != required_value:
                        issues.append({'code': code, 'severity': 'warning', 'coverage': coverage})
                if coverage.get('expression_provenance_complete') is False:
                    issues.append({'code': 'query_formula_expression_provenance_incomplete', 'severity': 'warning'})
                units = []
                for row in packet['rows']:
                    cells = row['cells']
                    for cell in cells.values():
                        present = cell.get('formula_present', cell.get('formula') is not None)
                        if present and not cell.get('formula_cached'):
                            issues.append({'code': 'formula_missing_cache', 'severity': 'warning',
                                           'row_num': row['row_num'], 'address': cell['address']})
                        if present and cell.get('formula_expression_complete') is False:
                            issues.append({'code': 'formula_expression_unavailable', 'severity': 'warning',
                                           'row_num': row['row_num'], 'address': cell['address']})
                    fields = list(packet['columns'])
                    values = []
                    for field in fields:
                        value = row['values'][field]
                        cell = cells[field]
                        # Match FileParser's datetime/time semantics across
                        # readers and Excel epochs. Keep the original serial
                        # and explicit epoch untouched in metadata.cells.
                        if cell.get('iso_value') is not None:
                            try:
                                value = datetime.fromisoformat(cell['iso_value'])
                            except ValueError:
                                value = time.fromisoformat(cell['iso_value'])
                        values.append(value)
                    identity = json.dumps([path, packet['sheet'], row['row_num']], ensure_ascii=False)
                    units.append(AddressableUnit(
                        unit_id=hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24],
                        source_file=path, file_type='xlsx', row_num=row['row_num'], page_num=None,
                        paragraph_num=None, char_offset=None, sheet_name=packet['sheet'],
                        field_names=fields, field_values=values,
                        raw_text=' | '.join(f'{field}={value}' for field, value in zip(fields, values) if value is not None),
                        metadata={'cells': cells, 'location_kind': 'excel_physical_row',
                                  'reader': 'xlsx_query', 'header_row': packet['header_row'],
                                  'source_sha256': packet['source_sha256'], 'date_epoch': packet.get('date_epoch'),
                                  'ingestion_scope': 'explicit_query_filters_and_projected_columns'},
                    ))
                scope = {
                    'file': path, 'sheet': packet['sheet'], 'header_row': packet['header_row'],
                    'query': query, 'projected_columns': packet['columns'], 'coverage': coverage,
                    'source_sha256': packet['source_sha256'], 'cache': packet.get('cache'),
                    'elapsed_seconds': packet.get('elapsed_seconds'),
                    'scope': 'explicit_query_subset; plan_filters_apply_after_query',
                    'business_population_completeness_proven': False,
                    'scope_review_required': bool(query.get('where') or query.get('key_rows') or query.get('columns')),
                }
                parsed[key] = (units, scope)
            else:
                units = fp.parse(path)
                issues = list(getattr(fp, 'last_diagnostics', []))
                parsed[key] = (units, None)
                retain_hash(path, sha256_file(path))
            ingestion.setdefault(path, []).extend(issues)
            sidecar = Path(path + '.ocr.json')
            if sidecar.is_file():
                retain_hash(str(sidecar), sha256_file(sidecar))
        units, scope = parsed[key]
        if scope is not None:
            source_queries[role] = scope
        if ref.get("sheet") is not None:
            units = [u for u in units if u.sheet_name == ref["sheet"]]
        if ref.get("table") is not None:
            units = [u for u in units if u.metadata.get("table", u.sheet_name) == ref["table"]]
        return units

    def load(ref, role):
        path = resolve(ref['file'])
        streamed = 'header_row' in ref or 'query' in ref
        query = normalized_query(ref.get('query', {}))
        key = ('xlsx_query', path, ref.get('sheet'), ref.get('header_row', 1),
               json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(',', ':'))) if streamed else ('file', path)
        # Keep only an opaque ref/query key plus type/message. No exception,
        # traceback, source rows, or query scalar values enter the failure cache.
        failure_key = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode('utf-8')).hexdigest()
        failures = context.setdefault('load_errors', {})
        if failure_key in failures:
            failure = failures[failure_key]
            raise _CachedSourceLoadError(failure['type'], failure['message']) from None
        try:
            return read_source(ref, role, path, streamed, query, key)
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, DecimalException) as exc:
            failures[failure_key] = {'type': type(exc).__name__, 'message': str(exc)}
            raise

    result_ref = plan["result"]
    results = [u for u in load(result_ref, 'result') if u.row_num == result_ref["row"]]
    if len(results) != 1:
        raise PlanError("结果行必须唯一；明确工作表/表名及物理行号")
    result = results[0]
    rule_evidence = []
    for rule_index, anchor in enumerate(plan.get("rule_evidence", [])):
        quote = anchor.get("quote", "")
        if not isinstance(quote, str) or not quote.strip():
            raise PlanError("规则引文不能为空")
        matches = [u for u in load(anchor, f'rule_evidence:{rule_index}') if quote in u.raw_text]
        if not matches:
            raise PlanError("规则引文未在指定来源中找到")
        rule_evidence.append({"quote": quote, "locations": [location(u) for u in matches],
                              "validation": "substring_only_semantics_requires_review"})
    sources = {alias: [u for u in load(ref, f'source:{alias}') if not u.is_rule and not u.metadata.get('is_header_row')]
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
    bound_sources = {alias}
    for join in plan.get("joins", []):
        right = join["source"]
        keys = join.get('keys', [{'left': join.get('left'), 'right_field': join.get('right_field')}])
        cardinality = join["cardinality"]
        if right not in sources or right in bound_sources:
            raise PlanError("join来源未知或别名已使用")
        if cardinality not in ("one_to_one", "many_to_one", "one_to_many", "many_to_many"):
            raise PlanError("必须显式声明连接基数")
        if any(pair['left']['source'] not in bound_sources for pair in keys):
            raise PlanError("连接左侧来源未出现")

        def tuple_key(values):
            # Keep tuple boundaries. Concatenating keys would conflate (A, BC)
            # with (AB, C), or delimiter-bearing identifiers with other records.
            if any(value is None or value_text(value) == '' for value in values):
                return None
            return tuple(value_text(value) for value in values)

        lookup = defaultdict(list)
        unavailable_right_keys = 0
        for unit in sources[right]:
            key = tuple_key([get_value(unit, pair['right_field']) for pair in keys])
            if key is None:
                unavailable_right_keys += 1
            else:
                lookup[key].append(unit)
        new_rows, seen_left, missing = [], set(), 0
        for row in rows:
            key = tuple_key([get_value(row[pair['left']['source']], pair['left']['field']) for pair in keys])
            if key is None:
                raise PlanError('连接左键缺失/为空，不能把空键当成相同对象')
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
                      "keys": keys, "key_comparison": "componentwise_tuple; value_text normalization; no concatenation",
                      "input_rows": len(rows), "output_rows": len(new_rows), "unmatched_rows": missing,
                      "unavailable_right_key_rows": unavailable_right_keys})
        if missing:
            raise PlanError(f"{missing}条记录缺连接对象；不能以内连接静默丢失")
        rows = new_rows
        bound_sources.add(right)
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
                         "ingestion_complete": not incomplete, "source_queries": source_queries,
                         "query_scope_semantics": "query filters/projection are explicit extraction choices; completeness of business selection is not proven"},
            "ingestion_issues": incomplete}


def verify_plan(plan, base_dir=None):
    """Recompute one result row; retain the original single-row API."""
    return _verify_plan(plan, base_dir=base_dir)


def verify_plan_batch(plan, rows, base_dir=None):
    """Recompute requested physical rows against one shared set of source reads.

    Only result.row varies. Explicit source queries may batch all required keys;
    they are loaded once per unique reference/query within this invocation.
    Every requested row receives either a report or an explicit error record.
    """
    if not isinstance(plan, dict): raise PlanError('计划必须为JSON对象')
    if not isinstance(rows, (list, tuple)) or not rows:
        raise PlanError('批量rows必须为非空物理行号数组')
    context, results, statuses = {}, [], defaultdict(int)
    requested = list(rows)
    original_ref = plan.get('result', {})
    batch_base = Path(base_dir or '.').resolve()
    for request_index, row in enumerate(requested):
        ref = {**original_ref, 'row': row} if isinstance(original_ref, dict) else {'row': row}
        row_plan = {**plan, 'result': ref}
        try:
            report = _verify_plan(row_plan, base_dir=base_dir, context=context)
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, DecimalException) as exc:
            failed_ref = {k: ref.get(k) for k in ('file', 'sheet', 'table', 'row') if k in ref}
            if isinstance(failed_ref.get('file'), str):
                failed_ref['file'] = str((batch_base / failed_ref['file']).resolve())
            results.append({'request_index': request_index, 'row': row, 'status': 'error',
                            'result': failed_ref,
                            'error': str(exc), 'error_type': getattr(exc, 'original_error_type', type(exc).__name__),
                            'confidence': None})
        else:
            statuses[report['status']] += 1
            results.append({'request_index': request_index, 'row': row, 'status': 'processed', 'report': report})
    processed = sum(item['status'] == 'processed' for item in results)
    failed = len(results) - processed
    return {
        'status': 'batch_processed_with_errors' if failed else 'batch_processed',
        'analysis_stage': 'deterministic_reconciliation_batch', 'confidence': None,
        'semantics': 'processing_status_only; inspect_every_row_report; not_proof_of_historical_lineage',
        'summary': {'requested_rows': len(requested), 'processed_rows': processed,
                    'failed_rows': failed, 'report_status_counts': dict(statuses)},
        'results': results, 'source_sha256': dict(context.get('hashes', {})),
        'coverage': {'requested_physical_rows': requested, 'loaded_ref_snapshots': len(context.get('parsed', {})),
                     'failed_ref_snapshots': len(context.get('load_errors', {})),
                     'source_queries': dict(context.get('source_queries', {})),
                     'source_read_semantics': 'one_load_per_unique_file_or_explicit_query_in_this_batch'},
    }


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument('--rows', nargs='+', type=int,
                        help='Batch physical result rows, sharing source reads and exact query caches')
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        report = (verify_plan_batch(plan, args.rows, args.plan.resolve().parent) if args.rows is not None
                  else verify_plan(plan, args.plan.resolve().parent))
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, DecimalException) as exc:
        report = {"status": "invalid_or_unavailable", "error": str(exc), "confidence": None}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(report["status"])
    if report["status"] == "invalid_or_unavailable" or report.get('summary', {}).get('failed_rows', 0):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
