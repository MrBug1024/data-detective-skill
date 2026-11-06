"""
matcher.py — 多类型匹配引擎
支持：精确ID匹配、直接字段匹配、模糊匹配、联合字段匹配、语义关键词匹配
每个匹配结果包含：unit、match_type、matched_fields、score、evidence
"""
import re
import os
import math
from dataclasses import dataclass, field, replace
from typing import List, Dict, Any, Optional, Tuple, Set
from collections import defaultdict
from functools import lru_cache

from parsers import AddressableUnit
from indexer import SourceIndex, _tokenize, value_text


def _same_file(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


@lru_cache(maxsize=4096)
def is_identifier_field(name: str) -> bool:
    """Recognize key labels without treating 'video'/'side'/'note' as IDs."""
    if re.search(r'(编号|单号|代码|工号|证件|身份证|统一社会信用|账户|账号|帐号|客户号|订单号)', name):
        return True
    words = re.sub(r'([a-z])([A-Z])', r'\1_\2', name).lower()
    return bool(re.search(r'(?:^|[^a-z0-9])(?:id|key|code|no)(?:$|[^a-z0-9])', words)
                or re.search(r'(?:id|code|key)$', words)
                or re.search(r'[\u4e00-\u9fff](?:id|no)', words))


@lru_cache(maxsize=4096)
def _key_family(name: str) -> str:
    words = re.sub(r'([a-z])([A-Z])', r'\1_\2', name).lower()
    words = re.sub(r'(编号|单号|代码|工号|号码|证件号|账号|帐号|id|code|key|number|\bno\b)', '', words)
    return re.sub(r'[\W_]+', '', words).removesuffix('号')


@dataclass
class MatchResult:
    """单条匹配结果"""
    unit: AddressableUnit           # 匹配到的业务单元
    match_type: str                 # exact_id / direct / indirect / fuzzy / joint / semantic / rule
    matched_fields: List[str]       # 匹配的字段名
    result_fields: List[str]        # 结果表中对应的字段名
    score: float                    # 匹配置信度 0~1
    evidence: str                   # 证据描述（"字段X=值Y精确匹配"等）
    hop_path: List[str] = field(default_factory=list)  # 多跳路径

    def to_dict(self) -> Dict:
        d = {
            "unit_id": self.unit.unit_id,
            "source_file": self.unit.source_file,
            "file_type": self.unit.file_type,
            "row_num": self.unit.row_num,
            "page_num": self.unit.page_num,
            "paragraph_num": self.unit.paragraph_num,
            "sheet_name": self.unit.sheet_name,
            "match_type": self.match_type,
            "matched_fields": self.matched_fields,
            "result_fields": self.result_fields,
            "score": round(self.score, 4),
            "evidence": self.evidence,
            "hop_path": self.hop_path,
            "raw_text_snippet": self.unit.raw_text[:300],
        }
        return d


class MatchConfig:
    """匹配引擎配置"""
    def __init__(self, config: Dict = None):
        if config is not None and not isinstance(config, dict):
            raise ValueError('配置必须为 JSON 对象')
        cfg = config or {}
        self.fuzzy_threshold = cfg.get("fuzzy_threshold", 80)      # rapidfuzz相似度阈值
        self.semantic_threshold = cfg.get("semantic_threshold", 0.6)
        self.max_candidates = cfg.get("max_candidates", 20)
        self.max_hops = cfg.get("max_hops", 3)
        self.known_key_fields = cfg.get("known_key_fields", [])     # 用户指定的关联键字段
        self.field_aliases = cfg.get("field_aliases", {})           # 显式结果字段 → 源字段名
        self.field_weights = cfg.get("field_weights", {})           # 字段权重
        self.joint_fields = cfg.get("joint_fields", [])             # 联合关联字段组
        self.skip_fields = cfg.get("skip_fields", ["序号", "index", "no.", "编号", "id_auto"])
        if type(self.fuzzy_threshold) not in (int, float) or not math.isfinite(self.fuzzy_threshold) or not 0 <= self.fuzzy_threshold <= 100:
            raise ValueError("fuzzy_threshold must be between 0 and 100")
        if type(self.semantic_threshold) not in (int, float) or not math.isfinite(self.semantic_threshold) or not 0 <= self.semantic_threshold <= 1:
            raise ValueError("semantic_threshold must be between 0 and 1")
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 200:
            raise ValueError("max_candidates must be an integer between 1 and 200")
        if type(self.max_hops) is not int or not 1 <= self.max_hops <= 10:
            raise ValueError('max_hops must be an integer between 1 and 10 (reserved; no automatic traversal)')
        for name, values in [('known_key_fields', self.known_key_fields), ('skip_fields', self.skip_fields)]:
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError(f'{name} must be a list of field names')
        if not isinstance(self.joint_fields, list) or any(not isinstance(group, list) or len(group) < 2 or any(not isinstance(name, str) for name in group) for group in self.joint_fields):
            raise ValueError('joint_fields must contain groups of at least two field names')
        if not isinstance(self.field_weights, dict) or any(not isinstance(name, str) or type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0 for name, weight in self.field_weights.items()):
            raise ValueError('field_weights must map field names to finite nonnegative numbers')
        if not isinstance(self.field_aliases, dict) or any(
            not isinstance(name, str) or not isinstance(aliases, list) or not aliases
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
            for name, aliases in self.field_aliases.items()
        ):
            raise ValueError('field_aliases must map result field names to nonempty lists of source field names')


class MatchEngine:
    """多类型匹配引擎"""

    def __init__(self, index: SourceIndex, config: MatchConfig = None):
        self.index = index
        self.config = config or MatchConfig()
        self.last_search_stats = {}
        self._retrieval_limits = []
        self._known_names = {name.lower().strip() for name in self.config.known_key_fields}
        self._aliases = {key.lower().strip(): {value.lower().strip() for value in values}
                         for key, values in self.config.field_aliases.items()}

    def _is_key(self, name):
        return is_identifier_field(name) or name.lower().strip() in self._known_names

    def _same_key_role(self, result_name, source_name):
        left, right = result_name.lower().strip(), source_name.lower().strip()
        if left == right or right in self._aliases.get(left, set()):
            return True
        # Role labels are not translated or guessed. Explicit mappings handle
        # renamed keys; order_id and customer_id must remain distinct.
        family = _key_family(result_name)
        return self._is_key(result_name) and self._is_key(source_name) and bool(family) and family == _key_family(source_name)

    def match_result_unit(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """
        对结果行进行全类型匹配，返回候选匹配列表（按score降序）
        """
        candidates: List[MatchResult] = []
        self._retrieval_limits = []

        # 1. 精确ID匹配
        candidates.extend(self._exact_id_match(result_unit))

        # 2. 直接字段精确匹配
        candidates.extend(self._direct_field_match(result_unit))

        # 3. 联合字段匹配
        candidates.extend(self._joint_field_match(result_unit))

        # 4. 模糊字符串匹配
        candidates.extend(self._fuzzy_match(result_unit))

        # 5. Token/语义关键词匹配
        candidates.extend(self._token_semantic_match(result_unit))

        # 6. 规则节点匹配
        candidates.extend(self._rule_match(result_unit))

        # 去重（同一unit取最高分）
        deduped = self._deduplicate(candidates)
        conflicting = [m for m in deduped if self._conflicting_keys(result_unit, m.unit)]
        deduped = [m for m in deduped if not self._conflicting_keys(result_unit, m.unit)]

        # 排序
        deduped.sort(key=lambda x: (-x.score, -len(x.matched_fields), x.unit.source_file, x.unit.row_num or 0, x.unit.unit_id))

        # 确保每个源文件都有代表（不因总数限制而漏掉某个文件）
        # 先取每文件最高分，再补充其余高分候选
        from collections import defaultdict
        per_file_best: Dict[str, MatchResult] = {}
        others = []
        for m in deduped:
            fp = m.unit.source_file
            if fp not in per_file_best:
                per_file_best[fp] = m
            else:
                others.append(m)

        # 合并：每文件最佳 + 其他高分候选
        result = list(per_file_best.values())[:self.config.max_candidates]
        # 补充其他候选（总量不超过max_candidates）
        remaining = self.config.max_candidates - len(result)
        result.extend(others[:max(0, remaining)])
        self.last_search_stats = {
            'eligible_candidates': len(deduped), 'returned_candidates': len(result),
            'omitted_candidates': len(deduped) - len(result),
            'candidate_limit': self.config.max_candidates,
            'excluded_key_conflicts': len(conflicting),
            'excluded_key_conflict_units': [m.unit.unit_id for m in conflicting[:200]],
            'omitted_candidate_files': sorted(
                {os.path.normcase(os.path.abspath(m.unit.source_file)) for m in deduped}
                - {os.path.normcase(os.path.abspath(m.unit.source_file)) for m in result}),
            'retrieval_limits': list(self._retrieval_limits),
            'search_complete': not self._retrieval_limits and len(result) == len(deduped),
            'scope': 'single_hop_candidate_retrieval_not_verified_lineage',
        }
        return sorted(result, key=lambda x: (-x.score, -len(x.matched_fields), x.unit.source_file, x.unit.row_num or 0, x.unit.unit_id))

    def _conflicting_keys(self, result, source):
        """A shared product/date does not override contradictory same-named IDs."""
        for name, value in zip(result.field_names, result.field_values):
            if value is None or not value_text(value):
                continue
            if not self._is_key(name):
                continue
            for source_name, other in zip(source.field_names, source.field_values):
                if self._same_key_role(name, source_name) and other is not None and value_text(other) and value_text(other) != value_text(value):
                    return True
        return False

    def _exact_id_match(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """精确ID/主外键匹配：寻找ID-like字段精确相等"""
        results = []
        # 值本身看起来是ID（非纯数字、非日期、有字母前缀）
        id_value_pattern = re.compile(r'[A-Za-z\u4e00-\u9fff]+[-_]?\d+[A-Za-z0-9_-]*')

        for fname, fval in zip(result_unit.field_names, result_unit.field_values):
            if fval is None or str(fval).strip() == '':
                continue
            if fname.lower() in [s.lower() for s in self.config.skip_fields]:
                continue

            val_str = value_text(fval)

            # 判断是否为ID字段：字段名匹配 或 值格式匹配
            is_id_field = self._is_key(fname)
            is_id_value = bool(id_value_pattern.fullmatch(val_str)) and len(val_str) >= 4

            if not (is_id_field or is_id_value):
                continue

            # 日期格式排除（不参与ID匹配）
            if re.match(r'\d{4}-\d{2}-\d{2}', val_str):
                continue

            matches = self.index.lookup_exact(val_str)

            for unit in matches:
                if _same_file(unit.source_file, result_unit.source_file):
                    continue
                # 找到对应字段名
                matched_fn = [fn for fn, fv in zip(unit.field_names, unit.field_values)
                              if fv is not None and value_text(fv) == val_str
                              and self._same_key_role(fname, fn)]
                if matched_fn:
                    # 如果目标字段也是ID字段，给更高分
                    tgt_is_id = any(self._is_key(fn) for fn in matched_fn)
                    score = 0.95 if tgt_is_id else 0.85
                    results.append(MatchResult(
                        unit=unit,
                        match_type="exact_id",
                        matched_fields=matched_fn[:1],
                        result_fields=[fname],
                        score=score,
                        evidence=f"ID精确匹配: 结果字段[{fname}]={val_str!r} ↔ 源字段{matched_fn[:1]}"
                    ))
            # An embedded ID is a useful citation even when document-level
            # token Jaccard is low. This is a mention, never a proven join.
            for uid in self.index.lookup_tokens(val_str):
                unit = self.index.get_unit(uid)
                if (not unit or _same_file(unit.source_file, result_unit.source_file)
                    or unit.file_type in ('csv', 'tsv', 'xlsx', 'xls')
                    or unit.metadata.get('structured') or unit.metadata.get('structural')):
                    continue
                boundary = r'(?<![A-Za-z0-9_])' + re.escape(val_str) + r'(?![A-Za-z0-9_])'
                fields = [fn for fn, fv in zip(unit.field_names, unit.field_values)
                          if fv is not None and re.search(boundary, value_text(fv))]
                if fields:
                    results.append(MatchResult(
                        unit=unit, match_type='indirect', matched_fields=fields[:1],
                        result_fields=[fname], score=0.75,
                        evidence=f"标识原文提及: [{fname}]={val_str!r} 位于源字段[{fields[0]}]；提及不能独立证明派生/连接关系"))
        return results

    def _direct_field_match(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """直接字段值精确匹配"""
        results = []
        val_match_count: Dict[str, List[Tuple[str, str]]] = defaultdict(list)  # unit_id → [(result_fn, src_fn)]

        for fname, fval in zip(result_unit.field_names, result_unit.field_values):
            if fval is None:
                continue
            val_str = value_text(fval)
            if len(val_str) < 2:
                continue
            if fname.lower() in [s.lower() for s in self.config.skip_fields]:
                continue

            weight = self.config.field_weights.get(fname, 1.0)
            matches = self.index.lookup_exact(val_str)

            for unit in matches:
                if _same_file(unit.source_file, result_unit.source_file):
                    continue
                src_fn = [fn for fn, fv in zip(unit.field_names, unit.field_values)
                          if fv is not None and value_text(fv) == val_str]
                if src_fn:
                    # A single source cell cannot substantiate several result
                    # fields that happen to repeat the same value.
                    used = {s for _, s in val_match_count[unit.unit_id]}
                    available = [fn for fn in src_fn if fn not in used]
                    if available:
                        chosen = next((fn for fn in available if fn.lower() == fname.lower()), available[0])
                        val_match_count[unit.unit_id].append((fname, chosen))

        for unit_id, field_pairs in val_match_count.items():
            unit = self.index.get_unit(unit_id)
            if not unit:
                continue
            n = len(field_pairs)
            weighted = sum(max(0, min(float(self.config.field_weights.get(fn, 1.0)), 2.0)) for fn, _ in field_pairs)
            score = min(0.5 + 0.15 * weighted, 0.90)
            evidence = f"直接匹配{n}个字段: " + ", ".join(f"[{r}]↔[{s}]" for r, s in field_pairs)
            results.append(MatchResult(
                unit=unit,
                match_type="direct",
                matched_fields=[s for _, s in field_pairs],
                result_fields=[r for r, _ in field_pairs],
                score=score,
                evidence=evidence
            ))

        return results

    def _joint_field_match(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """联合字段匹配：多个字段组合共同匹配"""
        results = []
        # 使用配置的联合字段组，或自动探测
        joint_groups = self.config.joint_fields
        if not joint_groups:
            # 自动：取前5个非ID非空字段两两组合
            candidate_fields = [(fn, fv) for fn, fv in zip(result_unit.field_names, result_unit.field_values)
                                 if fv is not None and len(str(fv).strip()) > 1
                                 and not re.search(r'(id|_id|index|no\.|序号)', fn, re.IGNORECASE)][:5]
            if len(candidate_fields) >= 2:
                joint_groups = [[candidate_fields[i][0], candidate_fields[j][0]]
                                 for i in range(len(candidate_fields))
                                 for j in range(i+1, min(i+3, len(candidate_fields)))]

        for group in joint_groups:
            group_vals = []
            for fn in group:
                val = result_unit.get_field(fn)
                if val is not None:
                    if value_text(val):
                        group_vals.append((fn, value_text(val)))

            if len(group_vals) < 2 or len(group_vals) != len(group) or len(set(group)) != len(group):
                continue

            # 找同时包含所有字段值的单元
            candidate_sets = None
            for fn, val in group_vals:
                matched_uids = set(u.unit_id for u in self.index.lookup_exact(val))
                if candidate_sets is None:
                    candidate_sets = matched_uids
                else:
                    candidate_sets &= matched_uids

            if candidate_sets:
                for uid in candidate_sets:
                    unit = self.index.get_unit(uid)
                    if unit and not _same_file(unit.source_file, result_unit.source_file):
                        source_fields = []
                        for rfn, value in group_vals:
                            options = [fn for fn, fv in zip(unit.field_names, unit.field_values)
                                       if fv is not None and value_text(fv) == value and fn not in source_fields]
                            if not options:
                                break
                            source_fields.append(next((fn for fn in options if fn.lower() == rfn.lower()), options[0]))
                        if len(source_fields) != len(group_vals):
                            continue
                        score = 0.88
                        evidence = f"联合匹配{len(group_vals)}个字段: " + " & ".join(f"[{fn}]={val!r}" for fn, val in group_vals)
                        results.append(MatchResult(
                            unit=unit,
                            match_type="joint",
                            matched_fields=source_fields,
                            result_fields=[fn for fn, _ in group_vals],
                            score=score,
                            evidence=evidence
                        ))
        return results

    def _fuzzy_match(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """模糊字符串匹配（编辑距离/相似度）"""
        try:
            from rapidfuzz.fuzz import ratio as similarity
        except ImportError:
            from difflib import SequenceMatcher
            def similarity(left, right):
                return SequenceMatcher(None, left, right).ratio() * 100

        results = []
        threshold = self.config.fuzzy_threshold

        # 对结果行的文本字段进行模糊匹配
        for fname, fval in zip(result_unit.field_names, result_unit.field_values):
            if fval is None:
                continue
            val_str = str(fval).strip()
            if len(val_str) < 4:  # 太短的不模糊匹配
                continue
            if fname.lower() in {s.lower() for s in self.config.skip_fields} or re.fullmatch(r'[\d\W]+', val_str):
                continue
            if self._is_key(fname) or re.fullmatch(r'[A-Za-z]+\d+', val_str):
                continue

            # 从索引中获取候选（Token命中的单元中做模糊）
            token_hits = self.index.lookup_tokens(val_str, min_token_len=3)
            eligible = [(uid, hits) for uid, hits in token_hits.items()
                        if not _same_file(self.index.get_unit(uid).source_file, result_unit.source_file)
                        and not self._conflicting_keys(result_unit, self.index.get_unit(uid))]
            top_candidates = sorted(eligible, key=lambda x: (-x[1], x[0]))[:50]
            if len(eligible) > 50:
                self._retrieval_limits.append({'stage': 'fuzzy', 'field': fname,
                                               'candidate_units': len(eligible), 'examined_units': 50})

            for uid, _ in top_candidates:
                unit = self.index.get_unit(uid)
                if not unit or _same_file(unit.source_file, result_unit.source_file):
                    continue

                for src_fn, src_fv in zip(unit.field_names, unit.field_values):
                    if src_fv is None:
                        continue
                    src_str = str(src_fv).strip()
                    if len(src_str) < 3:
                        continue

                    ratio = similarity(val_str, src_str)
                    if ratio >= threshold:
                        score = 0.7 if threshold == 100 else 0.3 + (ratio - threshold) / (100 - threshold) * 0.4
                        results.append(MatchResult(
                            unit=unit,
                            match_type="fuzzy",
                            matched_fields=[src_fn],
                            result_fields=[fname],
                            score=score,
                            evidence=f"模糊匹配: [{fname}]={val_str!r} ~{ratio:.0f}%~ [{src_fn}]={src_str!r}"
                        ))
                        break

        return results

    def _token_semantic_match(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """Token/关键词语义匹配（无需大模型）"""
        results = []
        # 构造结果行的全文
        full_text = " ".join(str(v) for v in result_unit.field_values if v is not None)
        token_hits = self.index.lookup_tokens(full_text, min_token_len=3)

        # 取命中数最多的候选
        eligible = [(uid, hits) for uid, hits in token_hits.items()
                    if not _same_file(self.index.get_unit(uid).source_file, result_unit.source_file)
                    and not self._conflicting_keys(result_unit, self.index.get_unit(uid))]
        top = sorted(eligible, key=lambda x: (-x[1], x[0]))[:20]
        if len(eligible) > 20:
            self._retrieval_limits.append({'stage': 'literal_tokens', 'candidate_units': len(eligible), 'examined_units': 20})

        query_tokens = {t for t in _tokenize(full_text) if len(t) >= 3}

        for uid, hit_count in top:
            unit = self.index.get_unit(uid)
            if not unit or _same_file(unit.source_file, result_unit.source_file):
                continue

            source_tokens = {t for v in unit.field_values if v is not None for t in _tokenize(value_text(v)) if len(t) >= 3}
            overlap = len(query_tokens & source_tokens) / max(len(query_tokens | source_tokens), 1)
            score = min(0.2 + overlap * 0.5, 0.65)

            if overlap >= self.config.semantic_threshold:
                results.append(MatchResult(
                    unit=unit,
                    match_type="semantic",
                    matched_fields=[],
                    result_fields=[],
                    score=score,
                    evidence=f"字面关键词重叠: {hit_count}个共同词汇/值；Jaccard={overlap:.3f}，不代表语义或来源认定"
                ))

        return results

    def _rule_match(self, result_unit: AddressableUnit) -> List[MatchResult]:
        """规则节点匹配：相关规则/定义的查找"""
        results = []
        rule_units = self.index.get_rule_units()

        full_text = " ".join(str(v) for v in result_unit.field_values if v is not None).lower()

        for unit in rule_units:
            rule_text = unit.raw_text.lower()
            # 简单关键词重叠
            result_tokens = set(t for t in re.split(r'\W+', full_text) if len(t) >= 3)
            rule_tokens = set(t for t in re.split(r'\W+', rule_text) if len(t) >= 3)
            overlap = result_tokens & rule_tokens
            if len(overlap) >= 2:
                score = min(0.3 + len(overlap) * 0.05, 0.6)
                results.append(MatchResult(
                    unit=unit,
                    match_type="rule",
                    matched_fields=[],
                    result_fields=[],
                    score=score,
                    evidence=f"规则节点相关: 共同关键词 {list(overlap)[:5]}"
                ))

        return results

    def _deduplicate(self, candidates: List[MatchResult]) -> List[MatchResult]:
        """同一unit保留最高分匹配；分相同时用匹配字段数作为tiebreaker"""
        best: Dict[str, MatchResult] = {}
        for c in candidates:
            uid = c.unit.unit_id
            if uid not in best:
                best[uid] = replace(c, matched_fields=list(c.matched_fields), result_fields=list(c.result_fields))
            else:
                prev = best[uid]
                # 分更高 or 分相同但匹配字段更多
                if c.score > prev.score or (c.score == prev.score and len(c.matched_fields) > len(prev.matched_fields)):
                    best[uid] = replace(c, matched_fields=list(c.matched_fields), result_fields=list(c.result_fields))
        # Preserve verified field mappings discarded by the score winner; these
        # remain one source record, so do not add independent-evidence bonuses.
        for candidate in candidates:
            if candidate.match_type not in ('exact_id', 'direct', 'joint'):
                continue
            winner = best[candidate.unit.unit_id]
            for rfn, sfn in zip(candidate.result_fields, candidate.matched_fields):
                if rfn not in winner.result_fields and sfn not in winner.matched_fields:
                    winner.result_fields.append(rfn)
                    winner.matched_fields.append(sfn)
        return list(best.values())

    def _match_quality(self, m: MatchResult) -> float:
        """用于排序的综合质量分：分数 + 匹配字段数奖励"""
        field_bonus = min(len(m.matched_fields) * 0.01, 0.05)
        return m.score + field_bonus
