"""Citable lexical retrieval across rule tables and document sources.

The result records *why a passage was retrieved*, not a proven derivation or a
probability. Source text is always data. No document instruction is executed.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import codecs
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

from ingestion_support import sha256_file
from parsers import FileParser
from document_entities import extract_document_entities, identifier_namespace, _structured_scope


_IDENTIFIER_FIELD = re.compile(
    r'(^|[ _/])(id|uuid|guid|no|number|code|key)([ _/]|$)|'
    r'(?:id|uuid|guid|no|number|code)$|编号|编码|流水|单号|证号|账号|账户|标识', re.I)
_RULE_FIELD = re.compile(r'规则|依据|公式|问题清单|违规|定义|计算方法|rule|formula|policy|definition', re.I)
_RULE_NAME = re.compile(r'规则|标准|制度|政策|规范|办法|条例|rule|policy|formula|regulation', re.I)
_GENERIC = {'none', 'null', 'true', 'false', 'unknown', 'undefined', 'n/a', '未知', '其他', '正常', '合计', '总计'}
_LOW_INFORMATION_FIELD = re.compile(r'^(?:sex|gender|status|state|flag|boolean|性别|状态|标志|是否.+)$', re.I)
_TABLE_EXTENSIONS = {'.xlsx', '.csv', '.tsv', '.jsonl', '.ndjson'}
_DEFAULT_MAX_TERMS = 2048
_PREVIOUS_RETRACTION = re.compile(
    r'(?:撤回|撤销|作废|取消)(?:了)?(?:上(?:一|个)?段|前(?:一|个)?段|上一条|前一条)(?:的)?[^。；;\n]{0,30}'
    r'|\b(?:retract|withdraw|invalidate|revoke)(?:ed|s|ing)?\s+(?:the\s+)?previous\s+(?:paragraph|statement|claim|entry)\b', re.I)


def _term_text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not 3 <= len(text) <= 160 or text.casefold() in _GENERIC:
        return None
    if re.fullmatch(r'[\d\W_]+', text):
        return None
    return text


def _target_terms(target, max_terms):
    """Prefer complete field values; description ngrams are an explicit fallback."""
    if isinstance(target, str):
        target = {'description': target}
    if not isinstance(target, dict):
        raise ValueError('target must be a description string or object')
    terms = {}
    for identifier in target.get('identifiers', []):
        if isinstance(identifier, str):
            identifier = {'value': identifier}
        if not isinstance(identifier, dict) or not isinstance(identifier.get('value'), str):
            raise ValueError('target.identifiers entries require a string value')
        text = identifier['value'].strip()
        if not 1 <= len(text) <= 160:
            raise ValueError('Target identifier length must be 1..160 characters')
        terms[text.casefold()] = {'text': text, 'identifier': True,
                                 'origins': [{'kind': 'target_identifier', 'namespace': identifier.get('namespace', 'unspecified')}]}
    for text in target.get('terms', []):
        if not isinstance(text, str) or not 2 <= len(text.strip()) <= 160:
            raise ValueError('Explicit search terms must contain 2..160 characters')
        text = text.strip()
        terms.setdefault(text.casefold(), {'text': text, 'identifier': False,
                                         'origins': [{'kind': 'explicit_query'}]})
    rows = target.get('rows', [])
    if not rows and isinstance(target.get('values'), dict):
        rows = [target]
    for row in rows:
        values = row.get('values', row) if isinstance(row, dict) else {}
        for field, value in values.items():
            if _LOW_INFORMATION_FIELD.fullmatch(str(field).strip()):
                continue
            text = _term_text(value)
            # Numeric identifiers are useful only when the column explicitly
            # identifies them and they are not a single common digit.
            is_id = bool(_IDENTIFIER_FIELD.search(str(field)))
            if text is None and is_id and isinstance(value, (str, int)) and not isinstance(value, bool):
                candidate = str(value).strip()
                if len(candidate) >= 4 and re.fullmatch(r'[A-Za-z0-9_.:/-]+', candidate):
                    text = candidate
            if text:
                key = text.casefold()
                entry = terms.setdefault(key, {'text': text, 'origins': [], 'identifier': False})
                entry['identifier'] |= is_id and len(text) >= 4
                origin = {'kind': 'value', 'field': str(field), 'result_row': row.get('row_num')}
                if origin not in entry['origins']:
                    entry['origins'].append(origin)
    description = str(target.get('description') or '')
    description = re.sub(r'\.(?:xlsx?|csv|tsv|jsonl?|ndjson|txt|md|docx?|pdf)$', '', description, flags=re.I)
    fragments = re.findall(r'[\u3400-\u9fff]{4,}|[A-Za-z][A-Za-z0-9_-]{3,}', description)
    for fragment in fragments:
        if len(fragment) <= 160:
            entry = terms.setdefault(fragment.casefold(), {'text': fragment, 'origins': [], 'identifier': False})
            entry['origins'].append({'kind': 'description_phrase'})
        if re.fullmatch(r'[\u3400-\u9fff]+', fragment):
            for size in (12, 8, 6):
                for begin in range(max(0, len(fragment) - size + 1)):
                    text = fragment[begin:begin + size]
                    terms.setdefault(text, {'text': text, 'origins': [{'kind': 'description_ngram'}], 'identifier': False})
    items = sorted(terms.values(), key=lambda t: (
        -int(t['identifier']),
        -int(any(o['kind'] == 'value' for o in t['origins'])),
        -len(t['text']), t['text']))
    return items[:max_terms], len(items) > max_terms


@lru_cache(maxsize=8192)
def _term_pattern(token):
    if re.fullmatch(r'[A-Za-z0-9_.:/-]+', token):
        return re.compile(r'(?<![A-Za-z0-9_])' + re.escape(token) + r'(?![A-Za-z0-9_])', re.I)
    return re.compile(re.escape(token), re.I)


def _term_matches(text, term):
    return bool(_term_pattern(term['text']).search(text))


def _non_nested_matches(terms):
    """Do not count 12 overlapping ngrams as 12 independent reasons."""
    result = []
    for term in sorted(terms, key=lambda t: (-int(t['identifier']), -len(t['text']))):
        text = term['text'].casefold()
        if not any(text in accepted['text'].casefold() for accepted in result):
            result.append(term)
    return result


def _profile_for(profiles, file):
    if isinstance(profiles, dict):
        return profiles.get(str(file)) or profiles.get(str(file.resolve()))
    if isinstance(profiles, list):
        return next((p for p in profiles if Path(p.get('file', '')).resolve() == file), None)
    return None


def _sheet_profiles(profile):
    # profile_source's stable public representation is a list of sheet records.
    return profile.get('sheets', []) if profile else []


def _read_table(file, profile, cache_dir, max_units, options=None):
    options = options or {}
    if file.suffix.lower() == '.xlsx':
        if not profile:
            from source_profile import profile_source
            profile = profile_source(str(file), cache_dir=cache_dir)
        sheets = _sheet_profiles(profile)
        if not sheets:
            raise ValueError('No usable sheet profiles; choose a sheet/header explicitly')
        from xlsx_query import query_xlsx
        for sheet in sheets:
            header = sheet.get('header_row')
            if not header or sheet.get('header_status') in ('ambiguous', 'unavailable'):
                yield None, [{'code': 'ambiguous_table_header', 'severity': 'warning',
                              'sheet': sheet.get('sheet'), 'message': '规则表表头不明确，需显式指定，未猜测'}]
                continue
            query = query_xlsx(str(file), sheet=sheet.get('sheet') or sheet.get('name'),
                               header_row=header, limit=max_units + 1, cache_dir=cache_dir)
            yield query, query.get('issues', [])
    else:
        from record_query import query_records
        encoding = options.get('encoding')
        if encoding is None and file.suffix.lower() in ('.csv', '.tsv'):
            sample = next(iter((profile or {}).get('sheets', [])), {})
            sample_row = next(iter(sample.get('sample_rows', [])), {})
            encoding = sample_row.get('encoding') or _text_encoding(file)
        query = query_records(str(file), limit=max_units + 1,
                              encoding=encoding or 'utf-8-sig', delimiter=options.get('delimiter'))
        yield query, query.get('issues', [])


def _records_from_query(query):
    filename = query.get('file', query.get('source_file'))
    if not filename:
        raise ValueError('Evidence query must identify its source file')
    for row in query.get('rows', query.get('records', [])):
        values = row.get('values', {})
        text = ' | '.join(f'{field}={value}' for field, value in values.items() if value is not None)
        location = {'file': filename, 'sheet': query.get('sheet'),
                    'row': row.get('row_num'), 'cells': row.get('cells', {})}
        for name in ('json_pointer', 'field_pointers', 'line_end', 'byte_offset', 'byte_end', 'char_offset', 'char_end', 'encoding', 'delimiter', 'offset_scope'):
            if name in row:
                location[name] = row[name]
        yield {'text': text, 'values': values, 'location': location, 'quote_is_source_text': False,
               'structured_record': True,
               'is_rule': bool(_RULE_NAME.search(Path(filename).stem)) or any(_RULE_FIELD.search(str(k)) for k in values)}


def _records_from_units(units):
    for unit in units:
        location = {'file': unit.source_file, 'sheet': unit.sheet_name, 'row': unit.row_num,
                    'page': unit.page_num, 'paragraph': unit.paragraph_num, 'char_offset': unit.char_offset,
                    'metadata': unit.metadata, 'ocr_confidence': unit.ocr_confidence}
        for name in ('cells', 'json_pointer', 'field_pointers', 'line_end', 'byte_offset', 'byte_end'):
            if name in unit.metadata:
                location[name] = unit.metadata[name]
        textual = unit.file_type in ('txt', 'md', 'pdf') or (unit.file_type == 'docx' and not unit.sheet_name) or bool(unit.metadata.get('bbox'))
        structured = (unit.file_type in ('xlsx', 'xls', 'csv', 'tsv', 'json', 'jsonl', 'ndjson')
                      or unit.metadata.get('structured') or unit.metadata.get('structural')
                      or (unit.file_type == 'docx' and unit.sheet_name))
        yield {'text': unit.raw_text, 'values': dict(zip(unit.field_names, unit.field_values)),
               'location': location, 'quote_is_source_text': textual, 'structured_record': bool(structured),
               'is_rule': unit.is_rule or bool(_RULE_NAME.search(Path(unit.source_file).stem))
                          or any(_RULE_FIELD.search(str(k)) for k in unit.field_names)}


def _text_encoding(file):
    """Validate UTF-8 incrementally before falling back to strict GB18030."""
    with file.open('rb') as stream:
        prefix = stream.read(4)
        if prefix.startswith((b'\xff\xfe', b'\xfe\xff')):
            return 'utf-16'
        stream.seek(0)
        decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
        try:
            while block := stream.read(1024 * 1024):
                if '\x00' in decoder.decode(block):
                    raise ValueError('Text contains NUL; confirm the source encoding before retrieval')
            decoder.decode(b'', final=True)
            return 'utf-8-sig'
        except UnicodeDecodeError:
            return 'gb18030'


def _stream_text_records(file):
    """Scan arbitrarily long text without materializing a document or paragraph.

    Quotes refer to decoded source characters (CRLF retained), never UTF-8 byte
    offsets. Overlap exceeds the longest supported query term; duplicate hits
    retain their common source dependency group.
    """
    encoding = _text_encoding(file)
    size, overlap = 8000, 256
    offset, line, ordinal = 0, 1, 1
    with file.open('r', encoding=encoding, errors='strict', newline='') as stream:
        chunk = stream.read(size)
        while chunk:
            if '\x00' in chunk:
                raise ValueError('Text contains NUL; confirm the source encoding before retrieval')
            location = {'file': str(file), 'sheet': None, 'row': line, 'page': None,
                        'paragraph': None, 'char_offset': offset,
                        'metadata': {'encoding': encoding, 'offset_scope': 'decoded_source_text',
                                     'line_end': line + chunk.count('\n'), 'streaming_chunk': ordinal,
                                     'chunk_overlap_chars': overlap if ordinal > 1 else 0}}
            yield {'text': chunk, 'values': {'content': chunk}, 'location': location,
                   'quote_is_source_text': True,
                   'is_rule': bool(_RULE_NAME.search(file.stem)) or bool(_RULE_FIELD.search(chunk[:1000]))}
            if len(chunk) < size:
                break
            advance = len(chunk) - overlap
            following = stream.read(advance)
            if not following:
                break
            offset += advance
            line += chunk[:advance].count('\n')
            chunk = chunk[-overlap:] + following
            ordinal += 1


def _quote(text, terms, maximum=1800):
    positions = [match.start() for term in terms if (match := _term_pattern(term['text']).search(text))]
    begin = max(0, min(positions, default=0) - 120) if len(text) > maximum else 0
    end = min(len(text), begin + maximum)
    return {'text': text[begin:end], 'char_start_in_value': begin,
            'char_end_in_value': end, 'truncated': begin != 0 or end != len(text)}


def _quotes(record, matches):
    if record['quote_is_source_text']:
        quoted = _quote(record['text'], matches)
        loc = dict(record['location'])
        if loc.get('char_offset') is not None:
            loc['char_offset'] += quoted['char_start_in_value']
            loc['char_end'] = loc['char_offset'] + len(quoted['text'])
        return [{**quoted, 'source_location': loc, 'kind': 'extracted_text'}]
    result = []
    for field, value in record['values'].items():
        if not isinstance(value, str) or not any(_term_matches(value, term) for term in matches):
            continue
        loc = dict(record['location'])
        loc.pop('cells', None)
        loc['field'] = field
        cell = record['location'].get('cells', {}).get(field)
        if cell:
            loc['cell'] = cell.get('address') if isinstance(cell, dict) else cell
        pointer = record['location'].get('field_pointers', {}).get(field)
        if pointer:
            loc['json_pointer'] = pointer
        result.append({**_quote(value, matches), 'source_location': loc, 'kind': 'field_value'})
    return result


def retrieve_evidence(target, sources, *, profiles=None, tables=None, cache_dir=None,
                      limit=30, progress=None, max_units=100000,
                      max_source_bytes=64 * 1024 * 1024, max_terms=_DEFAULT_MAX_TERMS,
                      max_candidate_chars=32 * 1024 * 1024, source_options=None,
                      config=None, entity_aliases=None, scope_aliases=None,
                      max_document_hops=4, max_lookup_seeds=2048, _expand=True):
    """Retrieve relevant passages while separating mentions, rules and proof.

    ``target`` is ``{'description': str, 'rows': [{'values': {...}}]}``.
    ``profiles`` maps absolute file paths to ``profile_source`` results.
    Optional ``tables`` maps paths to already queried table reports. Only pass
    complete source queries here; filtered input is labelled partial.
    ``limit`` bounds returned passages, never silently claims full coverage.
    """
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('limit must be an integer in 1..10000')
    if type(max_units) is not int or max_units < 1 or type(max_terms) is not int or max_terms < 1:
        raise ValueError('max_units/max_terms must be positive integers')
    if type(max_candidate_chars) is not int or max_candidate_chars < 1:
        raise ValueError('max_candidate_chars must be a positive integer')
    config = config or {}
    entity_aliases = entity_aliases if entity_aliases is not None else config.get('entity_aliases')
    scope_aliases = scope_aliases if scope_aliases is not None else config.get('scope_aliases')
    source_options = source_options if source_options is not None else config.get('sources')
    max_document_hops = config.get('evidence', {}).get('max_document_hops', max_document_hops)
    max_lookup_seeds = config.get('evidence', {}).get('max_lookup_seeds', max_lookup_seeds)
    if type(max_document_hops) is not int or not 1 <= max_document_hops <= 8:
        raise ValueError('max_document_hops must be an integer in 1..8')
    if type(max_lookup_seeds) is not int or not 1 <= max_lookup_seeds <= 10000:
        raise ValueError('max_lookup_seeds must be an integer in 1..10000')
    terms, term_truncated = _target_terms(target, max_terms)
    issues, source_status, potential, revisions = [], [], [], []
    frequencies = Counter()
    units_scanned = 0
    retained_chars = 0
    if term_truncated:
        issues.append({'code': 'query_terms_truncated', 'severity': 'warning',
                       'message': '检索词超过预算；须分批重试补足检索覆盖', 'max_terms': max_terms})
    files = list(dict.fromkeys(str(Path(p).resolve()) for p in sources))
    for source in files:
        file = Path(source)
        status = {'file': source, 'status': 'complete', 'units_scanned': 0, 'source_sha256': None}
        source_status.append(status)
        if progress:
            progress({'phase': 'retrieving_evidence', 'source': source, 'source_index': len(source_status)})
        try:
            if file.suffix.lower() not in ('.txt', '.md') and file.stat().st_size > max_source_bytes:
                raise ValueError(f'Evidence source exceeds {max_source_bytes} byte full-text budget; provide a bounded source query')
            initial_stat = file.stat()
            initial_hash = sha256_file(file)
            status['source_sha256'] = initial_hash
            records, local_issues = [], []
            supplied = tables.get(source) if isinstance(tables, dict) else None
            if supplied is not None or file.suffix.lower() in _TABLE_EXTENSIONS:
                table_queries = ((q, q.get('issues', [])) for q in
                                 (supplied if isinstance(supplied, list) else [supplied])) if supplied is not None else _read_table(
                                     file, _profile_for(profiles, file), cache_dir, max_units,
                                     (source_options or {}).get(source, (source_options or {}).get(file.name, {})))
                for query, query_issues in table_queries:
                    local_issues.extend(query_issues)
                    if query is None:
                        status['status'] = 'partial'
                        continue
                    cov = query.get('coverage', {})
                    if (not cov.get('scan_complete') or cov.get('truncated')
                            or cov.get('filter_evaluation_complete') is False
                            or query.get('where') or query.get('key_rows')):
                        status['status'] = 'partial'
                        local_issues.append({'code': 'evidence_table_coverage_partial', 'severity': 'warning',
                                             'message': '仅检索了规则表的选定记录，未证明整表覆盖', 'sheet': query.get('sheet')})
                    if query.get('source_sha256') != initial_hash:
                        raise ValueError('Supplied table query source hash does not match source file')
                    records.extend(_records_from_query(query))
            elif file.suffix.lower() in ('.txt', '.md') and file.stat().st_size > max_source_bytes:
                records = _stream_text_records(file)
            else:
                parser = FileParser()
                records = _records_from_units(parser.parse(str(file)))
                local_issues.extend(parser.last_diagnostics)
            for issue in local_issues:
                issues.append({'file': source, **issue})
                if issue.get('severity') in ('warning', 'error'):
                    status['status'] = 'partial'
            previous_location = None
            for ordinal, record in enumerate(records, 1):
                if ordinal > max_units:
                    status['status'] = 'partial'
                    issues.append({'file': source, 'code': 'evidence_units_truncated', 'severity': 'warning',
                                   'max_units': max_units, 'message': '检索单元超过预算；需按来源位置拆分后重试'})
                    break
                units_scanned += 1
                status['units_scanned'] += 1
                if not record.get('structured_record'):
                    for change in _PREVIOUS_RETRACTION.finditer(record['text']):
                        if previous_location is not None:
                            revisions.append({'kind': 'explicit_previous_unit_retraction',
                                'source_location': record['location'], 'source_sha256': initial_hash,
                                'target_location': previous_location,
                                'selector': {'type': 'TextPositionSelector', 'start': change.start(), 'end': change.end(),
                                             'offset_scope': 'unit_text', 'exact': change.group(),
                                             'source_start': record['location']['char_offset'] + change.start()
                                                 if type(record['location'].get('char_offset')) is int else None,
                                             'source_end': record['location']['char_offset'] + change.end()
                                                 if type(record['location'].get('char_offset')) is int else None,
                                             'quote': {'type': 'TextQuoteSelector', 'exact': change.group(),
                                                       'prefix': record['text'][max(0, change.start() - 32):change.start()],
                                                       'suffix': record['text'][change.end():change.end() + 32]}}})
                    previous_location = record['location']
                if progress and ordinal % 1000 == 0:
                    progress({'phase': 'retrieving_evidence', 'source': source,
                              'units_scanned': status['units_scanned']})
                all_matches = [t for t in terms if _term_matches(record['text'], t)]
                matched = _non_nested_matches(all_matches)
                if matched:
                    if retained_chars + len(record['text']) > max_candidate_chars:
                        status['status'] = 'partial'
                        issues.append({'file': source, 'code': 'evidence_candidate_budget', 'severity': 'warning',
                                       'max_candidate_chars': max_candidate_chars,
                                       'message': '命中文本超过保留预算；缩小目标或分批检索，未宣称后续来源已检索'})
                        break
                    retained_chars += len(record['text'])
                    for term in matched:
                        frequencies[term['text'].casefold()] += 1
                    potential.append({**record, 'matches': matched, 'all_matches': all_matches,
                                      'source_sha256': initial_hash})
            if status['units_scanned'] == 0 and status['status'] == 'complete':
                status['status'] = 'partial'
                issues.append({'file': source, 'code': 'empty_source', 'severity': 'warning',
                               'message': '来源未提取到可检索内容，不能据此作否定判断'})
            final_stat = file.stat()
            if ((initial_stat.st_size, initial_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns)
                    or sha256_file(file) != initial_hash):
                raise ValueError('Source changed during evidence retrieval; retry against a stable file')
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            status['status'] = 'unavailable'
            # A source that changed or failed halfway must not leave trusted hits.
            retained_chars -= sum(len(record['text']) for record in potential if record['location']['file'] == source)
            potential = [record for record in potential if record['location']['file'] != source]
            issues.append({'file': source, 'code': 'evidence_source_unavailable', 'severity': 'error',
                           'type': type(exc).__name__, 'message': str(exc)[:500]})
    # Exclude any source invalidated by a parse/change failure from ranking.
    frequencies = Counter(term['text'].casefold() for record in potential for term in record['matches'])
    retrieved = []
    selected_records = {}
    for record in potential:
        matches = record['matches']
        identifiers = [t for t in matches if t['identifier']]
        substantive = [t for t in matches if not t['identifier'] and len(t['text']) >= 4]
        phrase = any(len(t['text']) >= 8 and any(o['kind'] != 'description_ngram' for o in t['origins']) for t in substantive)
        # A common short label, name, or one overlapping fragment alone cannot
        # establish a meaningful document association.
        explicit_query = any(any(origin['kind'] == 'explicit_query' for origin in term['origins']) for term in matches)
        if not identifiers and not (phrase or len(substantive) >= 2 or explicit_query):
            continue
        # If a phrase is present in most units it is background context, not an
        # informative association. Identifier mentions remain reportable.
        informative = [t for t in substantive if frequencies[t['text'].casefold()] / max(1, units_scanned) <= 0.35]
        if not identifiers and not explicit_query and units_scanned >= 10 and not informative:
            continue
        score = sum((4 if t['identifier'] else 1) * min(3, len(t['text']) / 6) *
                    (1 + math.log((1 + units_scanned) / (1 + frequencies[t['text'].casefold()]))) for t in matches)
        location = record['location']
        content_hash = hashlib.sha256(record['text'].encode('utf-8')).hexdigest()
        evidence_id = 'doc_' + hashlib.sha256((str(location) + content_hash).encode('utf-8')).hexdigest()[:16]
        selected_records[evidence_id] = record
        kind = 'identifier_mention' if identifiers else 'rule_candidate' if record['is_rule'] else 'text_candidate'
        quotes = _quotes(record, record['all_matches'])
        preview = '\n'.join(q['text'] for q in quotes)[:4000]
        retrieved.append({'evidence_id': evidence_id, 'kind': kind,
                          'match_type': 'direct' if identifiers else 'rule' if record['is_rule'] else 'semantic',
                          'source_location': location, 'source_sha256': record['source_sha256'],
                          'content_sha256': content_hash, 'duplicate_group': 'content_' + content_hash[:16],
                          'dependency_group': 'source_' + record['source_sha256'][:16],
                          'independence': 'unknown_same_content_not_independent',
                          'score': round(score, 6), 'confidence': None,
                          'score_semantics': 'lexical_retrieval_rank_not_probability_or_lineage',
                          'matched_terms': matches, 'quotes': quotes, 'value': preview, 'matched_value': preview,
                          'verification': 'related_source_passage_not_verified_derivation',
                          'entity_linkage': 'identifier_mention_only' if identifiers else 'unresolved',
                          'rule_applicability': 'not_verified' if record['is_rule'] else 'not_assessed',
                          'evidence': '原文/字段值命中：' + '、'.join(t['text'] for t in matches[:12])})
    retrieved.sort(key=lambda e: (-e['score'], e['source_location']['file'], str(e['source_location'])))
    total = len(retrieved)
    selected = retrieved[:limit]
    for evidence in selected:
        record = selected_records[evidence['evidence_id']]
        packet = extract_document_entities(record['text'], location=record['location'],
                                           source_sha256=record['source_sha256'],
                                           fields=record['values'] if record.get('structured_record') else None,
                                           entity_aliases=entity_aliases, scope_aliases=scope_aliases)
        evidence['entity_mentions'] = packet['mentions']
        evidence['bridge_candidates'] = packet['bridges']
        evidence['entity_extraction_coverage'] = packet['coverage']
        if record.get('structured_record'):
            evidence['record'] = {'values': record['values'], 'source_location': record['location'],
                                  'cells': record['location'].get('cells', {}),
                                  'field_pointers': record['location'].get('field_pointers', {})}
        issues.extend({'file': record['location']['file'], **issue} for issue in packet['issues'])
    _apply_source_revisions(selected, revisions)
    if total > limit:
        issues.append({'code': 'evidence_return_limit', 'severity': 'warning', 'matched_units': total,
                       'returned_units': limit, 'message': '已检索全部可读单元，但只返回排名靠前的证据；提高limit查看其余候选'})
    complete = not term_truncated and all(s['status'] == 'complete' for s in source_status)
    report = {'analysis_stage': 'related_evidence_retrieval', 'evidence': selected, 'issues': issues,
            'sources': source_status, 'coverage': {'source_files_requested': len(files),
                'source_files_complete': sum(s['status'] == 'complete' for s in source_status),
                'scan_complete': complete, 'retrieval_complete': complete,
                'units_scanned': units_scanned, 'matched_units': total, 'returned_units': len(selected),
                'truncated': total > limit, 'query_terms': len(terms), 'query_terms_truncated': term_truncated},
            'source_revisions': revisions, 'rule_applicability': 'not_verified', 'historical_lineage': 'not_proven'}
    if _expand:
        closure_config = dict(config, entity_aliases=entity_aliases or {}, scope_aliases=scope_aliases or {})
        closure_config['evidence'] = dict(config.get('evidence', {}), max_document_hops=max_document_hops,
                                          max_lookup_seeds=max_lookup_seeds)
        _expand_document_evidence(report, target, files, closure_config, cache_dir=cache_dir, limit=limit,
                                  progress=progress, max_units=max_units, max_source_bytes=max_source_bytes,
                                  max_terms=max_terms, max_candidate_chars=max_candidate_chars,
                                  source_options=source_options)
    return report


def _apply_source_revisions(evidence, revisions):
    """A citable withdrawal blocks only the explicitly referenced earlier unit."""
    for item in evidence:
        changes = [change for change in revisions if change['source_sha256'] == item['source_sha256']
                   and change['target_location'] == item['source_location']]
        if not changes:
            continue
        item['source_revisions'] = changes
        for mention in item.get('entity_mentions', []):
            mention.setdefault('original_assertion', mention['assertion'])
            mention['assertion'] = {'status': 'retracted', 'cues': changes,
                                    'semantics': 'explicit_same_source_previous_unit_retraction'}
            mention['can_seed_lookup'] = False
        for bridge in item.get('bridge_candidates', []):
            bridge['can_seed_lookup'] = False
            bridge['reason'] = 'explicit_same_source_previous_unit_retraction'


def _mark_located_records(evidence, traced):
    """Allow a record bridge only after an independent exact source query hit."""
    def key_for(location, digest):
        metadata = location.get('metadata') or {}
        if 'json_pointer' in location or 'json_pointer' in metadata:
            address = ('json_pointer', location.get('json_pointer', metadata.get('json_pointer')))
        else:
            address = ('physical_row', location.get('row', location.get('row_num')),
                       location.get('tuple_num', metadata.get('tuple_num')))
        return (location.get('file'), location.get('sheet'), address, digest)
    located = {}
    for row in traced.get('result_rows', []):
        for match in row.get('source_matches', []):
            if match.get('status') == 'located' and match.get('query_complete') is True:
                key = key_for(match, match.get('source_sha256'))
                located[key] = match
    for item in evidence:
        if not item.get('record'):
            continue
        item.pop('record_localization', None)
        location = item['source_location']
        key = key_for(location, item.get('source_sha256'))
        match = located.get(key)
        if match:
            item['record_localization'] = {'status': 'located', 'source_ref': match.get('source_ref'),
                                           'source_sha256': match.get('source_sha256'),
                                           'query_complete': True, 'location': match.get('location') or {
                                               key: match[key] for key in ('file', 'sheet', 'row_num', 'tuple_num', 'json_pointer') if key in match},
                                           'basis': 'completed_exact_query_before_record_bridge',
                                           'match_keys': match.get('match_keys')}


def evidence_from_located_records(rows_or_matches, *, config=None):
    """Extract citable record/note bridges from complete unique query hits.

    Accept result rows containing ``source_matches`` or a flat match list. This
    reads no files, performs no new query, and makes no whole-source coverage
    claim. Field values/cells/pointers are those already returned by the engine.
    """
    config = config or {}
    matches = [match for item in rows_or_matches for match in item.get('source_matches', [item])]
    evidence, seen = [], set()
    for match in matches:
        if (match.get('status') != 'located' or match.get('query_complete') is not True
                or not match.get('source_sha256') or not match.get('source_ref')
                or not isinstance(match.get('values'), dict)):
            continue
        identity = (match['source_ref'], match['source_sha256'])
        if identity in seen:
            continue
        seen.add(identity)
        location = dict(match.get('location') or {})
        for key in ('file', 'sheet', 'line_end', 'char_offset', 'offset_scope', 'tuple_num', 'json_pointer', 'row_num_kind'):
            if key in match:
                location.setdefault(key, match[key])
        location['row'] = match.get('row_num', location.get('row_num'))
        location['cells'], location['field_pointers'] = match.get('cells', {}), match.get('field_pointers', {})
        location['metadata'] = dict(location.get('metadata') or {}, structured=True, raw_text_is_source_quote=False)
        values = match['values']
        text = '\n'.join(str(field) + '=' + str(value) for field, value in values.items() if value is not None)
        packet = extract_document_entities(text, fields=values, location=location,
                                           source_sha256=match['source_sha256'],
                                           entity_aliases=config.get('entity_aliases'), scope_aliases=config.get('scope_aliases'))
        content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        evidence_id = 'record_' + hashlib.sha256(json.dumps(identity).encode('utf-8')).hexdigest()[:20]
        quotes = []
        for field, value in values.items():
            if value is None:
                continue
            field_location = {key: value for key, value in location.items() if key not in ('cells', 'field_pointers')}
            field_location['field'] = field
            cell = match.get('cells', {}).get(field)
            if cell:
                field_location['cell'] = cell.get('address') if isinstance(cell, dict) else cell
            if field in match.get('field_pointers', {}):
                field_location['json_pointer'] = match['field_pointers'][field]
            quotes.append({'text': str(value), 'source_location': field_location, 'kind': 'field_value', 'matched_terms': []})
        item = {'evidence_id': evidence_id, 'kind': 'located_record', 'match_type': 'direct',
                'source_location': location, 'source_sha256': match['source_sha256'], 'source_ref': match['source_ref'],
                'content_sha256': content_hash, 'duplicate_group': 'content_' + content_hash[:16],
                'dependency_group': 'source_' + match['source_sha256'][:16],
                'independence': 'unknown_same_content_not_independent', 'score': None, 'confidence': None,
                'score_semantics': 'not_scored_exact_structural_lookup', 'matched_terms': [], 'quotes': quotes,
                'value': text[:4000], 'matched_value': text[:4000], 'text_is_source_quote': False,
                'verification': 'located_source_record_not_verified_derivation', 'entity_linkage': 'exact_record_lookup',
                'rule_applicability': 'not_assessed', 'evidence': '已定位记录的原始字段值',
                'entity_mentions': packet['mentions'], 'bridge_candidates': packet['bridges'],
                'entity_extraction_coverage': packet['coverage'], 'entity_extraction_issues': packet['issues'],
                'record': {'values': values, 'source_location': location, 'cells': match.get('cells', {}),
                           'field_pointers': match.get('field_pointers', {})},
                'coverage': {'scope': 'already_located_record_only', 'source_scan_claim': False}}
        _mark_located_records([item], {'result_rows': [{'source_matches': [match]}]})
        evidence.append(item)
    return evidence


def _target_anchors(target, config=None):
    """Explicit query/schema identifiers, with target row and scope preserved."""
    config = config or {}
    if isinstance(target, str):
        packet = extract_document_entities(target, entity_aliases=config.get('entity_aliases'),
                                           scope_aliases=config.get('scope_aliases'))
        target = {'description': target, 'identifiers': [
            {'value': m['raw_value'], 'namespace': m['identifier_namespace'], 'scope': m['scope']}
            for m in packet['mentions'] if m['can_seed_lookup']]}
    anchors = []
    for item in target.get('identifiers', []):
        if isinstance(item, str):
            item = {'value': item, 'namespace': 'unspecified'}
        namespace = item.get('namespace', item.get('identifier_namespace', 'unspecified'))
        anchors.append({'identifier_namespace': namespace, 'value': str(item['value']),
                        'scope': item.get('scope', {'values': {}, 'ambiguous': {}}),
                        'result_row': item.get('result_row', target.get('target_index', 1)),
                        'origin': 'target_identifier'})
    for index, row in enumerate(target.get('rows', []), 1):
        fields = row.get('values', {})
        scope = _structured_scope(fields, config.get('scope_aliases'))
        for field, value in fields.items():
            namespace = identifier_namespace(field, config=config)
            if namespace and value is not None and not isinstance(value, (dict, list, bool)):
                anchors.append({'identifier_namespace': namespace, 'value': str(value), 'scope': scope,
                                'field': field, 'result_row': row.get('row_num', index),
                                'origin': 'result_field'})
    return anchors


def _merge_scope(left, right):
    if left.get('ambiguous') or right.get('ambiguous'):
        return None
    values = dict(left.get('values', {}))
    for namespace, value in right.get('values', {}).items():
        if namespace in values and values[namespace] != value:
            return None
        values[namespace] = value
    return {'values': values, 'ambiguous': {}}


def build_lookup_seeds(target, evidence, *, config=None):
    """Bounded typed candidate closure; never proves identity or relationships.

    Exact namespace/value plus nonconflicting scope reconnects repeated mentions.
    Each traversed edge must already pass assertion/OCR/ambiguity gates, and every
    returned seed retains its original target and all citable bridge steps.
    """
    config = config or {}
    budget = config.get('evidence', {}).get('max_lookup_seeds', 2048)
    max_hops = config.get('evidence', {}).get('max_document_hops', 4)
    mentions, index, edges, located_records = {}, defaultdict(list), defaultdict(list), {}
    for item in evidence:
        proof = item.get('record_localization') or {}
        if item.get('record') and not (proof.get('status') == 'located' and proof.get('query_complete') is True
                and proof.get('source_ref') and proof.get('source_sha256') == item.get('source_sha256')):
            # An unlocated record cannot supply its own missing scope/key and
            # then prove itself. Table propagation belongs to exact query joins.
            continue
        if item.get('record_localization'):
            located_records[item['evidence_id']] = item['record_localization']
        local = {m['mention_id']: m for m in item.get('entity_mentions', [])}
        for mention_id, mention in local.items():
            if mention.get('entity_type') == 'identifier' and mention.get('can_seed_lookup'):
                mentions[mention_id] = mention
                index[(mention['identifier_namespace'], mention['raw_value'])].append(mention_id)
        for bridge in item.get('bridge_candidates', []):
            left, right = bridge['from_mention'], bridge['to_mention']
            if bridge.get('can_seed_lookup') and left in mentions and right in mentions:
                edges[left].append((right, bridge, item['evidence_id']))
                edges[right].append((left, bridge, item['evidence_id']))
    anchors = _target_anchors(target, config)
    result, emitted, truncated = [], set(), False
    for anchor in anchors:
        if anchor['scope'].get('ambiguous'):
            continue
        queue = deque([(anchor['identifier_namespace'], anchor['value'], anchor['scope'], [])])
        visited = set()
        while queue:
            namespace, value, scope, path = queue.popleft()
            key = (namespace, value, json.dumps(scope, sort_keys=True, ensure_ascii=False))
            if key in visited:
                continue
            visited.add(key)
            candidates = index.get((namespace, value), []) if namespace != 'unspecified' else [
                mid for (ns, literal), mids in index.items() if literal == value for mid in mids]
            for mid in candidates:
                matched = mentions[mid]
                inherited = _merge_scope(scope, matched['scope'])
                if inherited is None:
                    continue
                for other_id, bridge, evidence_id in edges[mid]:
                    other = mentions[other_id]
                    merged_scope = _merge_scope(inherited, other['scope'])
                    if merged_scope is None:
                        continue
                    next_key = (other['identifier_namespace'], other['raw_value'],
                                json.dumps(merged_scope, sort_keys=True, ensure_ascii=False))
                    if next_key in visited or (other['raw_value'] == anchor['value'] and
                            anchor['identifier_namespace'] in ('unspecified', other['identifier_namespace'])):
                        continue
                    if len(path) >= max_hops:
                        truncated = True
                        continue
                    step = {'bridge_id': bridge['bridge_id'], 'evidence_id': evidence_id,
                            'from': {'identifier_namespace': matched['identifier_namespace'], 'value': matched['raw_value'],
                                     'mention_id': mid, 'selector': matched.get('selector')},
                            'to': {'identifier_namespace': other['identifier_namespace'], 'value': other['raw_value'],
                                   'mention_id': other_id, 'selector': other.get('selector')},
                            'source_location': bridge['source_location'], 'source_sha256': bridge.get('source_sha256'),
                            'kind': bridge['kind'], 'relationship_verified': False}
                    if evidence_id in located_records:
                        step['record_localization'] = located_records[evidence_id]
                    provenance = path + [step]
                    output_key = (anchor['result_row'],) + next_key
                    if output_key not in emitted:
                        if len(result) >= budget:
                            truncated = True
                            continue
                        emitted.add(output_key)
                        result.append({'result_row': anchor['result_row'], 'target_index': anchor['result_row'],
                                       'identifier_namespace': other['identifier_namespace'], 'value': other['raw_value'],
                                       'scope': merged_scope, 'source_location': other['source_location'],
                                       'source_sha256': other.get('source_sha256'),
                                       'evidence_id': evidence_id, 'bridge_id': bridge['bridge_id'],
                                       'anchor': anchor, 'provenance': provenance, 'can_seed_lookup': True,
                                       'verification': 'candidate_for_lookup_not_verified_relationship'})
                    queue.append((other['identifier_namespace'], other['raw_value'], merged_scope, provenance))
    if truncated:
        for seed in result:
            seed['closure_truncated'] = True
    return result


def _expand_document_evidence(report, target, files, config, *, cache_dir, limit, progress, **parameters):
    """Revisit only document sources for newly encountered typed literals."""
    documents = [file for file in files if Path(file).suffix.lower() not in _TABLE_EXTENSIONS]
    known = {item['evidence_id']: item for item in report['evidence']}
    queried = {(a['identifier_namespace'], a['value']) for a in _target_anchors(target, config)}
    rounds, exhausted, expansions = 1, False, []
    max_hops = config.get('evidence', {}).get('max_document_hops', 4)
    while documents:
        seeds = build_lookup_seeds(target, list(known.values()), config=config)
        frontier = {(s['identifier_namespace'], s['value']) for s in seeds} - queried
        if not frontier:
            break
        if rounds >= max_hops:
            exhausted = True
            break
        if progress:
            progress({'phase': 'expanding_document_evidence', 'round': rounds + 1, 'identifiers': len(frontier)})
        query = {'identifiers': [{'namespace': namespace, 'value': value} for namespace, value in sorted(frontier)]}
        next_report = retrieve_evidence(query, documents, config=config, cache_dir=cache_dir, limit=limit,
                                        progress=progress, _expand=False, **parameters)
        queried.update(frontier)
        rounds += 1
        expansions.append(next_report['coverage'])
        report['issues'].extend(next_report['issues'])
        report.setdefault('source_revisions', []).extend(next_report.get('source_revisions', []))
        for evidence in next_report['evidence']:
            known.setdefault(evidence['evidence_id'], evidence)
        _apply_source_revisions(list(known.values()), report.get('source_revisions', []))
        # A changed source cannot retain earlier passages under an old snapshot.
        statuses = {source['file']: source for source in next_report['sources']}
        for source in report['sources']:
            newer = statuses.get(source['file'])
            if newer and (newer['source_sha256'] != source['source_sha256'] or newer['status'] != 'complete'):
                source['status'] = newer['status'] if newer['status'] != 'complete' else 'unavailable'
                if newer['source_sha256'] != source['source_sha256']:
                    known = {key: item for key, item in known.items() if item['source_location']['file'] != source['file']}
                    report['issues'].append({'code': 'evidence_source_changed_between_hops', 'severity': 'error', 'file': source['file']})
    coverage = report['coverage']
    report['evidence'] = list(known.values())[:limit]
    coverage.update(document_search_rounds=rounds, document_hop_budget=max_hops,
                    document_expansion_complete=not exhausted,
                    returned_units=len(report['evidence']), matched_units=max(coverage['matched_units'], len(known)))
    coverage['units_scanned'] += sum(c['units_scanned'] for c in expansions)
    coverage['truncated'] |= exhausted or len(known) > limit or any(c['truncated'] for c in expansions)
    coverage['retrieval_complete'] &= not exhausted and all(c['retrieval_complete'] for c in expansions)
    coverage['scan_complete'] &= all(c['scan_complete'] for c in expansions)
    if exhausted:
        report['issues'].append({'code': 'document_hop_budget', 'severity': 'warning', 'max_document_hops': max_hops,
                                 'message': '文档中的新标识仍有未继续检索的前沿；提高跳数或缩小目标后重试'})
    if len(known) > limit:
        report['issues'].append({'code': 'evidence_closure_return_limit', 'severity': 'warning', 'matched_units': len(known),
                                 'returned_units': limit, 'message': '跨段证据超过返回预算；部分桥接证据未返回'})


def explore_evidence(target, sources, *, cache_dir=None, limit=30, progress=None, config=None, **options):
    """Search by a description or typed identifier without requiring a result table.

    Newly encountered identifiers are returned as lookup seeds only when an
    affirmative, non-OCR, unambiguous bridge connects them to a target ID.
    """
    from lineage_config import validate_config
    config = validate_config(config)
    if isinstance(target, str):
        target_text = target.strip()
        if not target_text:
            raise ValueError('target must not be empty')
        parsed = extract_document_entities(target_text, entity_aliases=config.get('entity_aliases'),
                                           scope_aliases=config.get('scope_aliases'))
        identifiers = [{'value': m['raw_value'], 'namespace': m['identifier_namespace'], 'scope': m['scope']}
                       for m in parsed['mentions'] if m['can_seed_lookup']]
        if not identifiers and re.fullmatch(r'[A-Za-z0-9_.:/#-]{3,160}', target_text) and re.search(r'\d', target_text):
            identifiers = [{'value': target_text, 'namespace': 'unspecified'}]
        query = {'description': target_text, 'identifiers': identifiers}
        if not identifiers and len(target_text) <= 160:
            query['terms'] = [target_text]
    elif isinstance(target, dict):
        query = dict(target)
    else:
        raise ValueError('target must be a string or query object')
    parameters = dict(config.get('evidence', {}))
    allowed = {'max_source_bytes', 'max_units', 'max_terms', 'max_candidate_chars', 'max_document_hops', 'max_lookup_seeds'}
    if set(parameters) - allowed:
        raise ValueError('Unknown evidence configuration: ' + ', '.join(sorted(set(parameters) - allowed)))
    parameters['source_options'] = config.get('sources', {})
    parameters.update(options)
    sources = list(sources)
    report = retrieve_evidence(query, sources, cache_dir=cache_dir, limit=limit, progress=progress, config=config, **parameters)
    seeds = build_lookup_seeds(query, report['evidence'], config=config)
    all_mentions = [mention for evidence in report['evidence'] for mention in evidence.get('entity_mentions', [])]
    all_bridges = [bridge for evidence in report['evidence'] for bridge in evidence.get('bridge_candidates', [])]
    report.update(analysis_stage='evidence_search', target=query, query=query,
                  related_evidence=report['evidence'], entity_mentions=all_mentions, bridge_candidates=all_bridges,
                  lookup_seeds=seeds, source_localization={
                      'status': 'passages_found' if report['evidence'] else 'no_passages_found',
                      'passages': len(report['evidence']), 'lookup_seeds': len(seeds)},
                  historical_lineage='not_proven',
                  status='evidence_found' if report['evidence'] else 'no_matches')
    if any(seed.get('closure_truncated') for seed in seeds):
        report['coverage']['truncated'] = True
        report['issues'].append({'code': 'lookup_seed_closure_budget', 'severity': 'warning',
                                  'message': '实体查询线索闭包达到预算；未声明后续跳数完整'})
    # The shared structural engine applies the same exact namespace/scope join
    # policy used for result tables. A document claim remains a candidate cause.
    from trace_pipeline import trace_seed_sources, TABULAR
    structured = [source for source in sources if Path(source).suffix.lower() in TABULAR]
    query_seeds = []
    for anchor in _target_anchors(query, config):
        if anchor['identifier_namespace'] == 'unspecified' or anchor['scope'].get('ambiguous'):
            continue
        query_id = 'query_' + hashlib.sha256(json.dumps(anchor, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:20]
        query_seeds.append({**anchor, 'anchor': anchor, 'source_location': {'kind': 'user_query', 'query': query.get('description')},
                            'evidence_id': query_id, 'bridge_id': None, 'can_seed_lookup': True,
                            'verification': 'explicit_target_identifier_not_source_evidence',
                            'provenance': [{'kind': 'user_query', 'evidence_id': query_id,
                                            'identifier_namespace': anchor['identifier_namespace'], 'value': anchor['value']}]})
    report['query_seeds'] = query_seeds
    all_seeds = query_seeds + seeds
    budget = config.get('evidence', {}).get('max_lookup_seeds', 2048)
    if len(all_seeds) > budget:
        report['coverage']['truncated'] = True
        report['issues'].append({'code': 'lookup_query_seed_budget', 'severity': 'warning', 'omitted_seeds': len(all_seeds) - budget})
        all_seeds = all_seeds[:budget]
    if all_seeds and structured:
        traced = trace_seed_sources(all_seeds, structured, cache_dir=cache_dir or '.dd_cache', progress=progress, config=config)
        _mark_located_records(report['evidence'], traced)
        present_refs = {(item.get('record_localization') or {}).get('source_ref') for item in report['evidence']}
        found_records = [item for item in evidence_from_located_records(traced.get('result_rows', []), config=config)
                         if item.get('source_ref') not in present_refs]
        record_evidence = found_records[:max(0, limit - len(report['evidence']))]
        if len(record_evidence) < len(found_records):
            report['coverage']['truncated'] = True
            report['issues'].append({'code': 'located_record_evidence_limit', 'severity': 'warning',
                                    'omitted_records': len(found_records) - len(record_evidence)})
        report['evidence'].extend(record_evidence)
        report['coverage']['structural_record_evidence'] = len(record_evidence)
        report['coverage']['returned_units'] = len(report['evidence'])
        report['coverage']['matched_units'] = max(report['coverage']['matched_units'], len(report['evidence']))
        report['entity_mentions'] = [mention for item in report['evidence'] for mention in item.get('entity_mentions', [])]
        report['bridge_candidates'] = [bridge for item in report['evidence'] for bridge in item.get('bridge_candidates', [])]
        located_seeds = build_lookup_seeds(query, report['evidence'], config=config)
        existing = {(seed['identifier_namespace'], seed['value'], json.dumps(seed['scope'], sort_keys=True)) for seed in all_seeds}
        additional = [seed for seed in located_seeds if
                      (seed['identifier_namespace'], seed['value'], json.dumps(seed['scope'], sort_keys=True)) not in existing]
        if additional:
            report['lookup_seeds'] = located_seeds
            permitted = additional[:max(0, budget - len(all_seeds))]
            if permitted:
                traced = trace_seed_sources(all_seeds + permitted, structured, cache_dir=cache_dir or '.dd_cache', progress=progress, config=config)
            if len(permitted) < len(additional):
                report['coverage']['truncated'] = True
                report['issues'].append({'code': 'located_record_bridge_budget', 'severity': 'warning'})
        report['source_trace'] = traced
        for field in ('result_rows', 'source_records', 'source_profiles', 'field_mappings', 'lineage_dependencies'):
            if field in traced:
                report[field] = traced[field]
        report['coverage']['source_trace'] = traced['coverage']
        report['source_localization'] = traced['source_localization']
        report['issues'].extend(traced['issues'])
    return report
