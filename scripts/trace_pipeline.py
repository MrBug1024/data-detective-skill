"""Batch source localization without materializing whole workbooks.

The pipeline reconstructs source relationships. It deliberately reports calculation,
rule applicability and historical execution as separate evidence dimensions.
"""
from __future__ import annotations

from collections import defaultdict, Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from zipfile import ZipFile

from source_profile import profile_source
from xlsx_query import query_xlsx
from record_query import query_records
from ingestion_support import sha256_file


TABULAR = {'.xlsx', '.xls', '.csv', '.tsv', '.json', '.jsonl', '.ndjson', '.sql'}
MATERIALIZED = {'.json', '.sql', '.xls'}
GENERIC_IDS = {'id', 'key', 'recordid', 'rowid', 'uuid', 'guid', '数据唯一记录号', '唯一记录号', '记录号'}
LOW_VALUES = {'', '0', '1', 'true', 'false', 'none', 'null', 'nan'}


@lru_cache(maxsize=8192)
def _normalized_name(text):
    # Schema labels repeat across every row/candidate. Cache only pure label
    # normalization, never record values or run-specific alias decisions.
    return re.sub(r'[\s_\-./:（）()]+', '', unicodedata.normalize('NFKC', text)).casefold()


def _name(value):
    return _normalized_name(str(value))


def _id_rank(field):
    name = _name(field)
    if name in GENERIC_IDS:
        return 25
    if name.endswith(('id', 'uuid', 'guid', 'ref', 'reference', '流水号', '记录号', '证件号码', '订单号', '合同号', '单据号')):
        return 80
    if name == 'sku' or name.endswith(('code', '代码', '编码')):
        return 45
    if name.endswith(('编号', 'key')):
        return 60
    if name in {'tenant', 'tenantid', '租户', '租户编号', 'namespace'}:
        return 70
    return 0


def _usable(value, explicit=False):
    if value is None or isinstance(value, (dict, list, bool)):
        return False
    text = str(value)
    return bool(text) and (explicit or text.casefold() not in LOW_VALUES)


def _token(value):
    # Keys retain lexical forms and leading zeros; booleans are not numeric IDs.
    return (type(value).__name__ if isinstance(value, bool) or value is None else 'scalar', str(value))


def _same(left, right, cell=None):
    if left is None or right is None or left == '' or right == '':
        return 'both_missing' if (left is None or left == '') and (right is None or right == '') else 'unresolved'
    if isinstance(left, bool) != isinstance(right, bool):
        return 'unresolved'
    if str(left) == str(right):
        return 'exact'
    iso = (cell or {}).get('iso_value')
    if iso:
        variants = {iso, iso.replace('T', ' ')}
        if iso.endswith('T00:00:00'):
            variants.add(iso[:10])
        if len(iso) == 19:
            variants.add(iso.replace('T', ' ') + '.000')
        if str(left) in variants:
            return 'format_equivalent'
    if str(left) in {'None', 'null'} or str(right) in {'None', 'null'}:
        return 'unresolved'
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        if a.is_finite() and b.is_finite():
            if a == b:
                return 'numeric_equal'
            if abs(a - b) < Decimal('0.0000001'):
                return 'representation_discrepancy'
    except InvalidOperation:
        pass
    return 'conflicting'


def _file_config(config, file):
    sources = config.get('sources', {})
    if not isinstance(sources, dict):
        raise ValueError('config.sources must map file paths or basenames to objects')
    value = sources.get(str(file), sources.get(file.name, {}))
    if not isinstance(value, dict):
        raise ValueError('Each source configuration must be an object')
    return value


def _table_options(options, sheet):
    """A table has its own grain, header and key namespace inside a workbook."""
    overrides = options.get('sheets', {})
    if not isinstance(overrides, dict):
        raise ValueError('source.sheets must map sheet/table names to configuration objects')
    specific = overrides.get(sheet, {})
    if not isinstance(specific, dict):
        raise ValueError('Each sheet configuration must be an object')
    merged = {**{k: v for k, v in options.items() if k != 'sheets'}, **specific}
    if overrides:
        merged['sheet'] = sheet
    return merged


def _aliases(config):
    aliases = {}
    for canonical, variants in config.get('field_aliases', {}).items():
        if isinstance(variants, str):
            variants = [variants]
        for field in [canonical, *variants]:
            key = _name(field)
            if key in aliases and aliases[key] != _name(canonical):
                raise ValueError('A field alias cannot map to two canonical names')
            aliases[key] = _name(canonical)
    @lru_cache(maxsize=8192)
    def canonical(field):
        normalized = _name(field)
        return aliases.get(normalized, normalized)
    return canonical


def _location(file, sheet, row, digest):
    result = {'file': str(file), 'sheet': sheet, 'row_num': row['row_num'], 'source_sha256': digest}
    for field in ('line_end', 'char_offset', 'byte_offset', 'byte_end', 'offset_scope', 'record_pointer',
                  'json_pointer', 'row_num_kind', 'tuple_num', 'statement_num', 'statement_span'):
        if field in row:
            result[field] = row[field]
    return result


def _bounded_json(file):
    """Ordinary JSON has explicit object/array grain and bounded materialization."""
    from parsers import FileParser
    if file.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('JSON exceeds the 64MiB object/array budget; convert with declared record grain to JSONL')
    before = sha256_file(file)
    parser = FileParser()
    units = parser.parse(str(file))
    rows = []
    fields = []
    for unit in units:
        data = unit.to_dict()
        values = dict(zip(data['field_names'], data['field_values']))
        fields.extend(name for name in values if name not in fields)
        metadata = data['metadata']
        rows.append({'row_num': metadata['record_num'], 'row_num_kind': 'json_record_index',
                     'values': values, 'json_pointer': metadata['json_pointer'],
                     'field_pointers': metadata['field_pointers'], 'value_types': metadata['value_types']})
    if sha256_file(file) != before:
        raise ValueError('JSON source changed during parsing')
    packet = {'file': str(file), 'sheet': None, 'source_sha256': before, 'rows': rows,
              'coverage': {'scan_complete': True, 'filter_evaluation_complete': True, 'truncated': False,
                           'scanned_rows': len(rows), 'matched_rows': len(rows), 'returned_rows': len(rows)},
              'issues': list(getattr(parser, 'last_diagnostics', []))}
    table = {'sheet': None, 'header_row': None, 'header_status': 'selected',
             'header_selection': 'json_object_field_names', 'single_sheet': True,
             'fields': [{'name': name} for name in fields], 'sample_rows': rows[:12],
             'record_grain': 'top_level_object_or_array_element; nested_arrays_stay_in_record'}
    profile = {'file': str(file), 'format': 'json', 'source_sha256': before,
               'sheets': [table], 'coverage': {'scope': 'bounded_complete_json_parse'}}
    return packet, profile


def _bounded_tables(file):
    """Use the existing static parser for bounded SQL and legacy XLS sources.

    SQL statements are never executed. Multiple tuples on one physical line
    retain statement/tuple offsets; their line number alone is not an identity.
    """
    if file.suffix.lower() == '.json':
        packet, profile = _bounded_json(file)
        return {None: packet}, profile
    from parsers import FileParser
    if file.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Static SQL/legacy XLS exceeds the 16MiB materialization budget; use a declared streaming export')
    digest = sha256_file(file)
    parser = FileParser()
    units = parser.parse(str(file))
    packets, profiles, total_cells, total_rows = {}, [], 0, 0
    for unit in units:
        data = unit.to_dict()
        meta = data['metadata']
        if file.suffix.lower() == '.sql' and not meta.get('structured'):
            continue
        fields = data['field_names']
        total_rows += 1; total_cells += len(fields)
        if total_rows > 100000 or total_cells > 500000:
            raise ValueError('Static structured input exceeds the record/cell budget')
        sheet = data['sheet_name'] or meta.get('table')
        packet = packets.setdefault(sheet, {'file': str(file), 'sheet': sheet, 'source_sha256': digest,
                                            'rows': [], 'issues': []})
        record = {'row_num': data['row_num'], 'values': dict(zip(fields, data['field_values'])),
                  'cells': meta.get('cells', {}), 'char_offset': data.get('char_offset')}
        for key in ('tuple_num', 'statement_num', 'statement_span', 'line_end', 'offset_scope'):
            if key in meta:
                record[key] = meta[key]
        record['row_num_kind'] = 'sql_physical_line_and_tuple' if file.suffix.lower() == '.sql' else 'excel_physical_row'
        packet['rows'].append(record)
    if sha256_file(file) != digest:
        raise ValueError('Structured source changed during parsing')
    for sheet, packet in packets.items():
        rows = packet['rows']
        packet['issues'] = list(getattr(parser, 'last_diagnostics', []))
        packet['coverage'] = {'scan_complete': True, 'filter_evaluation_complete': True, 'truncated': False,
                              'scanned_rows': len(rows), 'matched_rows': len(rows), 'returned_rows': len(rows)}
        names = list(dict.fromkeys(name for row in rows for name in row['values']))
        profiles.append({'sheet': sheet, 'header_row': 1 if file.suffix.lower() == '.xls' else None,
                         'header_status': 'selected', 'header_selection': 'parser_declared_fields',
                         'fields': [{'name': name} for name in names], 'sample_rows': rows[:12],
                         'single_sheet': len(packets) == 1})
    return packets, {'file': str(file), 'format': file.suffix.lower().lstrip('.'), 'source_sha256': digest,
                     'issues': list(getattr(parser, 'last_diagnostics', [])),
                     'sheets': profiles, 'coverage': {'scope': 'bounded_complete_static_parse', 'sql_executed': False}}


def _read_table(file, table, options, cache_dir, progress, *, where=None, key_rows=None, limit=None):
    query = dict(options.get('query', {}))
    allowed = {'where', 'key_rows', 'columns', 'limit', 'reader', 'max_return_cells', 'shared_string_memory_bytes', 'snapshot_dir'}
    unknown = set(query) - allowed
    if unknown:
        raise ValueError('Unsupported source query options: ' + ', '.join(sorted(unknown)))
    for field in ('columns', 'reader', 'max_return_cells', 'snapshot_dir'):
        if field in options:
            query[field] = options[field]
    fixed = dict(query.pop('where', {}))
    fixed.update(options.get('where', {}))
    if where:
        for field, values in where.items():
            if field in fixed:
                before = {_token(v) for v in fixed[field]}
                values = [v for v in values if _token(v) in before]
            fixed[field] = values
    if fixed:
        query['where'] = fixed
    if key_rows is not None:
        if 'key_rows' in query:
            raise ValueError('Do not combine generated key tuples with a second query.key_rows')
        query['key_rows'] = key_rows
    if limit is not None:
        query['limit'] = min(limit, query.get('limit', limit))
    if 'snapshot_dir' in query and file.suffix.lower() != '.xlsx':
        raise ValueError('Persistent source snapshots currently apply to XLSX inputs only')
    if file.suffix.lower() in MATERIALIZED:
        original = options['_materialized_packets'][table.get('sheet')]
        if sha256_file(file) != original['source_sha256']:
            raise ValueError('Structured source changed after schema discovery')
        selected, unavailable = [], 0
        tuples = query.get('key_rows')
        for record in original['rows']:
            values = record['values']
            required = set(query.get('where', {})) | (set(tuples[0]) if tuples else set())
            if required - set(values):
                unavailable += 1
                continue
            if any(_token(values[field]) not in {_token(v) for v in allowed}
                   for field, allowed in query.get('where', {}).items()):
                continue
            if tuples is not None and not any(all(_token(values[f]) == _token(v) for f, v in key.items()) for key in tuples):
                continue
            selected.append(record)
        maximum = query.get('limit')
        returned = selected if maximum is None else selected[:maximum]
        missing_projection = 0
        if query.get('columns'):
            observed = {f for r in original['rows'] for f in r['values']}
            if set(query['columns']) - observed:
                raise ValueError('Configured structured projection contains unavailable fields')
            missing_projection = sum(bool(set(query['columns']) - set(r['values'])) for r in selected)
            returned = [{**r, 'values': {f: r['values'][f] for f in query['columns'] if f in r['values']},
                         'field_pointers': {f: r.get('field_pointers', {})[f] for f in query['columns'] if f in r.get('field_pointers', {})},
                         'cells': {f: r.get('cells', {})[f] for f in query['columns'] if f in r.get('cells', {})}}
                        for r in returned]
        if sum(len(r['values']) for r in returned) > query.get('max_return_cells', 500000):
            raise ValueError('Structured evidence exceeds the return-cell budget')
        packet = {**original, 'rows': returned, 'where': query.get('where', {}), 'key_rows': tuples,
                  'coverage': {**original['coverage'], 'matched_rows': len(selected), 'returned_rows': len(returned),
                               'truncated': len(returned) < len(selected), 'filter_evaluation_complete': unavailable == 0,
                               'projection_complete': missing_projection == 0,
                               'unavailable_filter_rows': unavailable}}
        if sha256_file(file) != original['source_sha256']:
            raise ValueError('Structured source changed while selecting records')
    elif file.suffix.lower() == '.xlsx':
        # Omit the single-sheet name to reuse callers' content-keyed queries.
        sheet = options.get('sheet') if table.get('single_sheet', False) else table.get('sheet')
        packet = query_xlsx(file, sheet=sheet, header_row=table['header_row'],
                            cache_dir=Path(cache_dir) / 'xlsx_query', progress=progress, **query)
    else:
        query.pop('reader', None)
        query.pop('shared_string_memory_bytes', None)
        packet = query_records(file, encoding=options.get('encoding', 'utf-8-sig'),
                               delimiter=options.get('delimiter'), progress=progress, **query)
        packet['rows'] = packet.pop('records')
        packet['file'] = packet.pop('source_file')
        packet['sheet'] = None
    return packet


def _rules_table(table):
    fields = ' '.join(f['name'] for f in table.get('fields', []))
    return bool(re.search(r'规则|依据|问题清单|rule|policy|regulation', fields, re.I))


def _full_text_budget(file, maximum):
    size = file.stat().st_size
    if file.suffix.lower() == '.xlsx':
        with ZipFile(file) as archive:
            size = sum(i.file_size for i in archive.infolist()
                       if (i.filename.startswith('xl/worksheets/') and i.filename.endswith('.xml'))
                       or i.filename == 'xl/sharedStrings.xml')
    return size <= maximum


def _known_context(values, canon, file=None, sheet=None):
    known = {}
    for field, value in values.items():
        known.setdefault(canon(field), []).append({'field': field, 'value': value, 'origin': 'result',
                                                 'file': str(file) if file else None, 'sheet': sheet})
    return known


def _known_value(context, canonical):
    primary = _known_entries(context, canonical)
    tokens = {_token(entry['value']) for entry in primary}
    return primary[0]['value'] if len(tokens) == 1 else None


def _known_reference(value):
    if isinstance(value, str):
        return {'field': value}
    if not isinstance(value, dict) or not isinstance(value.get('field'), str) or set(value) - {'field', 'file', 'sheet', 'origin'}:
        raise ValueError('A key_map reference must be a field name or {field, file?, sheet?, origin?}')
    return value


def _known_entries(context, canonical, reference=None):
    candidates = [entry for entry in context.get(canonical, []) if _usable(entry['value'], True)]
    reference = reference or {}
    if 'file' in reference:
        wanted = str(reference['file'])
        candidates = [e for e in candidates if e.get('file') == wanted or Path(e.get('file') or '').name == wanted]
    if 'sheet' in reference:
        candidates = [e for e in candidates if e.get('sheet') == reference['sheet']]
    if 'origin' in reference:
        candidates = [e for e in candidates if e['origin'] == reference['origin']]
    if not candidates:
        return []
    # Result columns take precedence over newly connected records of another grain.
    primary = [entry for entry in candidates if entry['origin'] == 'result'] or candidates
    return primary


def _key_provenance(source, reference, entries):
    item = {'source_field': source, 'known_field': reference['field'], 'known_reference': reference,
            'origins': [{'origin': entry['origin'], 'field': entry['field'],
                         **({'identifier_namespace': entry['identifier_namespace']} if entry.get('identifier_namespace') else {})}
                        for entry in entries],
            'relationship': 'reconstructed_key_join'}
    namespaces = {entry['identifier_namespace'] for entry in entries if entry.get('identifier_namespace')}
    if len(namespaces) == 1:
        item['known_identifier_namespace'] = next(iter(namespaces))
    upstream = [key for entry in entries for key in entry.get('upstream_key_provenance', [])]
    if upstream:
        item['upstream_key_provenance'] = upstream
    return item


def _walk_key_provenance(items):
    for item in items:
        yield item
        yield from _walk_key_provenance(item.get('upstream_key_provenance', []))


def _key_plan(table, options, contexts, canon):
    fields = [f['name'] for f in table.get('fields', [])]
    targets = options.get('_target_indices', list(range(len(contexts))))
    attempted = options.get('_attempted_keysets', [])
    explicit = bool(options.get('keys') or options.get('key_map'))
    if options.get('key_map'):
        mapping = dict(options['key_map'])
    elif options.get('keys'):
        mapping = {field: field for field in options['keys']}
    else:
        candidates = []
        for field in fields:
            score = _id_rank(field) or options.get('_identifier_ranks', {}).get(field, 0)
            if field in options.get('_scope_fields', []):
                continue
            available = sum(_usable(_known_value(contexts[i], canon(field))) for i in targets)
            original = sum(any(e['origin'] == 'result' and _usable(e['value'])
                               for e in contexts[i].get(canon(field), [])) for i in targets)
            if score and available:
                candidates.append((score, original, available, field))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0], -x[1], -x[2], x[3]))
        mapping = None
        for score, _, _, field in candidates:
            # A code can identify a reference/catalogue record. In a fact
            # table with a transaction key it only narrows that transaction.
            if score == 45:
                descriptive = any(not _id_rank(other) and any(
                    _informative([_known_value(contexts[i], canon(other))]) for i in targets) for other in fields)
                if any(_id_rank(other) >= 60 for other in fields) or not descriptive:
                    continue
            candidate = {field: field}
            for other in fields:
                if (canon(other) in {'tenant', 'tenantid', '租户', '租户编号', 'namespace'}
                        or other in options.get('_scope_fields', [])) and other != field:
                    # Scope is part of identity, including when a result lacks
                    # it. Omitting it for a missing row would broaden the join.
                    candidate[other] = other
                elif _id_rank(other) == 45 and other != field:
                    if all(_usable(_known_value(contexts[i], canon(other))) for i in targets):
                        candidate[other] = other
            signature = tuple(sorted(candidate.items()))
            if signature not in attempted:
                mapping = candidate
                break
        if mapping is None:
            return None
    if set(mapping) - set(fields):
        raise ValueError('Configured key field missing from source header')
    signature = tuple((source, json.dumps(target, sort_keys=True, ensure_ascii=False)) for source, target in sorted(mapping.items())) if explicit else tuple(sorted(mapping.items()))
    if signature in attempted:
        return None
    keys, by_result, provenance = [], {}, {}
    references = {source: _known_reference(target) for source, target in mapping.items()}
    for index in targets:
        ctx = contexts[index]
        entries = {source: _known_entries(ctx, canon(reference['field']), reference) for source, reference in references.items()}
        values = {source: found[0]['value'] if len({_token(e['value']) for e in found}) == 1 else None
                  for source, found in entries.items()}
        if all(_usable(v, explicit) for v in values.values()):
            by_result[index] = values
            provenance[index] = [_key_provenance(source, references[source], entries[source]) for source in mapping]
            if values not in keys:
                keys.append(values)
    if not keys:
        return None
    return {'mapping': mapping, 'key_rows': keys, 'by_result': by_result, 'provenance': provenance,
            'targets': targets, 'signature': signature,
            'score': 100 if explicit else max(_id_rank(f) or options.get('_identifier_ranks', {}).get(f, 0) for f in mapping),
            'explicit': explicit}


def _compare_row(result, source, key_fields, canon, field_map=None):
    source_fields = defaultdict(list)
    for field in source['values']:
        source_fields[canon(field)].append(field)
    comparisons, exact, conflicts, hard = [], set(), set(), []
    for field, value in result.items():
        alternatives = source_fields.get(canon(field), [])
        declared = (field_map or {}).get(field)
        if declared in source['values'] and declared not in alternatives:
            alternatives = [*alternatives, declared]
        for other in alternatives:
            source_value = source['values'][other]
            status = _same(value, source_value, source.get('cells', {}).get(other))
            if _id_rank(field) and _usable(value, True) and _usable(source_value, True) and _token(value) != _token(source_value):
                status = 'conflicting'
            comparisons.append({'result_field': field, 'source_field': other, 'status': status})
            if not _usable(value, True):
                continue
            if status in ('exact', 'format_equivalent', 'numeric_equal'):
                exact.add(canon(field))
            elif status == 'conflicting':
                conflicts.add(canon(field))
                if _id_rank(field) >= 60 and other not in key_fields:
                    hard.append(field)
    return comparisons, exact, conflicts, hard


def _resolve_candidates(result, candidates, key_fields, canon, field_map=None):
    evaluated = []
    rejected = []
    for row in candidates:
        comparisons, equal, conflicts, hard = _compare_row(result, row, key_fields, canon, field_map)
        row_identifiers = [c for c in comparisons if _name(c['result_field']) in GENERIC_IDS
                           and c['status'] == 'exact' and _usable(result[c['result_field']])]
        item = {'row': row, 'comparisons': comparisons, 'equal': equal, 'conflicts': conflicts,
                'record_identifier_matched': bool(row_identifiers)}
        if hard:
            rejected.append({'row_num': row['row_num'], 'reason': 'conflicting_stable_identifier', 'fields': hard})
        else:
            evaluated.append(item)
    supported_record_ids = [item for item in evaluated if item['record_identifier_matched']
                            and all(item['conflicts'] <= other['conflicts'] for other in evaluated)]
    if supported_record_ids:
        # A matching generic row ID is usable only when it introduces no
        # contradictory field beyond any alternative. Coincident IDs from
        # another grain must not override otherwise conflicting records.
        for item in evaluated:
            if item not in supported_record_ids:
                rejected.append({'row_num': item['row']['row_num'], 'reason': 'different_record_identifier',
                                 'field_comparisons': item['comparisons'],
                                 **{key: item['row'][key] for key in ('tuple_num', 'char_offset', 'json_pointer') if key in item['row']}})
        evaluated = supported_record_ids
    # A compatible unique stable-key record is a location even if fields differ.
    # For duplicate keys only strict evidence dominance may distinguish rows.
    retained = []
    for item in evaluated:
        dominated = any(other is not item and other['equal'] > item['equal']
                        and other['conflicts'] <= item['conflicts'] for other in evaluated)
        if not dominated:
            retained.append(item)
        else:
            source = item['row']
            rejected.append({'row_num': source['row_num'], 'reason': 'strict_evidence_dominance',
                             'field_comparisons': item['comparisons'],
                             **{key: source[key] for key in ('tuple_num', 'char_offset', 'json_pointer') if key in source}})
    return retained, rejected


def _identity_support(result, item, plan, canon):
    if plan['explicit']:
        return True, 'declared_key_mapping'
    if any(_name(field) not in GENERIC_IDS for field in plan['mapping']):
        return True, 'semantic_identifier_with_conflict_checks'
    key_names = {canon(field) for field in plan['mapping']}
    for comparison in item['comparisons']:
        field = comparison['result_field']
        if canon(field) in key_names or comparison['status'] not in ('exact', 'format_equivalent', 'numeric_equal'):
            continue
        if canon(field) in {'gender', 'sex', 'status', 'state', 'flag', 'active', '性别', '状态', '标志'}:
            continue
        if _usable(result.get(field)):
            return True, 'generic_identifier_with_corroborating_field'
    return False, 'generic_identifier_without_corroboration'


def _invalidate_conflicting_dependencies(result_rows, matches, contexts, canon, queries=()):
    """A later contradictory key invalidates earlier dependent reconstructions.

    This is dependency propagation over actually used key origins, not an
    assumption that every similarly named field shares a historical lineage.
    Independent records located using the original result key stay locatable.
    """
    issues = []
    def request_key(provenance):
        reference = provenance.get('known_reference', {'field': provenance['known_field']})
        return json.dumps({**reference, 'field': canon(provenance['known_field'])}, sort_keys=True, ensure_ascii=False)
    for index, row_matches in enumerate(matches):
        requests = [p for match in row_matches for p in match.get('key_provenance', [])]
        requests.extend(p for query in queries for entry in query.get('key_provenance', [])
                        if entry['result_row'] == result_rows[index]['row_num'] for p in entry['keys'])
        requests = list(_walk_key_provenance(requests))
        used = {request_key(p): p for p in requests if any(o['origin'] != 'result' for o in p['origins'])}
        conflicts = {}
        for key, provenance in used.items():
            entries = _known_entries(contexts[index], canon(provenance['known_field']), provenance.get('known_reference'))
            if any(e['origin'] == 'result' for e in entries):
                continue
            if len({_token(e['value']) for e in entries}) > 1:
                conflicts[key] = [{'origin': e['origin'], 'field': e['field'], 'value': e['value']} for e in entries]
        if not conflicts:
            continue
        invalid = set()
        changed = True
        while changed:
            changed = False
            for match in row_matches:
                if match['status'] != 'located':
                    continue
                bad = [p for p in _walk_key_provenance(match.get('key_provenance', []))
                       if request_key(p) in conflicts
                       or any(o['origin'] in invalid for o in p['origins'])]
                if bad:
                    match['status'] = 'ambiguous'
                    match['localization_reason'] = 'conflicting_propagated_identifier'
                    match['invalidated_key_provenance'] = bad
                    invalid.add(match['source_ref'])
                    changed = True
        issues.append({'code': 'conflicting_propagated_identifier', 'severity': 'warning',
                       'result_row': result_rows[index]['row_num'], 'conflicting_fields': conflicts,
                       'invalidated_source_refs': sorted(invalid)})
    return issues


def _blocked_key_conflicts(table, options, contexts, canon, result_rows):
    """Explain an unexecutable join without inventing an executed dependency.

    When two linking records arrive before their dependent table, no query is
    issued at all. That must expose the same disagreement as a conflict found
    after an earlier query, while keeping planned and actually used keys apart.
    """
    fields = [f['name'] for f in table.get('fields', [])]
    mapping = options.get('key_map') or {f: f for f in options.get('keys', [])}
    if not mapping:
        mapping = {f: f for f in fields if _id_rank(f) and _id_rank(f) != 45}
    issues = []
    for index in options.get('_target_indices', range(len(contexts))):
        conflicts = []
        for source, target in mapping.items():
            reference = _known_reference(target)
            entries = _known_entries(contexts[index], canon(reference['field']), reference)
            if any(e['origin'] == 'result' for e in entries):
                continue
            if len({_token(e['value']) for e in entries if _usable(e['value'], True)}) > 1:
                conflicts.append({'source_field': source, 'known_reference': reference,
                                  'origins': [{'origin': e['origin'], 'field': e['field'], 'value': e['value']}
                                              for e in entries if _usable(e['value'], True)]})
        if conflicts:
            issues.append({'code': 'conflicting_propagated_identifier', 'severity': 'warning',
                           'result_row': result_rows[index]['row_num'], 'phase': 'key_planning',
                           'blocked_keys': conflicts, 'invalidated_source_refs': []})
    return issues


def _invalidate_document_dependencies(result_rows, matches, evidence):
    """A later explicit withdrawal also invalidates records reached earlier."""
    rejected = {bridge['bridge_id'] for item in evidence for bridge in item.get('bridge_candidates', [])
                if bridge.get('can_seed_lookup') is False}
    issues = []
    for index, row in enumerate(result_rows):
        invalid = set()
        for seed in row.get('document_lookup_seeds', []):
            blocked = sorted({step['bridge_id'] for step in seed.get('provenance', [])
                              if step.get('bridge_id') in rejected})
            if blocked:
                seed['lookup_status'] = 'invalidated'
                seed['invalidated_bridge_ids'] = blocked
                invalid.add(seed['seed_ref'])
        if not invalid:
            continue
        changed = True
        while changed:
            changed = False
            for match in matches[index]:
                if match['status'] == 'located' and any(origin['origin'] in invalid
                        for key in _walk_key_provenance(match.get('key_provenance', [])) for origin in key['origins']):
                    match['status'] = 'ambiguous'
                    match['localization_reason'] = 'document_bridge_invalidated'
                    invalid.add(match['source_ref'])
                    changed = True
        issues.append({'code': 'document_bridge_invalidated', 'severity': 'warning', 'result_row': row['row_num'],
                       'invalidated_refs': sorted(invalid)})
    return issues


def _refine_ambiguous_candidates(result_rows, matches, contexts, canon):
    """Refine complete candidate blocks using later independently located keys.

    This never rescans a source or relaxes an earlier predicate. A new key must
    have one nonempty value from an independent origin, and every excluded
    candidate must have a different nonempty value. Missing fields cannot
    eliminate alternatives. Prior candidates remain in the row's audit history.
    """
    changed = True
    while changed:
        changed = False
        for index, items in enumerate(matches):
            groups = defaultdict(list)
            for item in items:
                if item['status'] == 'ambiguous' and item.get('query_complete'):
                    groups[(item['file'], item.get('sheet'))].append(item)
            for (file, sheet), group in groups.items():
                if len(group) < 2:
                    continue
                refs = {item['source_ref'] for item in group}
                existing = {canon(f) for item in group for f in item['match_keys']}
                field_names = set.intersection(*(set(item['values']) for item in group))
                additions = []
                for field in sorted(field_names):
                    if not _id_rank(field) or _name(field) in GENERIC_IDS or canon(field) in existing:
                        continue
                    entries = [e for e in _known_entries(contexts[index], canon(field))
                               if e['origin'] not in refs and (e.get('file'), e.get('sheet')) != (file, sheet)]
                    tokens = {_token(e['value']) for e in entries}
                    if len(tokens) != 1 or not all(_usable(item['values'][field], True) for item in group):
                        continue
                    additions.append((field, next(iter(tokens)), entries))
                if not additions:
                    continue
                compatible = [item for item in group if all(_token(item['values'][f]) == token for f, token, _ in additions)]
                if len(compatible) != 1:
                    continue
                chosen = compatible[0]
                extra = [_key_provenance(field, {'field': field}, entries) for field, _, entries in additions]
                rejected = [{'row_num': item['row_num'], 'source_ref': item['source_ref'],
                             'location': item['location'], 'reason': 'independent_additional_key',
                             'fields': [f for f, token, _ in additions if _token(item['values'][f]) != token]}
                            for item in group if item is not chosen]
                refined = {**chosen, 'status': 'located', 'localization_reason': 'independent_additional_key',
                           'match_keys': [*chosen['match_keys'], *(f for f, _, _ in additions)],
                           'key_mapping': {**chosen['key_mapping'], **{f: f for f, _, _ in additions}},
                           'key_provenance': [*chosen['key_provenance'], *extra],
                           'retained_candidate_count': 1,
                           'rejected_candidates': [*chosen['rejected_candidates'], *rejected]}
                result_rows[index].setdefault('source_candidate_history', []).append({
                    'reason': 'refined_using_independent_keys', 'additional_key_provenance': extra,
                    'candidates': group, 'selected_source_ref': chosen['source_ref']})
                items[:] = [item for item in items if item not in group]
                items.append(refined)
                for field, value in refined['values'].items():
                    contexts[index].setdefault(canon(field), []).append({'field': field, 'value': value,
                        'origin': refined['source_ref'], 'file': file, 'sheet': sheet})
                changed = True


def _informative(values):
    values = [v for v in values if _usable(v)]
    if not values:
        return False
    distinct = {str(v) for v in values}
    if len(distinct) >= 2 and len(values) >= 2:
        return True
    value = str(values[0])
    return len(value) >= 5 and not re.fullmatch(r'[-+0-9.eE]+', value)


def _field_mappings(result_rows, matches, canon, configurations):
    # Infer renamed fields from whole requested-row value vectors, after IDs have
    # independently localized records. Equality alone never establishes a join.
    vectors = defaultdict(dict)
    for index, items in enumerate(matches):
        for item in items:
            if item['status'] != 'located':
                continue
            for field, value in item['values'].items():
                vectors[(item['file'], item.get('sheet'), field)][index] = value
    inferred = defaultdict(list)
    fields = list(dict.fromkeys(field for r in result_rows for field in r['values']))
    for field in fields:
        for key, values in vectors.items():
            comparable = [(i, result_rows[i]['values'].get(field), value) for i, value in values.items()
                          if _usable(result_rows[i]['values'].get(field)) and _usable(value)]
            if len(comparable) < min(2, len(result_rows)) or not _informative([a for _, a, _ in comparable]):
                continue
            if all(_same(a, b) in ('exact', 'numeric_equal') for _, a, b in comparable):
                inferred[field].append((key, len(comparable)))
    output = []
    for index, row in enumerate(result_rows):
        for field, value in row['values'].items():
            sources = []
            for item in matches[index]:
                if item['status'] != 'located':
                    continue
                options = _table_options(configurations.get(item['file'], {}), item.get('sheet'))
                explicit_field = options.get('field_map', {}).get(field)
                candidates = [(f, 'explicit_mapping', None) for f in [explicit_field] if f in item['values']]
                candidates += [(f, 'same_field_name_or_alias', None) for f in item['values'] if canon(f) == canon(field)]
                candidates += [(key[2], 'requested_row_value_vector', n) for key, n in inferred[field]
                               if key[:2] == (item['file'], item.get('sheet'))]
                seen = set()
                for source_field, method, count in candidates:
                    if source_field in seen:
                        continue
                    seen.add(source_field)
                    cell = item.get('cells', {}).get(source_field, {})
                    status = _same(value, item['values'][source_field], cell)
                    if _id_rank(field) and _usable(value, True) and _usable(item['values'][source_field], True) and _token(value) != _token(item['values'][source_field]):
                        status = 'conflicting'
                    sources.append({'source_ref': item['source_ref'], 'source_field': source_field,
                                    'address': cell.get('address'),
                                    'pointer': item.get('field_pointers', {}).get(source_field),
                                    'status': status, 'mapping_method': method,
                                    'vector_nonempty_rows': count})
            consistent = [s for s in sources if s['status'] in ('exact', 'numeric_equal', 'format_equivalent')]
            if consistent:
                status = 'mapped' if all(s['mapping_method'] == 'requested_row_value_vector' for s in consistent) else (
                    'format_equivalent' if all(s['status'] == 'format_equivalent' for s in consistent) else 'exact')
                inferred_by_record = defaultdict(set)
                for source in consistent:
                    if source['mapping_method'] == 'requested_row_value_vector':
                        inferred_by_record[source['source_ref']].add(source['source_field'])
                if status == 'mapped' and any(len(names) > 1 for names in inferred_by_record.values()):
                    status = 'candidate_ambiguous'
            elif any(s['status'] == 'representation_discrepancy' for s in sources):
                status = 'representation_discrepancy'
            elif sources and all(s['status'] == 'conflicting' for s in sources):
                status = 'conflicting'
            else:
                status = 'unresolved'
            output.append({'result_row': row['row_num'], 'result_field': field,
                           'result_address': row.get('cells', {}).get(field, {}).get('address'),
                           'status': status, 'sources': sources,
                           'historical_origin_verified': False})
    return output


def _document_scope_field(field, config):
    from document_entities import scope_namespace
    return scope_namespace(field, config)


def _verified_record_bridge(step, profiles):
    location = step.get('source_location') or {}
    if Path(location.get('file', '')).suffix.lower() not in TABULAR:
        return True
    proof = step.get('record_localization') or {}
    address = proof.get('location') or {}
    digest = step.get('source_sha256')
    if (proof.get('status') != 'located' or proof.get('query_complete') is not True or
            not isinstance(address, dict) or not digest or digest != proof.get('source_sha256') or
            digest != profiles.get(location.get('file'), {}).get('source_sha256')):
        return False
    expected_ref = 'src_' + hashlib.sha256(json.dumps(address, sort_keys=True).encode()).hexdigest()[:20]
    if proof.get('source_ref') != expected_ref or address.get('file') != location.get('file') or address.get('sheet') != location.get('sheet'):
        return False
    metadata = location.get('metadata') or {}
    pointer = location.get('json_pointer', metadata.get('json_pointer'))
    if pointer is not None:
        return pointer == address.get('json_pointer')
    return (location.get('row', location.get('row_num')) == address.get('row_num') and
            location.get('tuple_num', metadata.get('tuple_num')) == address.get('tuple_num'))


def _inject_document_seeds(seeds, result_rows, contexts, profiles, canon, config, seen):
    """Expose only affirmative typed bridge destinations as exact lookup keys."""
    from document_entities import identifier_namespace
    by_number = {row['row_num']: i for i, row in enumerate(result_rows)}
    table_fields = {f['name'] for p in profiles.values() for t in p.get('sheets', []) for f in t.get('fields', [])}
    added = 0
    for seed in seeds:
        index = by_number.get(seed.get('result_row', seed.get('target_index', 1)))
        namespace = seed.get('identifier_namespace', seed.get('namespace'))
        scope = seed.get('scope') or {'values': {}, 'ambiguous': {}}
        if index is None or seed.get('can_seed_lookup') is not True or not namespace or namespace == 'unspecified' or scope.get('ambiguous'):
            continue
        direct_query = (seed.get('source_location') or {}).get('kind') == 'user_query'
        if not _usable(seed.get('value'), True) or not seed.get('evidence_id') or (not seed.get('bridge_id') and not direct_query):
            continue
        # An unlocated table cannot supply its own missing scope/foreign key
        # through text retrieval. Structured keys enter through verified query
        # records; prose bridges retain their independent document assertion.
        if any(not _verified_record_bridge(step, profiles) for step in seed.get('provenance', [])):
            continue
        location = seed.get('source_location') or {}
        anchor = seed.get('anchor') or {}
        anchor_field = anchor.get('field')
        anchor_keys = []
        if anchor_field:
            entries = [entry for entry in _known_entries(contexts[index], canon(anchor_field))
                       if _token(entry['value']) == _token(anchor.get('value'))]
            if entries:
                anchor_keys = [_key_provenance(anchor_field, {'field': anchor_field}, entries)]
                anchor = {**anchor, 'origin': 'result_field' if all(e['origin'] == 'result' for e in entries) else 'source_key'}
                seed = {**seed, 'anchor': anchor, 'anchor_key_provenance': anchor_keys}
        for step in seed.get('provenance', []):
            proof = step.get('record_localization')
            if proof:
                reference = {'field': 'record_note', 'origin': proof['source_ref']}
                anchor_keys.append({'source_field': 'record_note', 'known_field': 'record_note', 'known_reference': reference,
                                    'origins': [{'origin': proof['source_ref'], 'field': 'record_note'}],
                                    'relationship': 'located_record_text_bridge'})
        if anchor_keys:
            seed = {**seed, 'anchor_key_provenance': anchor_keys}
        from lineage_graph import seed_reference
        seed = {**seed, 'seed_ref': seed_reference(seed)}
        signature = (index, seed['seed_ref'])
        if signature in seen:
            continue
        assignments = [(field, seed['value'], namespace) for field in table_fields
                       if identifier_namespace(field, config) == namespace]
        assignments += [(field, scope['values'][kind], 'scope:' + kind) for field in table_fields
                        if (kind := _document_scope_field(field, config)) in scope.get('values', {})]
        for field, value, kind in assignments:
            contexts[index].setdefault(canon(field), []).append({
                'field': field, 'value': value, 'origin': seed['seed_ref'],
                'file': location.get('file', location.get('source_file')), 'sheet': location.get('sheet'),
                'identifier_namespace': kind, 'document_bridge_id': seed.get('bridge_id'),
                'upstream_key_provenance': anchor_keys})
        seen.add(signature)
        result_rows[index].setdefault('document_lookup_seeds', []).append(seed)
        added += bool(assignments)
    return added


def trace_seed_sources(seeds, source_files, *, cache_dir='.dd_cache', progress=None, config=None):
    """Trace independently gated document lookup seeds without inventing a file.

    The in-memory query rows are explicitly labelled lookup_seed_index, and
    each keeps the real document location and bridge provenance. They are not
    represented as rows from a nonexistent spreadsheet.
    """
    from lineage_config import validate_config
    config = validate_config(config)
    valid = [dict(seed) for seed in seeds if seed.get('can_seed_lookup') is True]
    if not valid:
        raise ValueError('No affirmative, scoped document lookup seeds were provided')
    if len(valid) > config.get('evidence', {}).get('max_lookup_seeds', 2048):
        raise ValueError('Document lookup seed budget exceeded')
    canon = _aliases(config)
    rows, groups = [], {}
    for seed in valid:
        anchor = seed.get('anchor') or {'identifier_namespace': seed.get('identifier_namespace', seed.get('namespace')),
                                      'value': seed['value'], 'scope': seed.get('scope', {})}
        group = (seed.get('result_row', seed.get('target_index', 1)), anchor.get('identifier_namespace'),
                 str(anchor.get('value')), json.dumps(anchor.get('scope', {}), sort_keys=True))
        if group not in groups:
            number = len(rows) + 1
            groups[group] = number
            namespace = anchor.get('identifier_namespace', 'unspecified')
            rows.append({'row_num': number, 'row_num_kind': 'lookup_target_index',
                         'values': {namespace + '_id': anchor.get('value')},
                         'seed_source_location': seed['source_location'], 'lookup_anchor': anchor})
        seed['result_row'] = groups[group]
    first_location = valid[0]['source_location']
    location_file = first_location.get('file', first_location.get('source_file'))
    if not location_file and first_location.get('kind') != 'user_query':
        raise ValueError('Document seeds require an original source file location')
    file = Path(location_file).expanduser().resolve() if location_file else None
    packet = {'kind': 'document_lookup_seeds', 'sheet': None, 'source_sha256': None,
              'result_ref': {'kind': 'document_lookup_seeds', 'source_locations': [s['source_location'] for s in valid]},
              'coverage': {'scan_complete': True, 'filter_evaluation_complete': True, 'truncated': False,
                           'requested_seeds': len(valid), 'record_grain': 'explicit_document_lookup_seed'}}
    return _trace_context(file, source_files, rows, packet, list(range(1, len(rows) + 1)), [{} for _ in rows],
                          config=config, cache_dir=cache_dir, progress=progress, started=time.perf_counter(),
                          canon=canon, initial_seeds=valid)


def trace_sources(result_file, source_files, *, row=2, rows=None, result_sheet=None,
                  cache_dir='.dd_cache', progress=None, config=None):
    """Locate all requested result rows, preserving ambiguity and source errors.

    `rows='all'` selects the entire result within the explicit return budget.
    Config `sources[path_or_basename]` may declare header_row/sheet, keys or
    key_map {source_field: known_field}, field_map, columns and fixed where.
    """
    started = time.perf_counter()
    from lineage_config import validate_config
    config = validate_config(config)
    if not isinstance(config, dict):
        raise ValueError('config must be an object')
    canon = _aliases(config)
    result_file = Path(result_file).expanduser().resolve()
    if not result_file.is_file():
        raise ValueError('Result file does not exist: ' + str(result_file))
    if result_file.suffix.lower() not in TABULAR:
        raise ValueError('Result must be a structured XLSX/CSV/TSV/JSONL/NDJSON file')
    if result_file.suffix.lower() == '.sql':
        raise ValueError('SQL statements may contain several tuples per physical row; use a declared record export for the result table')
    first_auto = rows is None and row is None
    requested = None if rows == 'all' or first_auto else list(rows) if rows is not None else [row]
    if requested is not None and (not requested or any(type(v) is not int or v < 1 for v in requested)):
        raise ValueError('rows must be nonempty physical row numbers or all')
    if requested is not None and len(set(requested)) != len(requested):
        raise ValueError('Requested physical rows must not repeat')
    options = dict(config.get('result', {}))
    if result_sheet is not None:
        options['sheet'] = result_sheet
    def event(phase, **data):
        if progress:
            progress({'phase': phase, **data})
    def query_progress(file):
        return lambda update: event('source_scan', file=str(file),
                                    **(update if isinstance(update, dict) else {'scanned_rows': update}))
    event('profiling_result', file=str(result_file))
    if result_file.suffix.lower() in MATERIALIZED:
        options['_materialized_packets'], profile = _bounded_tables(result_file)
        if options.get('sheet') is not None:
            profile['sheets'] = [s for s in profile['sheets'] if s['sheet'] == options['sheet']]
    else:
        profile = profile_source(result_file, cache_dir=Path(cache_dir) / 'profiles',
                                 sheet=options.get('sheet'), header_row=options.get('header_row'),
                                 encoding=options.get('encoding', 'utf-8-sig'), delimiter=options.get('delimiter'))
    tables = [s for s in profile.get('sheets', []) if s['header_status'] == 'selected']
    if len(tables) != 1:
        raise ValueError('Result sheet/header is ambiguous; set result.sheet/header_row explicitly')
    table = {**tables[0], 'single_sheet': len(profile.get('sheets', [])) == 1}
    maximum = config.get('max_result_rows', 10000)
    if type(maximum) is not int or maximum < 1:
        raise ValueError('max_result_rows must be a positive integer')
    limit = 1 if first_auto else maximum if requested is None else max(requested) - (table.get('header_row') or 0)
    if limit < 1 or limit > maximum:
        raise ValueError('Requested result rows exceed the result budget or refer to the header')
    result_packet = _read_table(result_file, table, options, cache_dir, query_progress(result_file), limit=limit)
    by_row = {r['row_num']: r for r in result_packet['rows']}
    if requested is None:
        requested = list(by_row)
        if result_packet['coverage'].get('truncated') and not first_auto:
            raise ValueError('Entire result exceeds max_result_rows; increase explicit budget or request rows')
    absent = [r for r in requested if r not in by_row]
    if absent:
        raise ValueError('Requested result physical rows do not exist: ' + ', '.join(map(str, absent)))
    result_rows = [dict(by_row[r]) for r in requested]
    contexts = [_known_context(r['values'], canon, result_file, result_packet.get('sheet')) for r in result_rows]
    return _trace_context(result_file, source_files, result_rows, result_packet, requested, contexts,
                          config=config, cache_dir=cache_dir, progress=progress, started=started, canon=canon)


def _trace_context(result_file, source_files, result_rows, result_packet, requested, contexts,
                   *, config, cache_dir, progress, started, canon, initial_seeds=()):
    """Shared structural execution for result records and located document seeds."""
    def event(phase, **data):
        if progress:
            progress({'phase': phase, **data})
    def query_progress(file):
        return lambda update: event('source_scan', file=str(file),
                                    **(update if isinstance(update, dict) else {'scanned_rows': update}))
    matches = [[] for _ in result_rows]
    profiles, configurations, pending, text_sources = {}, {}, [], []
    issues, queries, unmatched_sources = [], [], []
    files = list(dict.fromkeys(str(Path(f).expanduser().resolve()) for f in source_files))
    for filename in files:
        file = Path(filename)
        source_options = dict(_file_config(config, file))
        configurations[filename] = source_options
        if not file.is_file():
            issues.append({'code': 'source_missing', 'file': filename, 'severity': 'error'})
            continue
        if file == result_file and result_packet.get('kind') != 'document_lookup_seeds':
            issues.append({'code': 'result_is_not_its_own_source', 'file': filename, 'severity': 'warning'})
            continue
        try:
            event('profiling_source', file=filename)
            if file.suffix.lower() in MATERIALIZED:
                source_options['_materialized_packets'], p = _bounded_tables(file)
                requested_sheets = set(source_options.get('sheets', {}))
                if source_options.get('sheet') is not None:
                    requested_sheets = {source_options['sheet']}
                if requested_sheets:
                    missing = requested_sheets - {s['sheet'] for s in p['sheets']}
                    if missing:
                        raise ValueError('Configured table/sheet does not exist: ' + ', '.join(map(str, missing)))
                    p['sheets'] = [s for s in p['sheets'] if s['sheet'] in requested_sheets]
            elif source_options.get('sheets'):
                if not isinstance(source_options['sheets'], dict):
                    raise ValueError('source.sheets must be a configuration object')
                parts = []
                for sheet in source_options['sheets']:
                    scoped = _table_options(source_options, sheet)
                    if scoped.get('skip'):
                        continue
                    parts.append(profile_source(file, cache_dir=Path(cache_dir) / 'profiles',
                                                sheet=sheet, header_row=scoped.get('header_row'),
                                                encoding=scoped.get('encoding', 'utf-8-sig'), delimiter=scoped.get('delimiter')))
                if not parts:
                    continue
                p = {**parts[0], 'sheets': [sheet for part in parts for sheet in part['sheets']]}
            else:
                p = profile_source(file, cache_dir=Path(cache_dir) / 'profiles',
                                   sheet=source_options.get('sheet'), header_row=source_options.get('header_row'),
                                   encoding=source_options.get('encoding', 'utf-8-sig'), delimiter=source_options.get('delimiter'))
            profiles[filename] = p
            issues.extend({'file': filename, **issue} for issue in p.get('issues', []))
            if file.suffix.lower() not in TABULAR:
                text_sources.append(filename)
                continue
            if not p.get('sheets'):
                issues.append({'code': 'no_structured_records', 'file': filename, 'severity': 'warning',
                               'message': 'No structured record grain was extracted; provide literal records or a declared export.'})
                text_sources.append(filename)
                continue
            for t in p.get('sheets', []):
                scoped_options = _table_options(source_options, t.get('sheet'))
                from document_entities import identifier_namespace
                scoped_options['_identifier_ranks'] = {f['name']: 45 if kind == 'product' else 80
                    for f in t.get('fields', [])
                    if (kind := identifier_namespace(f['name'], config=config)) and kind != 'unspecified'
                    and not kind.startswith('field:')}
                scoped_options['_scope_fields'] = [f['name'] for f in t.get('fields', [])
                                                    if _document_scope_field(f['name'], config)]
                if scoped_options.get('skip'):
                    continue
                if file.suffix.lower() == '.xls' and scoped_options.get('header_row', 1) != 1:
                    raise ValueError('Legacy XLS reader requires row 1 headers; convert a titled workbook with an explicit mapping first')
                if t.get('header_status') != 'selected':
                    issues.append({'code': 'source_header_ambiguous', 'file': filename,
                                   'sheet': t.get('sheet'), 'severity': 'warning'})
                    continue
                t = {**t, 'single_sheet': len(p.get('sheets', [])) == 1}
                # Rule-looking column names (e.g. rule_id on an order) do not
                # override available structural joins. Unkeyed rule tables
                # naturally reach bounded text retrieval after the join rounds.
                pending.append((file, t, scoped_options))
        except (ValueError, OSError, RuntimeError) as exc:
            issues.append({'code': 'source_profile_failed', 'file': filename,
                           'error_type': type(exc).__name__, 'message': str(exc), 'severity': 'error'})
    rounds = 0
    seen_seeds = set()
    _inject_document_seeds(initial_seeds, result_rows, contexts, profiles, canon, config, seen_seeds)
    max_passes = config.get('max_source_key_passes', 3)
    if type(max_passes) is not int or not 1 <= max_passes <= 8:
        raise ValueError('max_source_key_passes must be an integer from 1 to 8')
    def run_pending():
        nonlocal rounds
        while pending:
            _refine_ambiguous_candidates(result_rows, matches, contexts, canon)
            plans = []
            for position, (file, t, source_options) in enumerate(pending):
                try:
                    plan = _key_plan(t, source_options, contexts, canon)
                except (ValueError, TypeError) as exc:
                    issues.append({'code': 'invalid_key_configuration', 'file': str(file), 'message': str(exc), 'severity': 'error'})
                    plan = None
                if plan:
                    plans.append((plan['score'], len(plan['by_result']), -position, plan))
            if not plans:
                break
            _, _, negpos, plan = max(plans, key=lambda item: item[:3])
            file, t, source_options = pending.pop(-negpos)
            rounds += 1
            key_fields = list(plan['mapping'])
            # Single-key batches use the long-standing where cache signature.
            where = {key_fields[0]: [k[key_fields[0]] for k in plan['key_rows']]} if len(key_fields) == 1 else None
            key_rows = plan['key_rows'] if len(key_fields) > 1 else None
            try:
                event('querying_source', file=str(file), sheet=t.get('sheet'), round=rounds,
                      key_fields=key_fields, requested_key_count=len(plan['key_rows']))
                projected = source_options.get('columns', source_options.get('query', {}).get('columns'))
                if projected is not None and set(key_fields) - set(projected):
                    raise ValueError('Projected columns must include every matching key field')
                packet = _read_table(file, t, source_options, cache_dir, query_progress(file), where=where, key_rows=key_rows)
                coverage = packet['coverage']
                issues.extend({'file': str(file), **issue} for issue in packet.get('issues', []))
                queries.append({'file': str(file), 'sheet': packet.get('sheet'), 'header_row': t.get('header_row'),
                                'match_keys': key_fields, 'key_count': len(plan['key_rows']), 'coverage': coverage,
                                'key_pass': len(source_options.get('_attempted_keysets', [])) + 1,
                                'target_result_rows': [result_rows[i]['row_num'] for i in plan['targets']],
                                'key_provenance': [{'result_row': result_rows[i]['row_num'], 'keys': keys}
                                                   for i, keys in plan['provenance'].items()],
                                'cache': packet.get('cache'), 'elapsed_seconds': packet.get('elapsed_seconds'),
                                'source_sha256': packet['source_sha256']})
                event('source_query_complete', file=str(file), cache_hit=packet.get('cache', {}).get('hit'),
                      scanned_rows=coverage.get('scanned_rows'), matched_rows=coverage.get('matched_rows'))
                complete = coverage.get('scan_complete') and not coverage.get('truncated') and coverage.get('filter_evaluation_complete', True) and coverage.get('projection_complete', True)
                if not complete:
                    issues.append({'code': 'source_query_incomplete', 'file': str(file), 'severity': 'warning'})
                retry_indices = set(plan['targets']) - plan['by_result'].keys()
                for index in retry_indices:
                    unmatched_sources.append({'result_row': result_rows[index]['row_num'], 'file': str(file),
                                              'sheet': t.get('sheet'), 'reason': 'requested_row_missing_source_key',
                                              'match_keys': key_fields})
                lookup = defaultdict(list)
                for candidate in packet['rows']:
                    if all(f in candidate['values'] for f in key_fields):
                        lookup[tuple(_token(candidate['values'][f]) for f in key_fields)].append(candidate)
                for index, expected in plan['by_result'].items():
                    candidates = lookup.get(tuple(_token(expected[f]) for f in key_fields), [])
                    retained, rejected = _resolve_candidates(result_rows[index]['values'], candidates, key_fields, canon,
                                                             source_options.get('field_map'))
                    if not retained:
                        retry_indices.add(index)
                        unmatched_sources.append({'result_row': result_rows[index]['row_num'], 'file': str(file),
                                                  'sheet': t.get('sheet'), 'reason': 'identifier_conflict' if rejected else 'key_not_found',
                                                  'match_keys': key_fields, 'rejected_candidates': rejected})
                    for item in retained:
                        unmatched_sources[:] = [missing for missing in unmatched_sources
                                                if not (missing.get('file') == str(file) and missing.get('sheet') == t.get('sheet')
                                                        and missing.get('result_row') == result_rows[index]['row_num']
                                                        and missing.get('reason') in ('requested_row_missing_source_key', 'key_not_found', 'identifier_conflict'))]
                        candidate = item['row']
                        location = _location(file, packet.get('sheet'), candidate, packet['source_sha256'])
                        ref = 'src_' + hashlib.sha256(json.dumps(location, sort_keys=True).encode()).hexdigest()[:20]
                        supported, basis = _identity_support(result_rows[index]['values'], item, plan, canon)
                        status = 'located' if len(retained) == 1 and complete and supported else 'ambiguous'
                        match = {**location, 'location': location, 'source_ref': ref, 'status': status,
                                 'match_keys': key_fields, 'key_mapping': plan['mapping'],
                                 'key_provenance': plan['provenance'][index], 'localization_reason': basis,
                                 'field_comparisons': item['comparisons'], 'candidate_count': len(candidates),
                                 'retained_candidate_count': len(retained), 'rejected_candidates': rejected,
                                 'duplicate_key_warning': len(candidates) > 1,
                                 'query_complete': bool(complete),
                                 'values': candidate['values'], 'cells': candidate.get('cells', {}),
                                 'field_pointers': candidate.get('field_pointers', {})}
                        matches[index].append(match)
                        if status == 'located':
                            for field, value in candidate['values'].items():
                                contexts[index].setdefault(canon(field), []).append({'field': field, 'value': value, 'origin': ref,
                                                                                    'file': str(file), 'sheet': packet.get('sheet')})
                attempted = [*source_options.get('_attempted_keysets', []), plan['signature']]
                if retry_indices and complete and len(attempted) < max_passes and not plan['explicit']:
                    retry_options = {**source_options, '_attempted_keysets': attempted, '_target_indices': sorted(retry_indices)}
                    attempted_fields = {field for signature in attempted for field, _ in signature}
                    may_gain_key = any(_id_rank(f['name']) and f['name'] not in attempted_fields
                                       and f['name'] not in source_options.get('_scope_fields', []) for f in t.get('fields', []))
                    if _key_plan(t, retry_options, contexts, canon) is not None or may_gain_key:
                        pending.append((file, t, retry_options))
                elif retry_indices and complete and len(attempted) >= max_passes and not plan['explicit']:
                    retry_options = {**source_options, '_attempted_keysets': attempted, '_target_indices': sorted(retry_indices)}
                    if _key_plan(t, retry_options, contexts, canon) is not None:
                        issues.append({'code': 'key_pass_budget_exhausted', 'file': str(file), 'sheet': t.get('sheet'),
                                       'severity': 'warning', 'max_source_key_passes': max_passes,
                                       'result_rows': [result_rows[i]['row_num'] for i in sorted(retry_indices)]})
            except (ValueError, OSError, RuntimeError) as exc:
                issues.append({'code': 'source_query_failed', 'file': str(file), 'sheet': t.get('sheet'),
                               'error_type': type(exc).__name__, 'message': str(exc), 'severity': 'error'})
    evidence_by_id = {}
    retrieval = {'evidence': [], 'coverage': {}, 'issues': []}
    retrieval_rounds = []
    note_records_seen = set()
    max_document_hops = config.get('evidence', {}).get('max_document_hops', 4)
    for document_round in range(max_document_hops + 1):
        run_pending()
        _refine_ambiguous_candidates(result_rows, matches, contexts, canon)
        for file, t, pending_options in pending:
            if str(file) not in text_sources and _full_text_budget(file, config.get('unkeyed_text_max_bytes', 16 * 1024 * 1024)):
                text_sources.append(str(file))
        try:
            from evidence_retrieval import retrieve_evidence, build_lookup_seeds, evidence_from_located_records, _mark_located_records
            current_rows = [{'row_num': result['row_num'], 'source_matches': matches[i]} for i, result in enumerate(result_rows)]
            note_matches = [match for items in matches for match in items if match['status'] == 'located'
                            and match['source_ref'] not in note_records_seen and any(
                                value and re.search(r'备注|说明|内容|描述|comment|description|note|text|content|body|remark', field, re.I)
                                for field, value in match['values'].items())]
            for item in evidence_from_located_records(note_matches, config=config):
                note_records_seen.add(item['source_ref'])
                mentions = {m['mention_id']: m for m in item.get('entity_mentions', [])}
                useful = []
                for bridge in item.get('bridge_candidates', []):
                    endpoints = [mentions.get(mid, {}) for mid in (bridge['from_mention'], bridge['to_mention'])]
                    in_note = any((endpoint.get('selector') or {}).get('offset_scope') == 'decoded_field_value'
                                  for endpoint in endpoints)
                    # A field value and a note can repeat the same typed anchor;
                    # the explicit relation in that note is still usable after
                    # the record itself was independently located. Preserve the
                    # original ambiguity reason in the evidence item.
                    if (bridge.get('can_seed_lookup') or (bridge.get('relation_text') and in_note)):
                        useful.append({**bridge, 'can_seed_lookup': True,
                                       'derived_from_record_note': not bridge.get('can_seed_lookup')})
                if not useful:
                    continue
                item['bridge_candidates'] = useful
                item['matched_terms'] = [{'text': str(match['values'][key]), 'identifier': True,
                    'origins': [{'kind': 'value', 'field': key, 'result_row': result_rows[index]['row_num']}]}
                    for index, items in enumerate(matches) for match in items if match['source_ref'] == item['source_ref']
                    for key in match['match_keys'] if key in match['values']]
                evidence_by_id[item['evidence_id']] = item
            if not text_sources and not evidence_by_id:
                break
            event('retrieving_rule_and_text_evidence', source_count=len(text_sources), document_round=document_round + 1)
            anchors = []
            for index, result in enumerate(result_rows):
                values = dict(result['values'])
                for entries in contexts[index].values():
                    usable = [entry for entry in entries if _usable(entry['value'], True)]
                    if len({_token(entry['value']) for entry in usable}) == 1:
                        values.setdefault(usable[0]['field'], usable[0]['value'])
                anchors.append({'row_num': result['row_num'], 'values': values})
            target = {'description': result_file.stem if result_file else 'Document lookup seeds', 'rows': anchors}
            if text_sources:
                retrieval = retrieve_evidence(target, text_sources, profiles=profiles, cache_dir=cache_dir,
                                           limit=config.get('max_text_evidence', 30), progress=progress,
                                           max_source_bytes=config.get('text_max_source_bytes', 64 * 1024 * 1024), config=config)
                retrieval_rounds.append(retrieval.get('coverage', {}))
                issues.extend(retrieval.get('issues', []))
            for item in retrieval.get('evidence', []):
                previous = evidence_by_id.get(item['evidence_id'])
                if previous:
                    terms = {json.dumps(term, sort_keys=True): term for term in previous.get('matched_terms', [])}
                    terms.update({json.dumps(term, sort_keys=True): term for term in item.get('matched_terms', [])})
                    item = {**item, 'matched_terms': list(terms.values())}
                evidence_by_id[item['evidence_id']] = item
            _mark_located_records(list(evidence_by_id.values()), {'result_rows': current_rows})
            seeds = build_lookup_seeds(target, list(evidence_by_id.values()), config=config)
            added = _inject_document_seeds(seeds, result_rows, contexts, profiles, canon, config, seen_seeds)
            if not added or not pending:
                break
            if document_round == max_document_hops:
                issues.append({'code': 'document_bridge_round_budget_exhausted', 'severity': 'warning',
                               'max_document_hops': max_document_hops})
        except (ImportError, ValueError, OSError, RuntimeError) as exc:
            issues.append({'code': 'evidence_retrieval_failed', 'message': str(exc), 'severity': 'warning'})
            break
    for file, t, pending_options in pending:
        issues.extend({'file': str(file), 'sheet': t.get('sheet'), **issue}
                      for issue in _blocked_key_conflicts(t, pending_options, contexts, canon, result_rows))
        if not _rules_table(t):
            unmatched_sources.append({'file': str(file), 'sheet': t.get('sheet'), 'reason': 'no_shared_stable_key',
                                      'next_action': 'Declare sources[file].key_map or provide a linking table; names, sex and 0/1 values are not stable keys.'})
        if str(file) not in text_sources:
            issues.append({'code': 'unkeyed_source_exceeds_text_budget', 'file': str(file), 'severity': 'warning',
                           'message': 'Large structured source requires an explicit key map; full-table fuzzy retrieval was not attempted.'})
    evidence = list(evidence_by_id.values())
    if retrieval_rounds:
        retrieval['coverage'] = {**retrieval.get('coverage', {}), 'rounds': retrieval_rounds,
                                 'retrieval_complete': all(r.get('retrieval_complete') for r in retrieval_rounds)}
    issues.extend(_invalidate_conflicting_dependencies(result_rows, matches, contexts, canon, queries))
    issues.extend(_invalidate_document_dependencies(result_rows, matches, evidence))
    mappings = _field_mappings(result_rows, matches, canon, configurations)
    counts = Counter()
    for index, result in enumerate(result_rows):
        result['source_matches'] = matches[index]
        result['location'] = result.get('seed_source_location') or _location(result_file, result_packet.get('sheet'), result, result_packet['source_sha256'])
        result['field_mappings'] = [m for m in mappings if m['result_row'] == result['row_num']]
        documents = []
        for item in evidence:
            identifier_matches = [term for term in item.get('matched_terms', []) if term.get('identifier')
                                  and any(origin.get('kind') == 'value' and origin.get('result_row') == result['row_num']
                                          for origin in term.get('origins', []))]
            if identifier_matches:
                documents.append({'evidence_id': item['evidence_id'], 'source_location': item.get('source_location'),
                                  'status': 'identifier_mention_only', 'matched_identifiers': identifier_matches,
                                  'record_join_proven': False})
        result['document_evidence'] = documents
        result['document_localization'] = {'status': 'passages_found' if documents else 'not_found'}
        if any(m['status'] == 'ambiguous' for m in matches[index]) or any(
                i.get('code') == 'conflicting_propagated_identifier' and i.get('result_row') == result['row_num'] for i in issues):
            state = 'ambiguous'
        elif any(m['status'] == 'located' for m in matches[index]):
            state = 'located'
        elif documents:
            state = 'evidence_only'
        else:
            state = 'not_found'
        result['source_localization'] = {'status': state}
        counts[state] += 1
    if counts['ambiguous']:
        localized = 'ambiguous'
    elif counts['located'] == len(result_rows) and result_rows:
        localized = 'partial' if issues or unmatched_sources else 'located'
    elif counts['located']:
        localized = 'partial'
    elif counts['evidence_only'] == len(result_rows) and result_rows:
        localized = 'evidence_only'
    elif counts['evidence_only']:
        localized = 'partial'
    else:
        localized = 'not_found'
    rule_evidence = [e for e in evidence if e.get('kind') == 'rule_candidate' or e.get('rule_applicability') == 'not_verified']
    next_actions = []
    if localized != 'located':
        next_actions.append('Resolve ambiguous/missing source keys using field roles, explicit key_map or the missing source file.')
    unresolved_fields = sum(m['status'] in ('unresolved', 'conflicting', 'representation_discrepancy', 'candidate_ambiguous') for m in mappings)
    if unresolved_fields:
        next_actions.append('Review field-level gaps and transformations; provide dictionaries or calculation rules where needed.')
    report = {'schema_version': 'source-trace-1', 'analysis_stage': 'source_trace',
            'status': 'source_chain_reconstructed' if localized == 'located' else
                      'source_passages_located' if localized == 'evidence_only' else 'source_trace_incomplete',
            'confidence': None, 'result_file': str(result_file) if result_file else None, 'result_sheet': result_packet.get('sheet'),
            '_source_files': files, '_config': config,
            'result_ref': {'file': str(result_file) if result_file else None, 'sheet': result_packet.get('sheet'),
                           'sha256': result_packet['source_sha256'], 'source_sha256': result_packet['source_sha256'],
                           **result_packet.get('result_ref', {})},
            'result_rows': result_rows, 'source_profiles': profiles, 'field_mappings': mappings,
            'rule_evidence': rule_evidence, 'related_evidence': evidence,
            'source_localization': {'status': localized, **dict(counts),
                'requested_rows': len(requested), 'unmatched_sources': unmatched_sources},
            'document_localization': {'status': 'passages_found' if any(r['document_evidence'] for r in result_rows) else 'not_found',
                                      'rows_with_passages': sum(bool(r['document_evidence']) for r in result_rows)},
            'calculation_verification': {'status': 'not_checked', 'reason': 'Source matching does not execute a calculation plan.'},
            'rule_applicability': {'status': 'not_verified', 'reason': 'Located text is not proof of rule semantics or applicability dates.'},
            'historical_lineage': {'status': 'not_proven', 'reason': 'No claim that the reconstructed relationships were historically executed.'},
            'coverage': {'requested_rows': len(requested), 'processed_rows': len(result_rows),
                         'source_files_requested': len(files),
                         'source_files_unavailable': len({i['file'] for i in issues
                                                          if i.get('severity') == 'error' and i.get('file') in files}),
                         'requested_physical_rows': requested, 'source_queries': queries,
                         'result_query': result_packet['coverage'], 'retrieval': retrieval.get('coverage'),
                         'field_count': len(mappings), 'field_status_counts': dict(Counter(m['status'] for m in mappings)),
                         'source_scan_complete': all(q['coverage'].get('scan_complete') for q in queries),
                         'source_truncated': any(q['coverage'].get('truncated') for q in queries),
                         'source_errors': len([i for i in issues if i.get('severity') == 'error']),
                         'ingestion_complete': not issues and not unmatched_sources and
                             all(q['coverage'].get('scan_complete') and not q['coverage'].get('truncated')
                                 and q['coverage'].get('filter_evaluation_complete', True)
                                 and q['coverage'].get('projection_complete', True) for q in queries) and
                             (not text_sources or retrieval.get('coverage', {}).get('retrieval_complete') is True),
                         'elapsed_seconds': round(time.perf_counter() - started, 6)},
            'issues': issues, 'next_actions': next_actions,
            'evidence': evidence, 'chain': {'nodes': [], 'edges': [], 'confidence': None},
            'conclusions': [{'statement': 'Source locations reconstructed' if localized == 'located' else 'Source localization needs follow-up',
                             'status': localized, 'confidence': None}]}
    from trace_wall import attach_trace_graph
    return attach_trace_graph(report)
