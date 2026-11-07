"""
entity_extractor.py — 实体提取器
从文本和结构化数据中提取：人名、公司名、金额、日期、地点、ID、电话
为数据溯源提供带出处的字段角色、标识提及和候选连接。
"""
import re
import json
from decimal import Decimal
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from matcher import is_identifier_field
from document_entities import extract_document_entities


@dataclass
class Entity:
    entity_id: str
    name: str
    entity_type: str          # person / org / amount / date / location / id / phone / product
    value: Any                # 规范化值
    raw_text: str             # 原始文本
    source_file: str
    row_num: Optional[int]
    page_num: Optional[int]
    paragraph_num: Optional[int]
    char_offset: Optional[int]
    confidence: Optional[float]
    attributes: Dict[str, Any] = field(default_factory=dict)
    mentions: List[Dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "type": self.entity_type,
            "value": str(self.value) if self.value is not None else None,
            "raw_text": self.raw_text,
            "source_file": self.source_file,
            "row_num": self.row_num,
            "page_num": self.page_num,
            "paragraph_num": self.paragraph_num,
            "char_offset": self.char_offset,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "confidence_semantics": "legacy_extraction_heuristic_not_identity_probability" if self.confidence is not None else "not_assessed",
            "attributes": json.loads(json.dumps(self.attributes, ensure_ascii=False, default=str)),
            "mentions": self.mentions,
        }


@dataclass
class Relationship:
    from_entity: str          # entity_id
    to_entity: str
    relation_type: str        # 下单/属于/关联/授权/支付/签署/管理/...
    confidence: Optional[float]
    evidence: str
    source_file: str
    source_location: str

    def to_dict(self):
        return {
            "from": self.from_entity,
            "to": self.to_entity,
            "relation_type": self.relation_type,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "evidence": self.evidence,
            "source": self.source_location,
            "relationship_stage": "row_role_candidate",
            "verification": "unverified",
        }


@dataclass
class TimelineEvent:
    time: str                  # ISO格式或原始时间文本
    time_normalized: Optional[str]
    event: str                 # 事件描述
    entities: List[str]        # 相关实体ID或名称
    source_file: str
    source_location: str
    confidence: float
    event_type: str = "generic"  # order/payment/sign/anomaly/generic

    def to_dict(self):
        return {
            "time": self.time,
            "time_normalized": self.time_normalized,
            "event": self.event,
            "entities": self.entities,
            "source": self.source_location,
            "confidence": round(self.confidence, 4),
            "event_type": self.event_type,
        }


# ─────────────────────────────────────────────
# 正则模式库
# ─────────────────────────────────────────────

# 日期模式
DATE_PATTERNS = [
    (r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?', 'full_date', 0.95),
    (r'\d{4}[-/年]\d{1,2}[月]?', 'year_month', 0.85),
    (r'\d{1,2}[-/月]\d{1,2}[日]?(?:\s*\d{4})?', 'month_day', 0.75),
]

# 金额模式
AMOUNT_PATTERNS = [
    (r'[¥￥$€]\s*-?[\d,]+(?:\.\d+)?(?:\s*(?:亿元|万元|元|USD|CNY|EUR|亿|万))?', 'currency', 0.90),
    (r'(?<![A-Za-z\d])-?[\d,]+(?:\.\d+)?\s*(?:亿元|万元|元|USD|CNY|EUR|亿|万)', 'amount_cn', 0.85),
]

# ID 模式
ID_PATTERNS = [
    (r'(?<![A-Za-z0-9])[A-Z]{1,4}\d{4,12}(?![A-Za-z0-9])', 'business_id', 0.90),
    (r'(?<![A-Za-z0-9])\d{6,18}(?![A-Za-z0-9])', 'numeric_id', 0.70),
    (r'(?<![A-Za-z0-9])(?=[A-Z0-9]{8,20}(?![A-Za-z0-9]))(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{8,20}(?![A-Za-z0-9])', 'alphanumeric_id', 0.65),
]

# 电话
PHONE_PATTERNS = [
    (r'1[3-9]\d{9}', 'mobile_cn', 0.95),
    (r'0\d{2,3}[-\s]\d{7,8}', 'landline_cn', 0.90),
]

# 中文人名（2-4个汉字）
CN_NAME_PATTERN = r'[\u4e00-\u9fff]{2,4}(?:先生|女士|经理|总监|总裁|董事|主任)?'

# 公司名
ORG_PATTERNS = [
    r'[\u4e00-\u9fff]{2,20}(?:公司|集团|企业|机构|中心|部门|委员会)',
    r'[A-Z][A-Za-z\s]{2,30}(?:Inc\.|LLC|Corp\.|Ltd\.?|Co\.)',
]

# 关系动词（触发关系提取）
RELATION_VERBS = {
    '下单': 'order', '购买': 'purchase', '签署': 'sign', '授权': 'authorize',
    '支付': 'payment', '回款': 'payment_received', '关联': 'related',
    '负责': 'responsible', '审核': 'approve', '管理': 'manage',
    '属于': 'belongs_to', '拥有': 'owns', '参与': 'participate',
    '提供': 'provide', '合作': 'cooperate', '代理': 'agent',
}

# 事件类型关键词
EVENT_TYPE_KEYWORDS = {
    'order': ['下单', '订单', '购买', '采购', 'order'],
    'payment': ['支付', '回款', '付款', '转账', 'payment', 'PAY'],
    'sign': ['签署', '签订', '合同', '协议', 'sign', 'contract'],
    'anomaly': ['异常', '违规', '重复', '超额', '拒绝', 'error', 'fail'],
    'audit': ['审核', '审批', '核实', '核查', 'audit', 'approve'],
}


class EntityExtractor:
    """从可寻址单元中提取实体、关系、时间线事件"""

    def __init__(self):
        self._entity_counter = 0

    def _new_id(self, prefix: str) -> str:
        self._entity_counter += 1
        return f"{prefix}_{self._entity_counter:04d}"

    @staticmethod
    def _amount_value(text: str) -> Decimal:
        cleaned = re.sub(r'[,，\s¥￥$€]', '', text)
        match = re.fullmatch(r'(-?\d+(?:\.\d+)?)(亿元|万元|元|USD|CNY|EUR|亿|万)?', cleaned, re.I)
        if not match:
            raise ValueError(f"Invalid amount: {text}")
        multiplier = 100000000 if match.group(2) in ('亿', '亿元') else 10000 if match.group(2) in ('万', '万元') else 1
        return Decimal(match.group(1)) * multiplier

    def extract_mentions(self, unit):
        """Return typed, scoped mentions and conservative document bridges."""
        metadata = unit.metadata or {}
        location = {'file': unit.source_file, 'row': unit.row_num, 'sheet': unit.sheet_name,
                    'page': unit.page_num, 'paragraph': unit.paragraph_num,
                    'char_offset': unit.char_offset, 'metadata': metadata,
                    'cells': metadata.get('cells', {}), 'field_pointers': metadata.get('field_pointers', {})}
        structured = (unit.file_type in ('xlsx', 'xls', 'csv', 'tsv', 'json', 'jsonl', 'ndjson')
                      or metadata.get('structured') or metadata.get('structural'))
        return extract_document_entities(unit.raw_text, location=location,
            source_sha256=metadata.get('source_sha256'),
            fields=dict(zip(unit.field_names, unit.field_values)) if structured else None,
            ocr_confidence=unit.ocr_confidence)

    def extract_from_unit(self, unit) -> Tuple[List[Entity], List[TimelineEvent]]:
        """从一个AddressableUnit提取所有实体和时间线事件"""
        entities = []
        events = []

        # 结构化数据（Excel行）
        if unit.file_type in ('xlsx', 'xls', 'csv', 'tsv') or unit.metadata.get('structured') or unit.metadata.get('structural'):
            ents, evts = self._extract_structured(unit)
        else:
            ents, evts = self._extract_text(unit)

        entities.extend(ents)
        events.extend(evts)
        packet = self.extract_mentions(unit)
        for mention in packet['mentions']:
            expected = 'id' if mention['entity_type'] == 'identifier' else mention['entity_type']
            existing = next((entity for entity in entities if entity.entity_type == expected and
                             str(entity.name) == mention['raw_value'] and
                             (not mention['source_location'].get('field') or
                              entity.attributes.get('field') == mention['source_location']['field'])), None)
            if existing is None and expected == 'id':
                selector = mention.get('selector') or {}
                existing = Entity(mention['mention_id'], mention['raw_value'], 'id', mention['raw_value'],
                                  mention['raw_value'], unit.source_file, unit.row_num, unit.page_num,
                                  unit.paragraph_num, selector.get('source_start', selector.get('start')), None)
                entities.append(existing)
            if existing is not None:
                existing.attributes.update(identifier_namespace=mention['identifier_namespace'],
                                           assertion=mention['assertion'], scope=mention['scope'],
                                           ocr=mention['ocr'], identity_verified=False,
                                           can_seed_lookup=mention['can_seed_lookup'])
                existing.mentions.append({'mention_id': mention['mention_id'],
                                          'selector': mention['selector'], 'source_location': mention['source_location']})
        return entities, events

    def _extract_structured(self, unit) -> Tuple[List[Entity], List[TimelineEvent]]:
        """从结构化数据行提取"""
        entities = []
        events = []
        location = self._loc_str(unit)

        date_fields = []
        amount_fields = []
        id_fields = []
        name_fields = []

        for fname, fval in zip(unit.field_names, unit.field_values):
            if fval is None:
                continue
            val_str = str(fval).strip()
            if not val_str:
                continue
            fname_lower = fname.lower()
            is_key = is_identifier_field(fname)

            # 日期字段
            if not is_key and any(k in fname_lower for k in ['日期', '时间', 'date', 'time', '年月']):
                date_fields.append((fname, val_str))
                ent = Entity(
                    entity_id=self._new_id('date'),
                    name=val_str,
                    entity_type='date',
                    value=self._normalize_date(val_str),
                    raw_text=f"{fname}={val_str}",
                    source_file=unit.source_file,
                    row_num=unit.row_num,
                    page_num=unit.page_num,
                    paragraph_num=unit.paragraph_num,
                    char_offset=unit.char_offset,
                    confidence=0.95,
                    attributes={"field": fname, "normalization": "calendar_validated" if self._normalize_date(val_str) else "unresolved_or_invalid"},
                    mentions=[{"file": unit.source_file, "row": unit.row_num, "field": fname}]
                )
                entities.append(ent)

            # 金额字段
            elif not is_key and (any(k in fname_lower for k in ['金额', '价格', '费用', 'amount', 'price', 'fee', '单价']) or ('款' in fname_lower and not any(k in fname_lower for k in ['日期', '状态', '方式']))):
                amount_fields.append((fname, val_str))
                try:
                    num = self._amount_value(val_str)
                    ent = Entity(
                        entity_id=self._new_id('amt'),
                        name=val_str,
                        entity_type='amount',
                        value=num,
                        raw_text=f"{fname}={val_str}",
                        source_file=unit.source_file,
                        row_num=unit.row_num,
                        page_num=unit.page_num,
                        paragraph_num=unit.paragraph_num,
                        char_offset=unit.char_offset,
                        confidence=0.92,
                        attributes={"field": fname, "numeric": num},
                        mentions=[{"file": unit.source_file, "row": unit.row_num, "field": fname}]
                    )
                    entities.append(ent)
                    amount_fields.append((fname, num))
                except Exception:
                    pass

            # ID字段
            elif is_key:
                id_fields.append((fname, val_str))
                ent = Entity(
                    entity_id=self._new_id('id'),
                    name=val_str,
                    entity_type='id',
                    value=val_str,
                    raw_text=f"{fname}={val_str}",
                    source_file=unit.source_file,
                    row_num=unit.row_num,
                    page_num=unit.page_num,
                    paragraph_num=unit.paragraph_num,
                    char_offset=unit.char_offset,
                    confidence=0.95,
                    attributes={"field": fname},
                    mentions=[{"file": unit.source_file, "row": unit.row_num, "field": fname}]
                )
                entities.append(ent)

            # Explicit organization/location/phone columns precede broad name
            # heuristics; a company name must not turn into a person.
            elif any(k in fname_lower for k in ['公司', '企业', '机构', '组织', 'company', 'organization', '电话', '手机', 'phone', 'mobile', '城市', '地区', '地址', 'city', 'location']):
                kind = ('phone' if any(k in fname_lower for k in ['电话', '手机', 'phone', 'mobile']) else
                        'location' if any(k in fname_lower for k in ['城市', '地区', '地址', 'city', 'location']) else 'org')
                entities.append(Entity(
                    entity_id=self._new_id(kind), name=val_str, entity_type=kind,
                    value=val_str, raw_text=f"{fname}={val_str}",
                    source_file=unit.source_file, row_num=unit.row_num,
                    page_num=unit.page_num, paragraph_num=unit.paragraph_num,
                    char_offset=unit.char_offset, confidence=0.8,
                    attributes={"field": fname},
                    mentions=[{"file": unit.source_file, "row": unit.row_num, "field": fname}]))

            # 姓名字段
            elif any(k in fname_lower for k in ['姓名', '名字', '人名', 'name', '负责人', '销售', '客户']):
                explicit_name = any(k in fname_lower for k in ['姓名', '名字', '人名', 'name', '负责人'])
                supported_name = bool(re.fullmatch(CN_NAME_PATTERN, val_str) or
                                      (explicit_name and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ .'’\-]{1,119}", val_str)))
                if not any(k in fname_lower for k in ['product', '产品', '商品']) and supported_name:
                    name_fields.append((fname, val_str))
                    ent = Entity(
                        entity_id=self._new_id('person'),
                        name=val_str,
                        entity_type='person',
                        value=val_str,
                        raw_text=f"{fname}={val_str}",
                        source_file=unit.source_file,
                        row_num=unit.row_num,
                        page_num=unit.page_num,
                        paragraph_num=unit.paragraph_num,
                        char_offset=unit.char_offset,
                        confidence=0.85,
                        attributes={"field": fname},
                        mentions=[{"file": unit.source_file, "row": unit.row_num, "field": fname}]
                    )
                    entities.append(ent)

        # 生成时间线事件（若行中有日期和关键动作）
        if date_fields:
            event_desc = self._build_event_desc(unit)
            if event_desc:
                etype = self._classify_event(event_desc)
                evt = TimelineEvent(
                    time=date_fields[0][1],
                    time_normalized=self._normalize_date(date_fields[0][1]),
                    event=event_desc,
                    entities=[v for _, v in id_fields + name_fields],
                    source_file=unit.source_file,
                    source_location=location,
                    confidence=0.88,
                    event_type=etype,
                )
                events.append(evt)

        return entities, events

    def _extract_text(self, unit) -> Tuple[List[Entity], List[TimelineEvent]]:
        """从非结构化文本中提取"""
        entities = []
        events = []
        text = unit.raw_text
        location = self._loc_str(unit)

        # 提取日期
        date_spans = []
        for pattern, ptype, conf in DATE_PATTERNS:
            for m in re.finditer(pattern, text):
                if any(m.start() < end and m.end() > start for start, end in date_spans):
                    continue
                dt = m.group()
                normalized = self._normalize_date(dt)
                date_spans.append(m.span())
                if normalized is None:
                    continue
                ent = Entity(
                    entity_id=self._new_id('date'),
                    name=dt,
                    entity_type='date',
                    value=normalized,
                    raw_text=text[max(0, m.start()-20):m.end()+20],
                    source_file=unit.source_file,
                    row_num=unit.row_num,
                    page_num=unit.page_num,
                    paragraph_num=unit.paragraph_num,
                    char_offset=m.start() + (unit.char_offset or 0),
                    confidence=conf,
                    attributes={"pattern_type": ptype},
                    mentions=[{"file": unit.source_file, "char_offset": m.start()}]
                )
                entities.append(ent)

        # 提取金额
        amount_spans = []
        for pattern, ptype, conf in AMOUNT_PATTERNS:
            for m in re.finditer(pattern, text):
                if any(m.start() < end and m.end() > start for start, end in amount_spans):
                    continue
                amt_str = m.group().strip()
                if len(amt_str) < 2:
                    continue
                ent = Entity(
                    entity_id=self._new_id('amt'),
                    name=amt_str,
                    entity_type='amount',
                    value=self._amount_value(amt_str),
                    raw_text=text[max(0, m.start()-15):m.end()+15],
                    source_file=unit.source_file,
                    row_num=unit.row_num,
                    page_num=unit.page_num,
                    paragraph_num=unit.paragraph_num,
                    char_offset=m.start() + (unit.char_offset or 0),
                    confidence=conf,
                    attributes={"numeric": self._amount_value(amt_str)},
                    mentions=[]
                )
                entities.append(ent)
                amount_spans.append(m.span())

        # 提取ID
        id_spans = []
        for pattern, ptype, conf in ID_PATTERNS:
            for m in re.finditer(pattern, text):
                if any(m.start() < end and m.end() > start for start, end in date_spans + amount_spans + id_spans):
                    continue
                id_str = m.group()
                if re.fullmatch(r'1[3-9]\d{9}', id_str):
                    continue
                if len(id_str) < 4:
                    continue
                ent = Entity(
                    entity_id=self._new_id('id'),
                    name=id_str,
                    entity_type='id',
                    value=id_str,
                    raw_text=text[max(0, m.start()-10):m.end()+10],
                    source_file=unit.source_file,
                    row_num=unit.row_num,
                    page_num=unit.page_num,
                    paragraph_num=unit.paragraph_num,
                    char_offset=m.start() + (unit.char_offset or 0),
                    confidence=conf,
                    attributes={"id_type": ptype},
                    mentions=[]
                )
                entities.append(ent)
                id_spans.append(m.span())

        # Phone strings retain their literal value and never become identities.
        phone_spans = []
        for pattern, ptype, conf in PHONE_PATTERNS:
            bounded = r'(?<![A-Za-z0-9])(?:' + pattern + r')(?![A-Za-z0-9])'
            for m in re.finditer(bounded, text):
                if any(m.start() < end and m.end() > start for start, end in date_spans + amount_spans + phone_spans):
                    continue
                entities.append(Entity(
                    entity_id=self._new_id('phone'), name=m.group(), entity_type='phone',
                    value=m.group(), raw_text=text[max(0, m.start()-10):m.end()+10],
                    source_file=unit.source_file, row_num=unit.row_num,
                    page_num=unit.page_num, paragraph_num=unit.paragraph_num,
                    char_offset=m.start() + (unit.char_offset or 0), confidence=conf,
                    attributes={'phone_type': ptype, 'identity_status': 'not_an_identity'},
                    mentions=[{'file': unit.source_file, 'char_offset': m.start() + (unit.char_offset or 0)}]))
                phone_spans.append(m.span())

        # 提取时间线事件（文本中含日期+动词）
        for start, end in sorted(date_spans):
                dt = text[start:end]
                if self._normalize_date(dt) is None:
                    continue
                context = text[max(0, start-50):end+100]
                for verb, vtype in RELATION_VERBS.items():
                    if verb in context:
                        evt = TimelineEvent(
                            time=dt,
                            time_normalized=self._normalize_date(dt),
                            event=context.strip().replace('\n', ' ')[:120],
                            entities=re.findall(r'[A-Z]{1,4}\d{4,}', context),
                            source_file=unit.source_file,
                            source_location=location,
                            confidence=0.75,
                            event_type=self._classify_event(context),
                        )
                        events.append(evt)
                        break

        return entities, events

    def _build_event_desc(self, unit) -> str:
        """从结构化行构建事件描述"""
        parts = []
        important_fields = []
        for fname, fval in zip(unit.field_names, unit.field_values):
            if fval is None:
                continue
            fname_lower = fname.lower()
            if any(k in fname_lower for k in ['状态', 'status', '类型', 'type', '方式', '操作', '动作']):
                important_fields.insert(0, str(fval))
            elif any(k in fname_lower for k in ['金额', '价格', 'amount']):
                try:
                    num = self._amount_value(str(fval))
                    important_fields.append(f"金额:{num:.2f}")
                except Exception:
                    pass
            elif any(k in fname_lower for k in ['id', '编号', '单号']):
                parts.append(str(fval))
        parts.extend(important_fields[:3])
        return " | ".join(parts[:5]) if parts else ""

    def _classify_event(self, text: str) -> str:
        text_lower = text.lower()
        for etype, keywords in EVENT_TYPE_KEYWORDS.items():
            if any(k in text_lower for k in keywords):
                return etype
        return 'generic'

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """Retain supplied precision and clock; never invent a day or year."""
        from datetime import date, datetime
        s = date_str.replace('年', '-').replace('月', '-').replace('日', '').replace('/', '-').rstrip('-')
        m = re.fullmatch(r'(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(.*)', s)
        if m:
            y, mo, d, clock = m.groups()
            try:
                if d is None:
                    if clock or not 1 <= int(mo) <= 12:
                        return None
                    return f"{y}-{int(mo):02d}"
                day = date(int(y), int(mo), int(d)).isoformat()
                if clock.strip():
                    return datetime.fromisoformat(day + 'T' + clock.strip().lstrip('T')).isoformat()
                return day
            except ValueError:
                return None
        return None

    def _loc_str(self, unit) -> str:
        import os
        fname = os.path.basename(unit.source_file)
        parts = [fname]
        if unit.metadata.get('table'):
            parts.append(f"Table:{unit.metadata['table']}")
        if unit.sheet_name:
            parts.append(f"Sheet:{unit.sheet_name}")
        if unit.row_num:
            parts.append(f"Row:{unit.row_num}")
        if unit.page_num:
            parts.append(f"Page:{unit.page_num}")
        if unit.paragraph_num:
            parts.append(f"Para:{unit.paragraph_num}")
        return " | ".join(parts)

    def extract_relationships_from_row(self, unit, entities: List[Entity]) -> List[Relationship]:
        """从单行的实体集合推断关系"""
        rels = []
        ids = [e for e in entities if e.entity_type == 'id']
        persons = [e for e in entities if e.entity_type == 'person']
        amounts = [e for e in entities if e.entity_type == 'amount']

        # ID之间的关联（如订单ID↔客户ID在同一行 → 客户下单关系）
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                fa = id_a.attributes.get('field', '').lower()
                fb = id_b.attributes.get('field', '').lower()
                rel_type = self._infer_id_relation(fa, fb)
                source, target = id_a, id_b
                if rel_type is None:
                    rel_type = self._infer_id_relation(fb, fa)
                    source, target = id_b, id_a
                if rel_type:
                    rels.append(Relationship(
                        from_entity=source.entity_id,
                        to_entity=target.entity_id,
                        relation_type=rel_type,
                        confidence=None,
                        evidence=f"同行字段共现: {fa}={id_a.name} ↔ {fb}={id_b.name}；角色候选{rel_type}，未验证实际行为",
                        source_file=unit.source_file,
                        source_location=self._loc_str(unit),
                    ))

        # 人名↔ID关联
        for person in persons:
            for id_ent in ids:
                pf = person.attributes.get('field', '').lower()
                idf = id_ent.attributes.get('field', '').lower()
                if any(k in pf and k in idf for k in ['客户', '用户', 'customer', 'user']):
                    rels.append(Relationship(
                        from_entity=person.entity_id,
                        to_entity=id_ent.entity_id,
                        relation_type='identified_by',
                        confidence=None,
                        evidence=f"同行字段{pf}={person.name} ↔ {idf}={id_ent.name}；待核实身份对应",
                        source_file=unit.source_file,
                        source_location=self._loc_str(unit),
                    ))

        return rels

    def _infer_id_relation(self, field_a: str, field_b: str) -> Optional[str]:
        mappings = [
            (['客户', 'customer'], ['订单', 'order'], '下单'),
            (['订单', 'order'], ['回款', 'payment', 'pay'], '收款'),
            (['订单', 'order'], ['产品', 'product', '商品'], '包含'),
            (['供应商', 'supplier'], ['产品', 'product'], '供应'),
            (['员工', 'employee', '销售'], ['订单', 'order'], '负责'),
        ]
        for a_keys, b_keys, rel in mappings:
            a_match = any(k in field_a for k in a_keys)
            b_match = any(k in field_b for k in b_keys)
            if a_match and b_match:
                return rel
        return None
