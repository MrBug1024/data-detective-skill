"""Bounded, content-keyed source/schema discovery; samples are not full coverage.

profile_source(file, *, cache_dir=None, header_rows=12, sheet=None,
               header_row=None, encoding='utf-8-sig', delimiter=None)
returns a `sheets` list for XLSX/CSV/JSONL. Encoding/delimiter apply to CSV/TSV.
Only header_status=selected is an executable header choice. Candidate scores
rank layout hypotheses; they are not confidence probabilities or key proof.
"""
from __future__ import annotations

from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata

from run_contract import atomic_json
from xlsx_query import inspect_xlsx, _source_digest

VERSION = 'source-profile-2'
LABEL_TERMS = ('identifier', 'amount', 'quantity', 'description', 'status', 'number',
               'date', 'time', 'name', 'code', 'total', 'price', 'type', '编号', '编码',
               '代码', '名称', '时间', '日期', '数量', '金额', '比例', '单价', '地址',
               '说明', '内容', '单位', '姓名', '类别', '结果', '规则', '流水号', '标志')
KEY_PATTERN = re.compile(r'(^|[^a-z])(id|key|uuid|guid|no)([^a-z]|$)|(?:id|编号|编码|代码|流水号|记录号)$', re.I)
VALUE_PATTERN = re.compile(r'^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$|^\d{4}[-/]\d{1,2}[-/]\d{1,2}')


def _norm(value):
    return unicodedata.normalize('NFKC', str(value)).strip().casefold()


def _roles(name):
    hints = []
    if KEY_PATTERN.search(name): hints.append('identifier_candidate')
    if any(term in _norm(name) for term in ('date', 'time', '日期', '时间')): hints.append('date_candidate')
    if any(term in _norm(name) for term in ('amount', 'total', 'price', '金额', '费用', '单价')): hints.append('measure_candidate')
    if any(term in _norm(name) for term in ('name', '名称', '姓名')): hints.append('label_candidate')
    return hints


def _kind(value, cell=None):
    if value is None or value == '': return 'empty'
    if isinstance(value, bool): return 'boolean'
    if cell and cell.get('iso_value'): return 'date'
    if cell and cell.get('data_type') == 'n': return 'number'
    return 'text'


def _candidates(rows):
    filled = [(row, {col: value for col, value in row['values'].items()
                     if value is not None and str(value).strip()}) for row in rows]
    filled = [(row, values) for row, values in filled if values]
    candidates = []
    for index, (row, values) in enumerate(filled):
        labels = [_norm(value) for value in values.values()]
        following = filled[index + 1:index + 5]
        width = len(values)
        # Sparse data rows can legitimately omit most nullable columns. Match
        # their occupied positions to the candidate schema, not 70% occupancy
        # of a wide header (which would reject real sparse exports).
        same_layout = [other for _, other in following
                       if len(other) >= min(2, width)
                       and len(set(values) & set(other)) >= max(1, len(other) * .8)]
        unique = len(set(labels)) == width
        text_count = sum(isinstance(value, str) and not VALUE_PATTERN.match(value.strip())
                         for value in values.values())
        label_count = sum(any(term in label for term in LABEL_TERMS) or bool(KEY_PATTERN.search(label))
                          for label in labels)
        short_count = sum(len(label) <= 48 for label in labels)
        prior_full = any(len(previous) >= width for _, previous in filled[:index])
        score = (2 * text_count / width + 1.5 * short_count / width + (1 if unique else -4)
                 + min(2, len(same_layout)) + 2 * label_count / width
                 + (1.5 if not prior_full and same_layout else 0)
                 - (2 if width == 1 and any(len(other) > 1 for _, other in following) else 0))
        reasons = {'nonempty_fields': width, 'unique_labels': unique, 'text_fraction': round(text_count / width, 3),
                   'short_label_fraction': round(short_count / width, 3), 'label_term_fraction': round(label_count / width, 3),
                   'following_layout_rows': len(same_layout), 'first_full_width_row': not prior_full,
                   'sample_only': True}
        eligible = unique and text_count == width and bool(same_layout)
        candidates.append({'row_num': row['row_num'], 'score': round(score, 3),
                           'eligible': eligible, 'reasons': reasons,
                           'cells': {col: row.get('cells', {}).get(col, {}).get('address') for col in values}})
    return sorted(candidates, key=lambda item: (-item['score'], item['row_num']))


def _sheet_profile(sample, explicit_header=None):
    rows = sample['rows']
    candidates = _candidates(rows)
    eligible = [item for item in candidates if item['eligible']]
    selected, status, selection = None, 'unavailable', 'insufficient_sample'
    if explicit_header is not None:
        selected = next((row for row in rows if row['row_num'] == explicit_header), None)
        if selected is None:
            raise ValueError('Explicit header_row is absent from the sampled physical rows')
        status, selection = 'selected', 'explicit'
    elif eligible:
        best = eligible[0]
        margin = best['score'] - eligible[1]['score'] if len(eligible) > 1 else None
        if best['score'] >= 6 and (margin is None or margin >= 1):
            selected = next(row for row in rows if row['row_num'] == best['row_num'])
            status, selection = 'selected', 'heuristic_sample_only'
        else:
            status, selection = 'ambiguous', 'explicit_header_required'
    result = {'sheet': sample.get('sheet'), 'dimension': sample.get('dimension'),
              'header_row': selected['row_num'] if selected else None,
              'header_status': status, 'header_selection': selection,
              'header_candidates': candidates, 'fields': [], 'sample_rows': [],
              'sample_prefix': rows, 'key_uniqueness_scope': 'sample_only_not_full_source'}
    if selected is None:
        return result
    mapping = {column: str(value).strip() for column, value in selected['values'].items()
               if value is not None and str(value).strip()}
    if not mapping or len({_norm(label) for label in mapping.values()}) != len(mapping):
        raise ValueError('Selected header has empty or duplicate normalized labels')
    records = [row for row in rows if row['row_num'] > selected['row_num'] and any(
        value is not None and str(value).strip() for value in row['values'].values())]
    for column, name in mapping.items():
        observed = [row['values'].get(column) for row in records]
        nonempty = [value for value in observed if value is not None and str(value).strip()]
        result['fields'].append({'name': name, 'column': column,
            'cell': selected.get('cells', {}).get(column, {}).get('address'),
            'type_samples': sorted({_kind(row['values'].get(column), row.get('cells', {}).get(column)) for row in records}),
            'role_hints': _roles(name), 'sample_nonempty': len(nonempty),
            'sample_distinct': len({_norm(value) for value in nonempty}),
            'sample_rows': len(records)})
    for row in records:
        result['sample_rows'].append({'row_num': row['row_num'],
            'values': {name: row['values'].get(column) for column, name in mapping.items()},
            'cells': {name: row.get('cells', {}).get(column) for column, name in mapping.items()}})
    return result


def _text_profile(file, header_rows, header_row, encoding='utf-8-sig', delimiter=None):
    from record_query import iter_csv
    from json_records import iter_jsonl
    suffix = file.suffix.casefold()
    is_json = suffix in ('.jsonl', '.ndjson')
    if header_row is not None and (is_json or header_row != 1):
        raise ValueError('CSV streaming currently requires header_row=1; JSONL has no header row')
    iterator = iter_jsonl(file) if is_json else iter_csv(file, encoding=encoding, delimiter=delimiter)
    records = []
    with closing(iterator):
        for record in iterator:
            if record['row_num'] > header_rows:
                break
            records.append({key: value for key, value in record.items() if key != 'raw_record'})
    names = list(dict.fromkeys(name for row in records for name in row['values']))
    fields = []
    for name in names:
        observed = [row['values'].get(name) for row in records]
        present = [value for value in observed if value is not None and str(value).strip()]
        fields.append({'name': name, 'column': None, 'cell': None,
            'type_samples': sorted({row.get('value_types', {}).get(name, _kind(row['values'].get(name))) for row in records}),
            'role_hints': _roles(name), 'sample_nonempty': len(present),
            'sample_distinct': len({_norm(value) for value in present}), 'sample_rows': len(records)})
    return {'sheet': None, 'header_row': None if is_json else 1,
            'header_status': 'selected' if fields else 'unavailable',
            'header_selection': 'object_field_names' if is_json else 'csv_first_record',
            'header_candidates': [], 'fields': fields, 'sample_rows': records,
            'sample_prefix': [], 'key_uniqueness_scope': 'sample_only_not_full_source'}


def profile_source(file, *, cache_dir=None, header_rows=12, sheet=None, header_row=None,
                   encoding='utf-8-sig', delimiter=None):
    started = time.perf_counter()
    file = Path(file).expanduser().resolve()
    if type(header_rows) is not int or not 1 <= header_rows <= 100:
        raise ValueError('header_rows must be an integer between 1 and 100')
    if header_row is not None and (type(header_row) is not int or not 1 <= header_row <= 100):
        raise ValueError('header_row must be a physical row between 1 and 100')
    header_rows = max(header_rows, (header_row + 1) if header_row and header_row < 100 else (header_row or 1))
    digest = _source_digest(file)
    signature = {'version': VERSION, 'file': str(file), 'sha256': digest, 'header_rows': header_rows,
                 'sheet': sheet, 'header_row': header_row}
    # Default parsing semantics are unchanged: retain existing profile caches.
    if encoding != 'utf-8-sig': signature['encoding'] = encoding
    if delimiter is not None: signature['delimiter'] = delimiter
    key = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    cache_path = Path(cache_dir).resolve() / (key + '.json') if cache_dir else None
    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if isinstance(cached, dict) and cached.get('signature') == signature and isinstance(cached.get('profile'), dict):
                result = cached['profile']
                result['cache'] = {'hit': True, 'key': key, 'path': str(cache_path)}
                result['elapsed_seconds'] = round(time.perf_counter() - started, 4)
                return result
        except (ValueError, OSError):
            pass
    suffix = file.suffix.casefold()
    if suffix == '.xlsx':
        inspected = inspect_xlsx(file, header_rows=header_rows, sheet=sheet)
        sheets = [_sheet_profile(sample, header_row) for sample in inspected['sheets']]
        coverage = inspected['coverage']
    elif suffix in ('.csv', '.tsv', '.jsonl', '.ndjson'):
        if sheet is not None:
            raise ValueError('sheet is only supported for XLSX')
        sheets = [_text_profile(file, header_rows, header_row, encoding, delimiter)]
        coverage = {'scope': 'first_physical_rows_only', 'max_physical_row': header_rows}
    else:
        return {'file': str(file), 'format': suffix.lstrip('.'), 'source_sha256': digest,
                'sheets': [], 'coverage': {'scan_complete': False, 'scope': 'format_only'},
                'status': 'not_tabular', 'cache': {'hit': False},
                'elapsed_seconds': round(time.perf_counter() - started, 4)}
    if _source_digest(file) != digest:
        raise ValueError('Source changed during profiling; no mixed snapshot was cached')
    result = {'file': str(file), 'format': suffix.lstrip('.'), 'source_sha256': digest,
              'sheets': sheets, 'coverage': {**coverage, 'scan_complete': False},
              'status': 'profiled', 'cache': {'hit': False, 'key': key, 'path': str(cache_path) if cache_path else None},
              'elapsed_seconds': round(time.perf_counter() - started, 4)}
    if cache_path:
        atomic_json(cache_path, {'signature': signature, 'profile': result})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--header-rows', type=int, default=12)
    parser.add_argument('--header-row', type=int)
    parser.add_argument('--sheet')
    parser.add_argument('--encoding', default='utf-8-sig')
    parser.add_argument('--delimiter')
    parser.add_argument('--cache-dir', type=Path, default=Path('.dd_cache/source_profile'))
    args = parser.parse_args()
    if args.out.resolve() == Path(args.file).resolve():
        parser.error('Output must not overwrite source')
    result = profile_source(args.file, cache_dir=args.cache_dir, header_rows=args.header_rows,
                            header_row=args.header_row, sheet=args.sheet,
                            encoding=args.encoding, delimiter=args.delimiter)
    atomic_json(args.out, result)
    print(json.dumps({'out': str(args.out.resolve()), 'status': result['status'],
                      'sheet_count': len(result['sheets']), 'elapsed_seconds': result['elapsed_seconds']}))


if __name__ == '__main__':
    main()
