"""Strict JSON records with lexical numbers and RFC 6901 field locations.

Nested objects/arrays remain within their containing record. We never guess
that an envelope's `data` array or an invoice's items are separate business rows.
"""
import json
from pathlib import Path

MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_RECORD_FIELDS = 20000


class JsonNumber(str):
    """Preserve the source number token before any binary floating conversion."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'JSON重复键，不能唯一定位字段: {key!r}')
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f'JSON不允许非有限数字: {value}')


def decode_record(text):
    return json.loads(text, object_pairs_hook=_object, parse_int=JsonNumber,
                      parse_float=JsonNumber, parse_constant=_constant)


def pointer_token(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def record_fields(record, pointer=''):
    """Flatten leaf fields without changing the containing record's grain."""
    if not isinstance(record, dict):
        raise ValueError('JSON记录必须是对象；不推断标量/数组的业务粒度')
    fields, pointers, types, names_seen = {}, {}, {}, set()
    pending = [(key, value, pointer + '/' + pointer_token(key), 1)
               for key, value in reversed(list(record.items()))]
    while pending:
        root_name, value, location, depth = pending.pop()
        if depth > 64:
            raise ValueError('JSON嵌套超过64层预算')
        if isinstance(value, (dict, list)) and len(pending) + len(value) + len(fields) > MAX_RECORD_FIELDS:
            raise ValueError('JSON记录字段数超过预算，须先缩小记录范围')
        if isinstance(value, dict) and value:
            pending.extend((None, item, location + '/' + pointer_token(key), depth + 1)
                           for key, item in reversed(list(value.items())))
            continue
        if isinstance(value, list) and value:
            pending.extend((None, item, location + '/' + str(index), depth + 1)
                           for index, item in reversed(list(enumerate(value))))
            continue
        # Simple root names remain useful for cross-format key matching. Names
        # beginning '/' use escaped pointers so they cannot alias nested paths.
        name = root_name if root_name and not root_name.startswith('/') else location[len(pointer):]
        normalized = name.strip().casefold()
        if normalized in names_seen:
            raise ValueError('JSON字段名大小写/空格归一后冲突，需显式字段适配')
        names_seen.add(normalized)
        kind = ('number' if isinstance(value, JsonNumber) else 'null' if value is None
                else 'boolean' if isinstance(value, bool) else 'object' if isinstance(value, dict)
                else 'array' if isinstance(value, list) else 'string')
        fields[name] = str(value) if isinstance(value, JsonNumber) else value
        pointers[name], types[name] = location, kind
        if len(fields) > MAX_RECORD_FIELDS:
            raise ValueError('JSON记录字段数超过预算，须先缩小记录范围')
    return fields, pointers, types


def iter_jsonl(filepath, max_record_bytes=MAX_RECORD_BYTES):
    """Read one bounded UTF-8 physical line, retaining exact byte ranges."""
    with open(filepath, 'rb') as handle:
        row_num = offset = 0
        while True:
            raw = handle.readline(max_record_bytes + 1)
            if not raw:
                break
            row_num += 1
            if len(raw) > max_record_bytes:
                raise ValueError(f'JSONL第{row_num}行超过单记录字节预算')
            end = offset + len(raw)
            text = raw.decode('utf-8-sig' if row_num == 1 else 'utf-8')
            if text.strip():
                try:
                    record = decode_record(text)
                    fields, pointers, types = record_fields(record)
                except (ValueError, RecursionError) as exc:
                    raise ValueError(f'JSONL第{row_num}行无效: {exc}') from exc
                yield {'values': fields, 'raw_record': text.rstrip('\r\n'),
                       'row_num': row_num, 'line_end': row_num, 'byte_offset': offset,
                       'byte_end': end, 'json_pointer': '', 'field_pointers': pointers,
                       'value_types': types, 'encoding': 'utf-8', 'structured': True}
            offset = end
