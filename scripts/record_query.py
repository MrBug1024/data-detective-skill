#!/usr/bin/env python3
"""Bounded exact filtering of CSV/TSV/JSONL, retaining physical source locations.

One scan evaluates all complete key tuples. A return limit never ends the scan;
invalid trailing records and schema gaps must remain visible to the caller.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from ingestion_support import sha256_file
from json_records import iter_jsonl, MAX_RECORD_BYTES

VERSION = 'record-query-1'
MAX_SCHEMA_FIELDS = 20000


class _CsvLines:
    def __init__(self, handle, max_record_chars):
        self.handle, self.maximum = handle, max_record_chars
        self.line_num = self.offset = 0
        self.reset()

    def reset(self):
        self.parts, self.size = [], 0
        self.start, self.start_offset = self.line_num + 1, self.offset

    def __iter__(self):
        return self

    def __next__(self):
        line = self.handle.readline(self.maximum + 1)
        if not line:
            raise StopIteration
        if '\x00' in line:
            raise ValueError('CSV含NUL，须先确认编码或文件类型')
        self.size += len(line)
        if self.size > self.maximum:
            raise ValueError(f'CSV第{self.start}行开始的记录超过单记录字符预算')
        self.line_num += 1
        self.offset += len(line)
        self.parts.append(line)
        return line


def iter_csv(filepath, *, encoding='utf-8-sig', delimiter=None,
             max_record_chars=MAX_RECORD_BYTES, schema=None):
    with open(filepath, encoding=encoding, newline='') as handle:
        if delimiter is None:
            sample = handle.read(65536)
            handle.seek(0)
            delimiter = '\t' if Path(filepath).suffix.lower() == '.tsv' else ','
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
            except csv.Error:
                pass
        lines = _CsvLines(handle, max_record_chars)
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(max_record_chars)
        try:
            reader = csv.reader(lines, delimiter=delimiter, strict=True)
            names = [name.strip() for name in next(reader, [])]
            if not names or any(not name for name in names) or len({name.casefold() for name in names}) != len(names):
                raise ValueError('CSV表头必须非空且唯一')
            if len(names) > MAX_SCHEMA_FIELDS:
                raise ValueError('CSV表头字段数超过预算')
            if schema is not None:
                schema.update(names)
            while True:
                lines.reset()
                try:
                    values = next(reader)
                except StopIteration:
                    break
                if not values:
                    continue
                if len(values) != len(names):
                    raise ValueError(f'CSV第{lines.start}行字段数与表头不符，不能把缺字段猜成空值')
                yield {'values': dict(zip(names, values)), 'row_num': lines.start,
                       'line_end': lines.line_num, 'char_offset': lines.start_offset,
                       'offset_scope': 'decoded_source_text', 'encoding': encoding,
                       'delimiter': delimiter, 'raw_record': ''.join(lines.parts)}
        finally:
            csv.field_size_limit(previous_limit)


def _exact(value):
    if isinstance(value, (dict, list)):
        raise ValueError('筛选值必须是JSON标量；标识和十进制数请使用字符串')
    if value is None:
        return ('null', '')
    if isinstance(value, bool):
        return ('bool', str(value))
    return ('value', str(value))


def _filters(where, key_rows):
    if where is not None and not isinstance(where, dict):
        raise ValueError('where必须为字段到标量/列表的对象')
    allowed = {}
    for name, values in (where or {}).items():
        if not isinstance(name, str) or not name:
            raise ValueError('筛选字段名必须非空')
        allowed[name] = {_exact(value) for value in (values if isinstance(values, list) else [values])}
    tuple_fields, tuples = [], set()
    if key_rows is not None:
        if not isinstance(key_rows, list) or not key_rows or not isinstance(key_rows[0], dict) or not key_rows[0]:
            raise ValueError('key_rows必须为非空对象列表')
        tuple_fields = sorted(key_rows[0])
        if any(not isinstance(name, str) or not name for name in tuple_fields):
            raise ValueError('复合键字段名必须非空字符串')
        for row in key_rows:
            if not isinstance(row, dict) or sorted(row) != tuple_fields:
                raise ValueError('每个复合键必须有完全相同的字段集合')
            tuples.add(tuple(_exact(row[field]) for field in tuple_fields))
    return allowed, tuple_fields, tuples


def query_records(file, *, where=None, key_rows=None, columns=None, limit=None,
                  encoding='utf-8-sig', delimiter=None, max_return_cells=500000,
                  max_return_bytes=64 * 1024 * 1024, max_record_bytes=MAX_RECORD_BYTES,
                  progress=None):
    started = time.perf_counter()
    file = Path(file).resolve()
    if file.suffix.lower() not in ('.csv', '.tsv', '.jsonl', '.ndjson'):
        raise ValueError('流式查询支持CSV/TSV/JSONL/NDJSON；普通JSON请先确认记录粒度再转换')
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError('limit必须为非负整数')
    for value in (max_return_cells, max_return_bytes, max_record_bytes):
        if type(value) is not int or value <= 0:
            raise ValueError('记录/输出预算必须为正整数')
    if columns is not None and (not isinstance(columns, list) or not columns or
                               any(not isinstance(name, str) or not name for name in columns) or len(set(columns)) != len(columns)):
        raise ValueError('columns必须为不重复的非空字段列表')
    allowed, tuple_fields, tuples = _filters(where, key_rows)
    filter_fields = set(allowed) | set(tuple_fields)
    if progress:
        progress({'stage': 'hashing', 'scanned_rows': 0})
    source_hash = sha256_file(file)
    observed_fields = set()
    iterator = (iter_jsonl(file, max_record_bytes) if file.suffix.lower() in ('.jsonl', '.ndjson')
                else iter_csv(file, encoding=encoding, delimiter=delimiter, max_record_chars=max_record_bytes,
                              schema=observed_fields))
    records, scanned, matched, cell_count, byte_count = [], 0, 0, 0, 0
    missing_filter_rows = missing_projection_rows = 0
    last_progress = time.perf_counter()
    for record in iterator:
        scanned += 1
        values = record['values']
        observed_fields.update(values)
        if len(observed_fields) > MAX_SCHEMA_FIELDS:
            raise ValueError('来源不同字段总数超过预算，须先确认记录结构并缩小范围')
        if filter_fields - values.keys():
            missing_filter_rows += 1
            selected = False
        else:
            selected = (all(_exact(values[name]) in choices for name, choices in allowed.items())
                        and (not tuple_fields or tuple(_exact(values[name]) for name in tuple_fields) in tuples))
        if selected:
            matched += 1
            projected = columns if columns is not None else list(values)
            absent = [name for name in projected if name not in values]
            if absent:
                missing_projection_rows += 1
            if limit is None or len(records) < limit:
                output = {key: value for key, value in record.items() if key not in ('values', 'raw_record', 'field_pointers', 'value_types')}
                output['values'] = {name: values[name] for name in projected if name in values}
                # Missing and explicit null must remain different states.
                if absent:
                    output['missing_fields'] = absent
                for key in ('field_pointers', 'value_types'):
                    if key in record:
                        output[key] = {name: record[key][name] for name in projected if name in record[key]}
                cell_count += len(projected)
                byte_count += len(json.dumps(output, ensure_ascii=False).encode('utf-8'))
                if cell_count > max_return_cells or byte_count > max_return_bytes:
                    raise ValueError('命中证据超过返回预算；请缩小键集、投影字段或显式limit，未返回/保存部分结果')
                records.append(output)
        if progress and time.perf_counter() - last_progress >= 5:
            progress({'stage': 'scanning', 'scanned_rows': scanned, 'matched_rows': matched})
            last_progress = time.perf_counter()
    if progress:
        progress({'stage': 'verifying_snapshot', 'scanned_rows': scanned, 'matched_rows': matched})
    if sha256_file(file) != source_hash:
        raise ValueError('来源在扫描期间发生变化，拒绝保存混合版本证据')
    issues = []
    missing_filter_fields = sorted(filter_fields - observed_fields)
    if missing_filter_rows or missing_filter_fields:
        issues.append({'code': 'missing_filter_fields', 'severity': 'warning',
                       'rows': missing_filter_rows, 'never_observed_fields': missing_filter_fields})
    missing_projection_fields = sorted(set(columns or []) - observed_fields)
    if missing_projection_rows or missing_projection_fields:
        issues.append({'code': 'missing_projection_fields', 'severity': 'warning',
                       'rows': missing_projection_rows, 'never_observed_fields': missing_projection_fields})
    truncated = matched > len(records)
    return {'schema_version': VERSION, 'source_file': str(file), 'source_sha256': source_hash,
            'file_type': file.suffix.lower().lstrip('.'), 'columns': columns,
            'where': where or {}, 'key_rows': key_rows, 'records': records,
            'coverage': {'scan_complete': True, 'filter_evaluation_complete': not (missing_filter_rows or missing_filter_fields),
                         'projection_complete': not (missing_projection_rows or missing_projection_fields),
                         'scanned_rows': scanned, 'matched_rows': matched, 'returned_rows': len(records),
                         'truncated': truncated, 'omitted_rows': matched - len(records)},
            'status': 'partial' if issues or truncated else 'complete', 'issues': issues,
            'elapsed_seconds': round(time.perf_counter() - started, 6)}


def _write_atomic(path, data):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--where-json', type=Path)
    parser.add_argument('--keys-json', type=Path)
    parser.add_argument('--columns', nargs='+')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--encoding', default='utf-8-sig')
    parser.add_argument('--delimiter')
    parser.add_argument('--max-return-cells', type=int, default=500000)
    parser.add_argument('--max-return-bytes', type=int, default=64 * 1024 * 1024)
    parser.add_argument('--max-record-bytes', type=int, default=MAX_RECORD_BYTES,
                        help='单条JSONL原始字节上限；CSV为解码后的字符数上限')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        inputs = [path.resolve() for path in (args.file, args.where_json, args.keys_json) if path]
        if args.out.resolve() in inputs:
            raise ValueError('输出不能覆盖来源或筛选输入文件')
        load = lambda path: json.loads(path.read_text(encoding='utf-8-sig')) if path else None
        result = query_records(args.file, where=load(args.where_json), key_rows=load(args.keys_json),
                               columns=args.columns, limit=args.limit, encoding=args.encoding, delimiter=args.delimiter,
                               max_return_cells=args.max_return_cells, max_return_bytes=args.max_return_bytes,
                               max_record_bytes=args.max_record_bytes,
                               progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
        _write_atomic(args.out, result)
        print(json.dumps({'status': result['status'], 'out': str(args.out.resolve()), 'coverage': result['coverage']}, ensure_ascii=False))
        return 0 if result['status'] == 'complete' else 3
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc), 'error_type': type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
