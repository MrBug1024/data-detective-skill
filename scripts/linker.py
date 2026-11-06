"""
linker.py — 链路搜索、评分与假设生成
将匹配结果组装成有向图"数据链路"，计算整体置信度，标记缺失项和冲突项
"""
import uuid
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple, Set
from collections import defaultdict

from parsers import AddressableUnit
from matcher import MatchResult, MatchEngine, MatchConfig
from indexer import SourceIndex


@dataclass
class ChainNode:
    """链路节点"""
    node_id: str
    node_type: str              # result / source / rule / intermediate
    label: str
    source_file: str
    file_type: str
    row_num: Optional[int]
    page_num: Optional[int]
    paragraph_num: Optional[int]
    sheet_name: Optional[str]
    field_names: List[str]
    field_values: List[str]
    raw_text_snippet: str
    confidence: float
    is_rule: bool = False
    metadata: Dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class ChainEdge:
    """链路边"""
    edge_id: str
    from_node: str
    to_node: str
    edge_type: str              # exact_id / direct / indirect / fuzzy / joint / semantic / rule / inferred
    matched_fields: List[str]
    result_fields: List[str]
    score: float
    evidence: str
    hop_path: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class EvidenceItem:
    """证据片段"""
    evidence_id: str
    node_id: str
    field_name: str
    value: str
    matched_value: str
    match_type: str
    score: float
    source_location: str       # "文件:行号" 或 "文件:页:段"

    def to_dict(self):
        return asdict(self)


@dataclass
class DataChain:
    """完整数据链路"""
    result_ref: Dict
    chain_id: str
    nodes: List[ChainNode]
    edges: List[ChainEdge]
    evidence: List[EvidenceItem]
    confidence: float
    missing: List[Dict]           # 缺失的节点/字段
    conflicts: List[Dict]         # 冲突候选
    hypotheses: List[Dict]        # 暂疑结论
    source_locations: List[Dict]  # 定位信息
    coverage: Dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "result_ref": self.result_ref,
            "chain_id": self.chain_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "evidence": [ev.to_dict() for ev in self.evidence],
            "confidence": round(self.confidence, 4),
            "missing": self.missing,
            "conflicts": self.conflicts,
            "hypotheses": self.hypotheses,
            "source_locations": self.source_locations,
            "coverage": self.coverage,
        }

    def save_json(self, path: str):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2, default=str)
        print(f"[INFO] Chain saved to {path}")


class ChainLinker:
    """链路构建器"""

    def __init__(self, index: SourceIndex, config: MatchConfig = None):
        self.index = index
        self.config = config or MatchConfig()
        self.engine = MatchEngine(index, config)

    def build_chain(self, result_unit: AddressableUnit, source_files: List[str]) -> DataChain:
        """
        从结果单元出发，构建完整候选数据链路
        """
        chain_id = str(uuid.uuid4())[:8]

        # Candidate limits are explicit; no claim of a complete lineage graph.
        all_matches = self.engine.match_result_unit(result_unit)

        # Step 2: 按源文件分组，选择每个文件的最佳匹配
        # 先收集per-unit的所有匹配结果，合并分数（同unit多种匹配类型时叠加）
        unit_scores: Dict[str, float] = defaultdict(float)
        unit_match_count: Dict[str, int] = defaultdict(int)
        unit_best_match: Dict[str, MatchResult] = {}

        for m in all_matches:
            uid = m.unit.unit_id
            # 累加每种匹配类型的分数（最多累计到1.0）
            unit_scores[uid] = min(unit_scores[uid] + m.score * 0.3, 1.0) if uid in unit_scores else m.score
            unit_match_count[uid] += 1
            # 保留分最高的匹配类型作为代表
            if uid not in unit_best_match or m.score > unit_best_match[uid].score:
                unit_best_match[uid] = m

        # 用聚合分数更新每个match的score
        aggregated_matches = []
        for uid, match in unit_best_match.items():
            agg_score = min(match.score + (unit_match_count[uid] - 1) * 0.05, 1.0)
            match.score = agg_score
            aggregated_matches.append(match)

        file_best: Dict[str, MatchResult] = {}
        file_all: Dict[str, List[MatchResult]] = defaultdict(list)
        for m in aggregated_matches:
            fp = m.unit.source_file
            file_all[fp].append(m)
            if fp not in file_best or m.score > file_best[fp].score:
                file_best[fp] = m

        # 对每个文件内的候选按score排序
        for fp in file_all:
            file_all[fp].sort(key=lambda x: x.score, reverse=True)

        # Step 3: 构建节点
        nodes = []
        # 结果节点
        result_node = ChainNode(
            node_id=f"result_{chain_id}",
            node_type="result",
            label=f"结果记录 Row{result_unit.row_num}",
            source_file=result_unit.source_file,
            file_type=result_unit.file_type,
            row_num=result_unit.row_num,
            page_num=result_unit.page_num,
            paragraph_num=result_unit.paragraph_num,
            sheet_name=result_unit.sheet_name,
            field_names=result_unit.field_names,
            field_values=[str(v) for v in result_unit.field_values],
            raw_text_snippet=result_unit.raw_text[:300],
            confidence=1.0,
            metadata=dict(result_unit.metadata),
        )
        nodes.append(result_node)

        # Preserve alternative rows from the same file. A one-to-many join or
        # aggregate cannot be reviewed if only one representative survives.
        node_map: Dict[str, ChainNode] = {result_node.node_id: result_node}  # unit_id → node
        edges = []
        evidence_list = []
        source_locations = []

        for match in aggregated_matches:
            u = match.unit
            representative = file_best[u.source_file].unit.unit_id == u.unit_id
            node_id = f"src_{u.unit_id}"
            node = ChainNode(
                node_id=node_id,
                node_type="rule" if u.is_rule else "source" if representative else "intermediate",
                label=self._make_label(u),
                source_file=u.source_file,
                file_type=u.file_type,
                row_num=u.row_num,
                page_num=u.page_num,
                paragraph_num=u.paragraph_num,
                sheet_name=u.sheet_name,
                field_names=u.field_names,
                field_values=[str(v) for v in u.field_values],
                raw_text_snippet=u.raw_text[:300],
                confidence=match.score,
                is_rule=u.is_rule,
                metadata={**u.metadata,
                          "candidate_role": "file_representative" if representative else "alternative",
                          "verification": "unverified_candidate",
                          "score_semantics": "heuristic_match_score_not_probability"},
            )
            nodes.append(node)
            node_map[u.unit_id] = node

            # 边
            edge = ChainEdge(
                edge_id=f"edge_{chain_id}_{len(edges)}",
                from_node=result_node.node_id,
                to_node=node_id,
                edge_type=match.match_type,
                matched_fields=match.matched_fields,
                result_fields=match.result_fields,
                score=match.score,
                evidence=match.evidence,
            )
            edges.append(edge)

            # 证据
            for rfn, mfn in zip(match.result_fields or [], match.matched_fields or []):
                rv = result_unit.get_field(rfn)
                sv = u.get_field(mfn)
                ev = EvidenceItem(
                    evidence_id=f"ev_{chain_id}_{len(evidence_list)}",
                    node_id=node_id,
                    field_name=mfn,
                    value=str(rv) if rv is not None else "",
                    matched_value=str(sv) if sv is not None else "",
                    match_type=match.match_type,
                    score=match.score,
                    source_location=self._location_str(u),
                )
                evidence_list.append(ev)

            # 位置信息
            source_locations.append({
                "node_id": node_id,
                "file": u.source_file,
                "row": u.row_num,
                "page": u.page_num,
                "paragraph": u.paragraph_num,
                "char_offset": u.char_offset,
                "sheet": u.sheet_name,
                "metadata": dict(u.metadata),
            })

        # Step 4: 多候选冲突检测
        conflicts = []
        for fp, matches in file_all.items():
            if len(matches) > 1 and matches[0].score > 0.4:
                top2 = sorted(matches, key=lambda x: x.score, reverse=True)[:3]
                score_diff = top2[0].score - top2[1].score if len(top2) > 1 else 1.0
                if score_diff < 0.2:  # 两个候选分数接近，标记冲突
                    conflicts.append({
                        "file": fp,
                        "reason": f"多候选接近: 最高分{top2[0].score:.2f} vs {top2[1].score:.2f}",
                        "candidates": [m.to_dict() for m in top2[:2]],
                    })

        # Step 5: 缺失节点检测
        coverage = dict(self.engine.last_search_stats)
        missing = []
        if coverage.get('omitted_candidates'):
            missing.append({'type': 'candidate_truncated',
                            'reason': f"有{coverage['omitted_candidates']}条候选未返回；需提高max_candidates或缩小来源范围后定向核查。"})
        if coverage.get('retrieval_limits'):
            missing.append({'type': 'retrieval_budget',
                            'reason': '模糊/字面检索达到候选预算；未检出不代表其他记录不存在。'})
        source_file_set = {os.path.normcase(os.path.abspath(fp)) for fp in source_files}
        matched_files = {os.path.normcase(os.path.abspath(fp)) for fp in file_best}
        unmatched = source_file_set - matched_files
        for fp in sorted(unmatched):
            if fp in coverage.get('omitted_candidate_files', []):
                missing.append({'type': 'source_candidates_omitted', 'file': fp,
                                'reason': '该源存在候选，但被全局候选上限截断；不能视为未检出。'})
                continue
            missing.append({
                "type": "unmatched_source",
                "file": fp,
                "reason": "该源文件中未找到匹配记录",
            })

        # 检测结果字段是否有值在源中无对应
        matched_result_fields = set()
        for m in file_best.values():
            matched_result_fields.update(m.result_fields or [])
        for fname, fval in zip(result_unit.field_names, result_unit.field_values):
            if fname not in matched_result_fields and fval is not None and str(fval).strip():
                missing.append({
                    "type": "unmatched_field",
                    "field": fname,
                    "value": str(fval),
                    "reason": "结果字段值在源文件中未找到对应",
                })

        # Step 6: 计算整体置信度
        if file_best:
            scores = [m.score for m in file_best.values()]
            overall_conf = sum(scores) / len(scores) * (1 - 0.05 * len(conflicts))
        else:
            overall_conf = 0.0

        # Step 7: 生成假设
        hypotheses = self._generate_hypotheses(nodes, edges, evidence_list, missing, conflicts)

        return DataChain(
            result_ref={
                "file": result_unit.source_file,
                "row": result_unit.row_num,
                "sheet": result_unit.sheet_name,
                "fields": result_unit.field_names,
                "values": [str(v) for v in result_unit.field_values],
            },
            chain_id=chain_id,
            nodes=nodes,
            edges=edges,
            evidence=evidence_list,
            confidence=round(max(0.0, min(1.0, overall_conf)), 4),
            missing=missing,
            conflicts=conflicts,
            hypotheses=hypotheses,
            source_locations=source_locations,
            coverage=coverage,
        )

    def _make_label(self, unit: AddressableUnit) -> str:
        import os
        fname = os.path.basename(unit.source_file)
        if unit.row_num:
            return f"{fname} 第{unit.row_num}行"
        elif unit.page_num and unit.paragraph_num:
            return f"{fname} 第{unit.page_num}页第{unit.paragraph_num}段"
        elif unit.paragraph_num:
            return f"{fname} 第{unit.paragraph_num}段"
        return fname

    def _location_str(self, unit: AddressableUnit) -> str:
        import os
        fname = os.path.basename(unit.source_file)
        parts = [fname]
        if unit.sheet_name:
            parts.append(f"Sheet:{unit.sheet_name}")
        if unit.metadata.get('table'):
            parts.append(f"Table:{unit.metadata['table']}")
        if unit.row_num:
            parts.append(f"Row:{unit.row_num}")
        if unit.page_num:
            parts.append(f"Page:{unit.page_num}")
        if unit.paragraph_num:
            parts.append(f"Para:{unit.paragraph_num}")
        return " | ".join(parts)

    def _generate_hypotheses(self, nodes, edges, evidence, missing, conflicts) -> List[Dict]:
        """生成暂疑结论"""
        hypotheses = []

        source_nodes = [n for n in nodes if n.node_type in ("source", "intermediate")]
        rule_nodes = [n for n in nodes if n.node_type == "rule"]

        if source_nodes:
            files = list(set(n.source_file for n in source_nodes))
            h = {
                "hypothesis_id": f"h_{len(hypotheses)+1}",
                "type": "origin_hypothesis",
                "statement": f"找到{len(files)}个存在字段关联的候选业务数据源；尚未验证实际生成链路",
                "supporting_nodes": [n.node_id for n in source_nodes],
                "supporting_evidence": [e.evidence_id for e in evidence],
                "counter_evidence": [c["reason"] for c in conflicts],
                "missing_items": [m["reason"] for m in missing if m["type"] == "unmatched_source"],
                "confidence": round(sum(n.confidence for n in source_nodes) / len(source_nodes), 4),
                "status": "tentative",  # tentative / confirmed / refuted
            }
            hypotheses.append(h)

        if rule_nodes:
            hypotheses.append({
                "hypothesis_id": f"h_{len(hypotheses)+1}",
                "type": "rule_hypothesis",
                "statement": f"发现{len(rule_nodes)}个相关规则/计算逻辑节点，可能参与了结果生成",
                "supporting_nodes": [n.node_id for n in rule_nodes],
                "confidence": 0.5,
                "status": "tentative",
            })

        return hypotheses


def validate_chain(new_chain: DataChain, hypothesis: Dict) -> Dict:
    """Compare retrieval snapshots without manufacturing hypothesis support."""
    old_files = set(hypothesis.get("supporting_files", []))
    new_files = set(n.source_file for n in new_chain.nodes if n.node_type == "source")

    overlap = old_files & new_files
    union = old_files | new_files

    jaccard = len(overlap) / max(len(union), 1)
    old_conf = hypothesis.get("confidence")

    return {
        "validation_status": "requires_review",
        "previous_confidence": old_conf,
        "new_confidence": old_conf,
        "jaccard_overlap": round(jaccard, 4),
        "new_chain_confidence": new_chain.confidence,
        "validation_stage": "retrieval_comparison_only",
        "message": "候选来源重叠只描述检索变化；需核实连接键、规则和重算结果，不能自动提高或降低假说置信度。",
    }
