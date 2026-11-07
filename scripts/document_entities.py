"""Typed, span-addressed mentions and conservative document lookup bridges.

Mentions are source claims. A shared value is never an identity decision and a
co-occurrence is never a verified business relationship. No model service or
document-provided instruction is executed.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import combinations, islice, product
from functools import lru_cache
import json
import re
import unicodedata


_ALIASES = {
    'order': ('order id', 'order no', 'order number', 'purchase order', 'po number', 'po no', '订单编号', '订单号码', '订单号', '订单ID', '采购单号', '采购订单号', 'order', '订单'),
    'contract': ('contract id', 'contract no', 'contract number', 'agreement id', 'agreement no', '合同编号', '合同号码', '合同号', '合同ID', '协议编号', 'contract', '合同'),
    'device': ('device id', 'device no', 'equipment id', 'equipment no', 'serial number', 'serial no', '设备编号', '设备号', '设备ID', '设备序列号', '仪器编号', '序列号', 'device'),
    'sample': ('sample id', 'sample no', 'sample number', 'specimen id', 'specimen no', '样本编号', '样本号', '样品编号', '样品号', '检材编号', 'sample', 'specimen'),
    'customer': ('customer id', 'customer no', 'customer number', 'client id', 'client no', '客户编号', '客户号', '客户ID'),
    'supplier': ('supplier id', 'supplier no', 'vendor id', 'vendor no', '供应商编号', '供应商ID'),
    'invoice': ('invoice id', 'invoice no', 'invoice number', '发票编号', '发票号码', '发票号', '票据号', 'invoice'),
    'shipment': ('shipment id', 'shipment no', 'tracking number', 'tracking no', '运单编号', '运单号码', '运单号', '物流单号'),
    'batch': ('batch id', 'batch no', 'batch number', 'lot id', 'lot no', '批次编号', '批次号', '批号'),
    'product': ('product id', 'product code', 'sku', 'item code', '产品编号', '产品编码', '商品编号', '商品编码', '物料编号', '物料编码'),
    'person': ('person id', 'employee id', 'employee no', '人员编号', '员工编号', '员工号', '工号'),
    'visit': ('visit id', 'encounter id', '就诊ID', '就诊编号', '就诊流水号'),
    'settlement': ('settlement id', 'settlement no', '结算ID', '结算编号', '结算流水号'),
    'payment': ('payment id', 'payment no', 'transaction id', 'transaction no', '支付编号', '交易编号', '交易流水号', '回款ID'),
    'rental': ('rental id', 'lease id', 'lease number', 'rental', 'lease', '租约', '租赁编号', '租约编号'),
    'delivery': ('delivery id', 'delivery no', 'delivery note', '送货单', '送货单号', '交货单号'),
    'receipt': ('receipt id', 'receipt no', 'receipt number', 'receipt', '收据', '收据号', '收款单'),
    'publication': ('publication id', 'publication', 'paper id', '论文', '论文编号', '出版物编号'),
    'experiment': ('experiment id', 'experiment no', 'experiment', '实验', '实验编号', '实验号'),
    'production': ('production id', 'production no', '生产单', '生产单号', '生产编号'),
    'ticket': ('ticket id', 'ticket no', 'ticket number', 'ticket', '票号', '票据编号'),
    'lab': ('lab id', 'laboratory id', 'laboratory', '实验室', '实验室编号'),
    'carrier': ('carrier id', 'carrier', '承运方', '承运方编号'),
}
_ALIASES['payment'] += ('payment', '付款单', '支付单', '付款单号')
_ALIASES['batch'] += ('batch', '批次')
_ALIASES['invoice'] += ('发票',)
_SCOPE_ALIASES = {
    'tenant': ('tenant', 'tenant id', 'tenant_id', '租户', '租户ID', '租户编号'),
    'system': ('system', 'source system', 'source_system', '系统', '来源系统'),
    'project': ('project', 'project id', 'project_id', '项目编号', '项目ID'),
    'namespace': ('namespace', '命名空间'),
    'period': ('period', 'year', 'fiscal_year', '年度', '年份', '会计年度'),
    'revision': ('revision', 'version', '版本', '版本号'),
    'site': ('site', 'plant', '站点', '工厂编号', '机构编号'),
    'lab': ('lab', 'lab id', 'lab_id', 'laboratory', '实验室', '实验室编号'),
    'carrier': ('carrier', 'carrier id', 'carrier_id', '承运方', '承运方编号'),
}


def _field_key(value):
    return re.sub(r'[\s_\-.:：]+', '', unicodedata.normalize('NFKC', str(value))).casefold()


_FIELD_NAMESPACES = {_field_key(alias): kind for kind, aliases in _ALIASES.items() for alias in aliases}
_SCOPE_FIELDS = {_field_key(alias): kind for kind, aliases in _SCOPE_ALIASES.items() for alias in aliases}
_CLAUSE_BOUNDARY = re.compile(r'[\r\n;；。！？!?]|(?<!\bNo)\.(?=\s|$)', re.I)
_PARAGRAPH_BOUNDARY = re.compile(r'\r?\n[ \t]*\r?\n')
_NEGATED = re.compile(r'不属于|未发现|未找到|并非|不是|否认|排除|无关|未使用|未关联|未签署|不对应|不得|禁止|'
                      r'没有[^。；;\n]{0,12}(?:关系|对应|关联)|属于另|已撤回|撤销|作废|'
                      r'\b(?:not|never|denied|denies|exclude[ds]?|ruled out|retracted|withdrawn|unrelated|without|no evidence|belongs? to another)\b', re.I)
_REPORTED = re.compile(r'据称|据.+?(?:称|反映)|转述|声称|传闻|听说|\b(?:according to|reportedly|allegedly|claims? that|said that)\b', re.I)
_UNCERTAIN = re.compile(r'可能|疑似|待核实|待确认|待答|初稿|草稿|尚不确定|假设|示例|例如|是否|吗[”"）)]?$|[?？]|'
                        r'\b(?:possibly|possible|maybe|might|uncertain|unconfirmed|draft|hypothetical|example|question)\b', re.I)
_RELATION = re.compile(r'对应|关联|引用|属于|映射|连接|包含|使用|来自|\b(?:references?|maps? to|links? to|belongs? to|uses?|under|for)\b', re.I)
_DATE = re.compile(r'(?<!\d)\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?(?!\d)')
_MEASURE = re.compile(r'(?<![A-Za-z0-9_.])(?P<value>[+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?P<unit>亿元|万元|CNY|USD|EUR|元|kg|mg|μg|ug|g|mL|ml|L|mm|cm|km|m|℃|°C|kWh|kW|MPa|kPa|%)(?![A-Za-z])')
_NAME = re.compile(r'(?P<label>供应商名称|公司名称|机构名称|组织名称|客户名称|客户姓名|人员姓名|姓名|supplier name|company name|organization name|customer name|person name)\s*[:：=]\s*(?P<value>[^\r\n;；,，。]{2,100})', re.I)
_UNSET = object()


def _alias_key(aliases):
    if aliases is None:
        return ()
    if not isinstance(aliases, dict):
        raise ValueError('Aliases must map namespaces to literal label lists')
    normalized = []
    for namespace, labels in aliases.items():
        if not isinstance(namespace, str) or not namespace.strip() or not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
            raise ValueError('Each alias namespace requires a nonempty literal label list')
        normalized.append((namespace, tuple(labels)))
    return tuple(sorted(normalized))


@lru_cache(maxsize=64)
def _identifier_patterns(alias_key):
    aliases = {namespace: list(labels) for namespace, labels in _ALIASES.items()}
    for namespace, labels in alias_key:
        aliases.setdefault(namespace, []).extend(labels)
    labels, fields = {}, {}
    for namespace, names in aliases.items():
        for label in names:
            key = _field_key(label)
            if key in fields and fields[key] != namespace:
                raise ValueError('Identifier label maps to multiple namespaces: ' + label)
            fields[key] = namespace
            labels[label.casefold()] = namespace
    alternatives = '|'.join(re.escape(label).replace(r'\ ', r'[ _-]*') for label in sorted(labels, key=len, reverse=True))
    pattern = re.compile(r'(?<![A-Za-z0-9_])(?P<label>' + alternatives +
        r')(?![A-Za-z])\.?\s*(?:[:：=#]\s*|\b(?:is|was|numbered)\s+|(?:是|为|编号为|编号是)\s*)?'
        r'["“\[（(]?\s*(?P<value>[A-Za-z0-9][A-Za-z0-9_.:/#-]{1,159})(?![A-Za-z0-9_])', re.I)
    return pattern, fields


@lru_cache(maxsize=64)
def _scope_patterns(alias_key):
    labels, fields = {}, {}
    for namespace, names in list(_SCOPE_ALIASES.items()) + list(alias_key):
        for label in names:
            key = _field_key(label)
            if key in fields and fields[key] != namespace:
                raise ValueError('Scope label maps to multiple namespaces: ' + label)
            fields[key], labels[label.casefold()] = namespace, namespace
    alternatives = '|'.join(re.escape(label).replace(r'\ ', r'[ _-]*') for label in sorted(labels, key=len, reverse=True))
    pattern = re.compile(r'(?<![A-Za-z0-9_])(?P<label>' + alternatives +
        r')\s*(?:[:：=]\s*|\b(?:is|was)\s+|(?:是|为)\s*|[ \t]+)'
        r'(?P<value>[A-Za-z0-9\u3400-\u9fff][A-Za-z0-9_./\u3400-\u9fff-]{0,79})(?![A-Za-z0-9_])', re.I)
    return pattern, fields


def identifier_namespace(field, aliases=None, *, config=None):
    """Return a typed identifier namespace, retaining unknown field namespaces."""
    if config is not None:
        aliases = config.get('entity_aliases')
    elif isinstance(aliases, dict) and any(key in aliases for key in ('entity_aliases', 'scope_aliases', 'sources', 'evidence')):
        aliases = aliases.get('entity_aliases')
    key = _field_key(field)
    if key in set(_ALIASES) | {'specimen', '订单', '合同'}:
        return None  # Bare prose labels are not sufficient schema declarations.
    configured = {_field_key(label): namespace for namespace, labels in _alias_key(aliases) for label in labels}
    if key in configured:
        return configured[key]
    if key in _SCOPE_FIELDS and key not in {_field_key(label) for namespace in ('lab', 'carrier') for label in _ALIASES[namespace]}:
        return None
    if key in _FIELD_NAMESPACES:
        return _FIELD_NAMESPACES[key]
    # Compact English column names (orderID, customer_number) and Chinese keys.
    for kind, aliases in _ALIASES.items():
        if key in {kind + suffix for suffix in ('id', 'no', 'number', 'code')}:
            return kind
    if key in ('id', 'no', 'number', 'code', 'key', '编号', '序号', '代码'):
        return 'unspecified'
    spelled = re.sub(r'([a-z])([A-Z])', r'\1_\2', unicodedata.normalize('NFKC', str(field))).casefold()
    if (re.search(r'(?:^|[\s_./-])(?:id|uuid|guid|number|code)$', spelled)
            or re.search(r'(?:编号|编码|流水号|单号|[\u3400-\u9fff](?:id|no))$', spelled)):
        return 'field:' + key
    return None


def scope_namespace(field, config=None, *, aliases=None):
    """Map a declared scope field to its namespace; never guess entity equality."""
    if config is not None:
        aliases = config.get('scope_aliases')
    _, fields = _scope_patterns(_alias_key(aliases))
    return fields.get(_field_key(field))


def normalize_date(text):
    normalized = text.replace('年', '-').replace('月', '-').replace('日', '').replace('/', '-').rstrip('-')
    match = re.fullmatch(r'(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(.*)', normalized)
    if not match:
        return None
    year, month, day, clock = match.groups()
    try:
        if day is None:
            return f'{year}-{int(month):02d}' if not clock and 1 <= int(month) <= 12 else None
        value = date(int(year), int(month), int(day)).isoformat()
        return datetime.fromisoformat(value + 'T' + clock.strip().lstrip('T')).isoformat() if clock.strip() else value
    except ValueError:
        return None


def _hash_id(prefix, value):
    return prefix + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()[:20]


def _assertion(context):
    cues = []
    for label, pattern in (('negated', _NEGATED), ('reported', _REPORTED), ('uncertain', _UNCERTAIN)):
        found = list(pattern.finditer(context))
        if found:
            cues.extend({'type': label, 'text': m.group(), 'start_in_context': m.start(), 'end_in_context': m.end()} for m in found)
    status = next((label for label in ('negated', 'reported', 'uncertain') if any(c['type'] == label for c in cues)), 'affirmed')
    return {'status': status, 'cues': cues, 'semantics': 'source_statement_polarity_not_truth'}


def _clause_boundaries(text, identifier_pattern):
    """Split comma clauses only when both sides state complete ID relations."""
    hard = [m.start() for m in _CLAUSE_BOUNDARY.finditer(text)]
    result = set(hard)
    boundaries = [-1] + hard + [len(text)]
    for begin, end in zip(boundaries, boundaries[1:]):
        fragment = text[begin + 1:end]
        commas = [m.start() + begin + 1 for m in re.finditer('[,，]', fragment)]
        if not commas:
            continue
        ids = [m.start() + begin + 1 for m in identifier_pattern.finditer(fragment)]
        relations = [m.start() + begin + 1 for m in _RELATION.finditer(fragment)]
        previous = begin
        for comma in commas:
            if (bisect_right(ids, comma) - bisect_right(ids, previous) >= 2
                    and len(ids) - bisect_right(ids, comma) >= 2
                    and bisect_right(relations, comma) > bisect_right(relations, previous)
                    and len(relations) > bisect_right(relations, comma)):
                result.add(comma)
                previous = comma
    return [-1] + sorted(result) + [len(text)]


def _scope(text, scope_aliases=None):
    values = defaultdict(set)
    pattern, fields = _scope_patterns(_alias_key(scope_aliases))
    for match in pattern.finditer(text):
        kind = fields.get(_field_key(match['label']))
        values[kind].add(match['value'])
    return {'values': {key: next(iter(items)) for key, items in values.items() if len(items) == 1},
            'ambiguous': {key: sorted(items) for key, items in values.items() if len(items) > 1}}


def _structured_scope(fields, scope_aliases=None):
    values = defaultdict(set)
    _, scope_fields = _scope_patterns(_alias_key(scope_aliases))
    for field, value in fields.items():
        key = scope_fields.get(_field_key(field))
        if key and value is not None:
            values[key].add(str(value).strip())
    return {'values': {key: next(iter(items)) for key, items in values.items() if len(items) == 1},
            'ambiguous': {key: sorted(items) for key, items in values.items() if len(items) > 1}}


def _selector(text, begin, end, location):
    base = location.get('char_offset')
    return {'type': 'TextPositionSelector', 'start': begin, 'end': end,
            'offset_scope': 'unit_text',
            'source_start': base + begin if type(base) is int else None,
            'source_end': base + end if type(base) is int else None,
            'quote': {'type': 'TextQuoteSelector', 'exact': text[begin:end],
                      'prefix': text[max(0, begin - 32):begin], 'suffix': text[end:end + 32]}}


def extract_document_entities(text, *, location=None, source_sha256=None, fields=None,
                              ocr_confidence=None, max_mentions=5000, max_bridges=1000,
                              entity_aliases=None, scope_aliases=None):
    """Extract typed mentions and bounded lookup bridges from one source unit.

    Pass ``fields`` only for an actual parsed record, never an inferred prose
    table. ``location`` carries original file/row/page/paragraph/JSON pointers.
    """
    if not isinstance(text, str):
        raise ValueError('text must be a string')
    if type(max_mentions) is not int or max_mentions < 1 or type(max_bridges) is not int or max_bridges < 1:
        raise ValueError('Mention/bridge budgets must be positive integers')
    if fields is not None and not isinstance(fields, dict):
        raise ValueError('fields must be a parsed field mapping')
    location = dict(location or {})
    identifier_pattern, identifier_fields = _identifier_patterns(_alias_key(entity_aliases))
    scope_pattern, scope_fields = _scope_patterns(_alias_key(scope_aliases))
    metadata = location.get('metadata') or {}
    citation = {key: value for key, value in location.items() if key not in ('cells', 'field_pointers', 'metadata')}
    if metadata:
        citation['metadata'] = {key: value for key, value in metadata.items()
                                if key not in ('cells', 'field_pointers', 'raw_record', 'value_types')}
    ocr = ocr_confidence is not None or location.get('ocr_confidence') is not None or bool(metadata.get('bbox')) or 'ocr' in str(metadata.get('extraction', '')).lower()
    document_key = source_sha256 or location.get('file') or _hash_id('unlocated_', text)
    unit_key = _hash_id('unit_', [document_key, citation])
    boundaries = _clause_boundaries(text, identifier_pattern)
    paragraphs = [-1] + [m.end() - 1 for m in _PARAGRAPH_BOUNDARY.finditer(text)] + [len(text)]
    lines = [-1] + [m.start() for m in re.finditer('\n', text)] + [len(text)]
    scope_declarations = defaultdict(list)
    for match in scope_pattern.finditer(text):
        scope_declarations[scope_fields[_field_key(match['label'])]].append((match.start(), match['value']))
    scope_positions = {key: [position for position, _ in declarations] for key, declarations in scope_declarations.items()}
    mentions, issues, omitted = [], [], 0
    seen = set()
    scope_cache, assertion_cache = {}, {}
    record_scope = _structured_scope(fields, scope_aliases) if fields is not None else None

    def append(kind, namespace, value, begin, end, label, *, field=None, normalization='literal', normalized=_UNSET):
        nonlocal omitted
        if len(mentions) >= max_mentions:
            omitted += 1
            return
        key = (kind, namespace, begin, end, field)
        if key in seen:
            return
        seen.add(key)
        index = max(0, bisect_right(boundaries, begin) - 1)
        clause_begin, clause_end = boundaries[index] + 1, boundaries[min(index + 1, len(boundaries) - 1)]
        pindex = max(0, bisect_right(paragraphs, begin) - 1)
        para_begin, para_end = paragraphs[pindex] + 1, paragraphs[min(pindex + 1, len(paragraphs) - 1)]
        context = text[clause_begin:clause_end]
        line_index = max(0, bisect_right(lines, begin) - 1)
        if fields is None and line_index not in scope_cache:
            line_begin, line_end = lines[line_index] + 1, lines[min(line_index + 1, len(lines) - 1)]
            local_scope = _scope(text[line_begin:line_end], scope_aliases)
            # Explicit scope headers apply down to the next declaration inside
            # the paragraph. Two tenants stated on the same line stay ambiguous.
            for key, declarations in scope_declarations.items():
                if key in local_scope['values'] or key in local_scope['ambiguous']:
                    continue
                position = bisect_right(scope_positions[key], line_begin - 1) - 1
                if position >= 0 and declarations[position][0] >= para_begin:
                    declared_at, scope_value = declarations[position]
                    header_line = max(0, bisect_right(lines, declared_at) - 1)
                    header_scope = _scope(text[lines[header_line] + 1:lines[header_line + 1]], scope_aliases)
                    if key in header_scope['ambiguous']:
                        local_scope['ambiguous'][key] = header_scope['ambiguous'][key]
                    else:
                        local_scope['values'][key] = scope_value
            scope_cache[line_index] = local_scope
        scope = record_scope if fields is not None else scope_cache[line_index]
        if index not in assertion_cache:
            assertion_cache[index] = (_assertion(context + text[clause_end:clause_end + 1]) if fields is None else
                                      {'status': 'affirmed', 'cues': [], 'semantics': 'record_field_claim_not_truth'})
        assertion = assertion_cache[index]
        source_location = dict(citation)
        selector = _selector(text, begin, end, location) if field is None else None
        if field is not None:
            source_location['field'] = field
            cell = location.get('cells', {}).get(field)
            if cell:
                source_location['cell'] = cell.get('address') if isinstance(cell, dict) else cell
            pointer = location.get('field_pointers', metadata.get('field_pointers', {})).get(field)
            if pointer is not None:
                source_location['json_pointer'] = pointer
        reference = {'source': document_key, 'location': source_location, 'selector': selector,
                     'type': kind, 'namespace': namespace, 'value': value}
        mention = {'mention_id': _hash_id('mention_', reference), 'entity_type': kind,
                   'identifier_namespace': namespace, 'label': label, 'raw_value': value,
                   'normalized_value': value if normalized is _UNSET else normalized,
                   'normalization': normalization, 'source_location': source_location,
                   'source_sha256': source_sha256, 'selector': selector,
                   'assertion': assertion, 'scope': scope,
                   'context': {'start': clause_begin, 'end': clause_end, 'text': context[:1200],
                               'truncated': len(context) > 1200, 'unit': 'record' if fields is not None else 'clause',
                               'context_id': _hash_id('context_', [unit_key, None if fields is not None else clause_begin])},
                   'ocr': {'derived': ocr, 'confidence': ocr_confidence if ocr_confidence is not None else location.get('ocr_confidence'),
                           'review_required': ocr},
                   'verification': 'source_claim_unverified', 'identity_verified': False}
        mention['can_seed_lookup'] = (kind == 'identifier' and namespace != 'unspecified' and
                                      assertion['status'] == 'affirmed' and not scope['ambiguous'] and not ocr)
        mentions.append(mention)

    if fields is not None:
        narrative_fields = []
        for field, raw in fields.items():
            if raw is None or isinstance(raw, (dict, list, bool)):
                continue
            value = str(raw).strip()
            if not value:
                continue
            namespace = identifier_namespace(field, entity_aliases)
            if namespace:
                append('identifier', namespace, value, 0, 0, field, field=field)
            elif re.search(r'日期|时间|date|time', field, re.I):
                normalized = normalize_date(value)
                append('date', None, value, 0, 0, field, field=field,
                       normalization='calendar_validated' if normalized else 'unresolved_calendar', normalized=normalized)
            elif re.search(r'金额|数量|重量|温度|压力|amount|quantity|weight|temperature|pressure', field, re.I):
                append('measure', None, value, 0, 0, field, field=field)
            elif re.search(r'姓名|名称|name', field, re.I):
                append('name', None, value, 0, 0, field, field=field)
            elif re.search(r'备注|说明|内容|描述|comment|description|note|text|content|body|remark', field, re.I):
                narrative_fields.append((field, value))
        for field, value in narrative_fields:
            if len(mentions) >= max_mentions:
                omitted += 1
                continue
            narrative_location = dict(citation, field=field, char_offset=None, offset_scope='decoded_field_value')
            pointer = location.get('field_pointers', metadata.get('field_pointers', {})).get(field)
            if pointer is not None:
                narrative_location['json_pointer'] = pointer
            cell = location.get('cells', {}).get(field)
            if cell:
                narrative_location['cell'] = cell.get('address') if isinstance(cell, dict) else cell
            nested = extract_document_entities(value, location=narrative_location, source_sha256=source_sha256,
                                               ocr_confidence=ocr_confidence,
                                               max_mentions=max_mentions - len(mentions), max_bridges=max_bridges,
                                               entity_aliases=entity_aliases, scope_aliases=scope_aliases)
            for mention in nested['mentions']:
                if mention['selector']:
                    mention['selector']['offset_scope'] = 'decoded_field_value'
                # A note is a record attribute, while its own assertion cues and
                # text selector remain explicit and may block any lookup bridge.
                mention['claim_context'] = mention['context']
                mention['context'] = {'start': 0, 'end': len(text), 'text': text[:1200],
                                      'truncated': len(text) > 1200, 'unit': 'record',
                                      'context_id': _hash_id('context_', [unit_key, None])}
                explicit_scope = mention['scope']
                merged = dict(record_scope['values'])
                ambiguous = dict(record_scope['ambiguous'])
                for key, item in explicit_scope['values'].items():
                    if key in merged and merged[key] != item:
                        ambiguous[key] = [merged[key], item]
                    else:
                        merged[key] = item
                ambiguous.update(explicit_scope['ambiguous'])
                mention['scope'] = {'values': merged, 'ambiguous': ambiguous}
                mention['can_seed_lookup'] &= not bool(ambiguous)
                mentions.append(mention)
            omitted += nested['coverage']['omitted_mentions']
    else:
        identifier_spans = []
        for match in identifier_pattern.finditer(text):
            label = re.sub(r'[ _-]+', ' ', match['label']).casefold()
            namespace = identifier_fields.get(_field_key(label))
            value = match['value'].rstrip('.:/#-')
            strong_label = bool(re.search(r'id|no\b|number|code|号|编号|编码|序列', label, re.I))
            declared_scope_id = scope_fields.get(_field_key(label)) == namespace and bool(re.fullmatch(r'[A-Z][A-Z0-9_.:/#-]+', value))
            if not namespace or not value or (not strong_label and not declared_scope_id and not re.search(r'\d', value)):
                continue
            begin, end = match.start('value'), match.start('value') + len(value)
            append('identifier', namespace, value, begin, end, match['label'])
            identifier_spans.append((begin, end))
        # A later literal repetition can carry a retraction without repeating its
        # type label. Resolve only an already explicit, unambiguous type in this
        # unit; retain the new clause polarity and original typing reference.
        typed_literals = defaultdict(list)
        for mention in mentions:
            if mention['entity_type'] == 'identifier' and not mention['raw_value'].isdigit():
                typed_literals[mention['raw_value']].append(mention)
        occupied = set(identifier_spans)
        unambiguous_literals = {literal: typed[0] for literal, typed in typed_literals.items()
                                if len({m['identifier_namespace'] for m in typed}) == 1}
        for repeated in re.finditer(r'(?<![A-Za-z0-9_])[A-Za-z0-9][A-Za-z0-9_.:/#-]{1,159}(?![A-Za-z0-9_])', text):
            literal = repeated.group().rstrip('.:/#-')
            typed = unambiguous_literals.get(literal)
            span = (repeated.start(), repeated.start() + len(literal))
            if typed is None or span in occupied:
                continue
            count = len(mentions)
            append('identifier', typed['identifier_namespace'], literal, *span, 'same_unit_typed_literal_reference')
            if len(mentions) > count:
                mentions[-1]['typing_reference'] = typed['mention_id']
            occupied.add(span)
            identifier_spans.append(span)
        for match in _DATE.finditer(text):
            if any(match.start() < b and match.end() > a for a, b in identifier_spans):
                continue
            normalized = normalize_date(match.group())
            append('date', None, match.group(), match.start(), match.end(), 'date',
                   normalization='calendar_validated' if normalized else 'unresolved_calendar', normalized=normalized)
        for match in _MEASURE.finditer(text):
            if any(match.start() < b and match.end() > a for a, b in identifier_spans):
                continue
            value = match.group()
            normalized = {'value': str(Decimal(match['value'].replace(',', ''))), 'unit': match['unit']}
            append('measure', None, value, match.start(), match.end(), 'measure', normalization='decimal_token_and_explicit_unit', normalized=normalized)
        for match in _NAME.finditer(text):
            append('name', None, match['value'].strip(), match.start('value'), match.end('value'), match['label'])

    groups = defaultdict(list)
    for mention in mentions:
        if mention['entity_type'] == 'identifier':
            groups[mention['context']['context_id']].append(mention)
    bridges, bridge_omitted = [], 0
    for grouped in groups.values():
        namespaces = Counter(m['identifier_namespace'] for m in grouped)
        possible = (len(grouped) ** 2 - sum(count ** 2 for count in namespaces.values())) // 2
        room = max_bridges - len(bridges)
        bridge_omitted += max(0, possible - room)
        if not possible or room <= 0:
            continue
        by_namespace = defaultdict(list)
        for mention in grouped:
            by_namespace[mention['identifier_namespace']].append(mention)
        pairs = (pair for lefts, rights in combinations(by_namespace.values(), 2) for pair in product(lefts, rights))
        for left, right in islice(pairs, room):
                context = left['context']
                relation = _RELATION.search(context['text'])
                unambiguous = namespaces[left['identifier_namespace']] == namespaces[right['identifier_namespace']] == 1
                bridge = {'bridge_id': _hash_id('bridge_', [left['mention_id'], right['mention_id']]),
                          'from_mention': left['mention_id'], 'to_mention': right['mention_id'],
                          'kind': 'record_cooccurrence' if fields is not None else 'explicit_relation_claim' if relation else 'clause_cooccurrence',
                          'relation_text': relation.group() if relation else None, 'source_location': citation,
                          'source_sha256': source_sha256, 'context': context,
                          'can_seed_lookup': left['can_seed_lookup'] and right['can_seed_lookup'] and unambiguous and not context['truncated'],
                          'reason': 'multiple_identifiers_in_same_namespace' if not unambiguous else 'source_claim_requires_verification',
                          'relationship_verified': False, 'identity_merge_allowed': False}
                bridges.append(bridge)
    if omitted:
        issues.append({'code': 'entity_mention_budget', 'severity': 'warning', 'omitted_mentions': omitted})
    if bridge_omitted:
        issues.append({'code': 'entity_bridge_budget', 'severity': 'warning', 'omitted_bridges': bridge_omitted})
    return {'schema_version': 'document-entities-1', 'mentions': mentions, 'bridges': bridges,
            'coverage': {'text_chars': len(text), 'mentions': len(mentions), 'bridges': len(bridges),
                         'complete': not omitted and not bridge_omitted, 'omitted_mentions': omitted,
                         'omitted_bridges': bridge_omitted, 'supported_scope': 'explicit_labels_dates_units_not_general_ner'},
            'issues': issues}
