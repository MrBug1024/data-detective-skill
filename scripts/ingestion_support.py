"""Non-executing SQL parsing and hash-bound OCR sidecar ingestion.

These helpers deliberately do not execute SQL, macros, embedded scripts or OCR.
Unsupported SQL expressions remain source text and never become invented values.
"""
import hashlib
import json
import math
import re
from bisect import bisect_right
from pathlib import Path


def sha256_file(filepath):
    """Hash exact file bytes in bounded chunks, including on Python 3.10."""
    digest = hashlib.sha256()
    with open(filepath, 'rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(filepath):
    data = Path(filepath).read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text, encoding = data.decode('utf-16'), 'utf-16'
    else:
        try:
            text, encoding = data.decode('utf-8-sig'), 'utf-8-sig'
        except UnicodeDecodeError:
            # A strict, documented fallback for common Chinese legacy exports.
            # Do not use replacement characters or a permissive Latin-1 fallback.
            text, encoding = data.decode('gb18030'), 'gb18030'
    if '\x00' in text:
        raise ValueError('文本含NUL，可能是无BOM的UTF16或二进制文件，须先确认编码')
    return text, encoding


def ocr_sidecar(filepath, page_count=None):
    """Return validated, independently locatable OCR regions and diagnostics."""
    path = Path(str(filepath) + '.ocr.json')
    if not path.is_file():
        return [], []
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    digest = sha256_file(filepath)
    if not isinstance(data, dict) or data.get('source_sha256') != digest:
        raise ValueError('OCR sidecar source_sha256 缺失或与原始文件不匹配')
    pages = data.get('pages')
    if not isinstance(pages, list) or not pages:
        raise ValueError('OCR sidecar pages 必须为非空列表')
    result = []
    seen_pages = set()
    for page in pages:
        number = page.get('page_num')
        if type(number) is not int or number < 1 or number in seen_pages or (page_count and number > page_count):
            raise ValueError('OCR sidecar 页号必须唯一且在来源范围内')
        seen_pages.add(number)
        width, height = page.get('width'), page.get('height')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (width, height)):
            raise ValueError('OCR sidecar 每页须有正数 width/height（渲染图像像素）')
        regions = page.get('regions')
        if not isinstance(regions, list):
            raise ValueError('OCR sidecar regions 必须为列表')
        for ordinal, region in enumerate(regions, 1):
            text = region.get('text')
            bbox = region.get('bbox')
            confidence = region.get('confidence')
            if not isinstance(text, str) or not text.strip():
                raise ValueError('OCR区域必须有非空文本')
            if not isinstance(bbox, list) or len(bbox) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox):
                raise ValueError('OCR区域bbox须为四个有限坐标')
            x0, y0, x1, y1 = bbox
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise ValueError('OCR区域bbox超出页面或次序错误')
            if confidence is not None and (type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError('OCR confidence 应为0..1或null，且不是识别正确率')
            result.append({'text': text, 'page_num': number, 'region_num': ordinal,
                           'confidence': confidence, 'bbox': bbox, 'image_size': [width, height],
                           'source_sha256': digest, 'sidecar': str(path.resolve()),
                           'engine': data.get('engine', 'unspecified')})
    return result, [{'code': 'ocr_unverified', 'severity': 'warning',
                     'message': 'OCR为派生文本，关键标识/金额须回看原图；分数不代表正确率'}]


def split_sql(text, separator=';'):
    """Yield (start, end) spans, respecting quotes, comments and parentheses."""
    start, pos, depth, quote = 0, 0, 0, None
    while pos < len(text):
        char = text[pos]
        if quote:
            end_quote = ']' if quote == '[' else quote
            if quote.startswith('$'):
                if text.startswith(quote, pos):
                    pos += len(quote)
                    quote = None
                    continue
            elif char == '\\' and quote in ("'", '"', '`'):
                pos += 2
                continue
            elif char == end_quote:
                if pos + 1 < len(text) and text[pos + 1] == end_quote:
                    pos += 2
                    continue
                quote = None
            pos += 1
            continue
        if text.startswith('--', pos) or char == '#':
            end = text.find('\n', pos)
            pos = len(text) if end < 0 else end + 1
            continue
        if text.startswith('/*', pos):
            end = text.find('*/', pos + 2)
            if end < 0:
                raise ValueError('未闭合SQL块注释')
            pos = end + 2
            continue
        if char in ("'", '"', '`', '['):
            quote = char
        elif char == '$' and (match := re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', text[pos:])):
            quote = match.group()
            pos += len(quote)
            continue
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                raise ValueError('SQL括号不平衡')
        elif char == separator and depth == 0:
            yield start, pos
            start = pos + 1
        pos += 1
    if quote or depth:
        raise ValueError('SQL引号或括号未闭合')
    if start < len(text):
        yield start, len(text)


_IDENT = r'(?:"(?:[^"]|"")+"|`(?:[^`]|``)+`|\[[^\]]+\]|[\w$]+)'
_TABLE = _IDENT + r'(?:\s*\.\s*' + _IDENT + r')*'


def sql_identifier(text):
    return '.'.join(part.strip().strip('`"[]') for part in re.split(r'\s*\.\s*', text.strip()))


def _literal(token):
    token = token.strip()
    if re.fullmatch(r'NULL', token, re.I):
        return None, True
    if re.fullmatch(r'(?:TRUE|FALSE)', token, re.I):
        return token.upper() == 'TRUE', True
    if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', token):
        # Text preserves decimal precision and literal leading zero identifiers.
        return token, True
    if re.fullmatch(r"N?'(?:[^'\\]|''|\\.)*'", token, re.S | re.I):
        inner = token[token.index("'") + 1:-1].replace("''", "'")
        # Backslash escapes differ by dialect. Preserve instead of guessing.
        if '\\' in inner:
            return None, False
        return inner, True
    return None, False


def sql_records(text):
    """Return statement/row records and explicit unsupported-construct issues."""
    result, issues, tables = [], [], {}
    newlines = [match.start() for match in re.finditer('\n', text)]
    comma = re.compile(r'\s*,\s*')
    for statement_num, (begin, end) in enumerate(split_sql(text), 1):
        raw = text[begin:end]
        # Strip leading comments only; all original source text is retained.
        prefix = re.match(r'(?:(?:\s+)|(?:--[^\n]*(?:\n|$))|(?:\#[^\n]*(?:\n|$))|(?:/\*[\s\S]*?\*/))*', raw).end()
        sql = raw[prefix:].strip()
        if not sql:
            continue
        offset = begin + prefix
        base = {'statement_num': statement_num, 'char_offset': offset, 'statement_offset': offset,
                'statement_end': end,
                'row_num': bisect_right(newlines, offset - 1) + 1, 'statement': text[offset:end],
                'line_end': bisect_right(newlines, end - 1) + 1}
        create = re.match(r'CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(' + _TABLE + r')\s*\(', sql, re.I)
        if create:
            name = sql_identifier(create.group(1))
            # The outer CREATE list ends at its matching parenthesis.
            close = _matching_close(sql, create.end() - 1)
            columns = []
            for a, b in split_sql(sql[create.end():close], ','):
                definition = sql[create.end():close][a:b].strip()
                match = re.match('(' + _IDENT + ')', definition)
                if match and match.group(1).upper() not in ('PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT', 'KEY', 'INDEX'):
                    columns.append(sql_identifier(match.group(1)))
            tables[name.casefold()] = columns
        insert = re.match(r'INSERT\s+(?:(?:OR\s+(?:IGNORE|REPLACE)|IGNORE)\s+)?INTO\s+(' + _TABLE + r')\s*', sql, re.I)
        if not insert:
            result.append({**base, 'kind': 'statement', 'table': create.group(1) if create else None})
            if not create and re.match(r'(SELECT|WITH|UPDATE|DELETE|COPY|LOAD|EXEC|CALL|MERGE|DROP|ALTER|TRUNCATE|USE|SET|PRAGMA)\b', sql, re.I):
                issues.append({'code': 'sql_static_only', 'severity': 'warning', 'statement_num': statement_num,
                               'message': 'SQL仅保留静态文本，查询/变更/导入不执行，不能视为已读取数据库结果'})
            continue
        table, cursor = sql_identifier(insert.group(1)), insert.end()
        columns = None
        if cursor < len(sql) and sql[cursor] == '(':
            close = _matching_close(sql, cursor)
            columns = [sql_identifier(sql[cursor + 1:close][a:b]) for a, b in split_sql(sql[cursor + 1:close], ',')]
            cursor = close + 1
        values_match = re.match(r'\s*VALUES\s*', sql[cursor:], re.I)
        if not values_match:
            result.append({**base, 'kind': 'statement', 'table': table})
            issues.append({'code': 'sql_nonliteral_insert', 'severity': 'warning', 'statement_num': statement_num,
                           'message': 'INSERT非VALUES形式，仅保留SQL原文，不执行查询'})
            continue
        cursor += values_match.end()
        tuple_num = 0
        while cursor < len(sql) and sql[cursor] == '(':
            close = _matching_close(sql, cursor)
            tokens = [sql[cursor + 1:close][a:b] for a, b in split_sql(sql[cursor + 1:close], ',')]
            parsed = [_literal(token) for token in tokens]
            names = columns if columns is not None else tables.get(table.casefold())
            origin = 'explicit' if columns is not None else 'create_table' if names else 'positional'
            names = names or [f'$column{i}' for i in range(1, len(tokens) + 1)]
            if len(names) != len(tokens) or len({n.casefold() for n in names}) != len(names):
                raise ValueError('SQL INSERT列名重复或列数与VALUES不符')
            tuple_num += 1
            unknown = [names[i] for i, item in enumerate(parsed) if not item[1]]
            tuple_offset = offset + cursor
            result.append({**base, 'kind': 'row', 'table': table, 'columns': names,
                           'values': [item[0] for item in parsed], 'tuple_num': tuple_num,
                           'char_offset': tuple_offset, 'row_num': bisect_right(newlines, tuple_offset - 1) + 1,
                           'line_end': bisect_right(newlines, offset + close) + 1,
                           'tuple_sql': sql[cursor:close + 1], 'column_origin': origin,
                           'unparsed_fields': unknown})
            if unknown:
                issues.append({'code': 'sql_expression_not_evaluated', 'severity': 'warning', 'statement_num': statement_num,
                               'message': 'SQL表达式或方言转义未求值，字段为空并保留原文: ' + ', '.join(unknown)})
            cursor = close + 1
            match = comma.match(sql, cursor)
            if not match:
                break
            cursor = match.end()
        if sql[cursor:].strip():
            issues.append({'code': 'sql_trailing_clause', 'severity': 'warning', 'statement_num': statement_num,
                           'message': 'INSERT尾部子句未执行；已提取VALUES是输入声明，非最终数据库状态'})
        if not tuple_num:
            raise ValueError('SQL VALUES后无可解析元组')
    return result, issues


def _matching_close(text, start):
    # Reuse the scanner: a synthetic delimiter immediately following the outer
    # close reveals its endpoint, while quoted parentheses remain untouched.
    depth, quote, pos = 0, None, start
    while pos < len(text):
        char = text[pos]
        if quote:
            close_quote = ']' if quote == '[' else quote
            if char == '\\':
                pos += 2
                continue
            if char == close_quote:
                if pos + 1 < len(text) and text[pos + 1] == close_quote:
                    pos += 2
                    continue
                quote = None
        elif char in ("'", '"', '`', '['):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    raise ValueError('SQL括号未闭合')
