"""Build an auditable criminal case packet; substantive inference belongs to the agent.

No offender profiles, causal edges, identity decisions or probabilities are inferred
from keyword hits. All evidence text is a source claim pending verification.
"""
import hashlib
from collections import Counter
from pathlib import Path
from ingestion_support import sha256_file


def _location(unit):
    return {"file": unit.source_file, "sheet": unit.sheet_name,
            "row": unit.row_num, "page": unit.page_num,
            "paragraph": unit.paragraph_num, "char_offset": unit.char_offset}


def build_case_packet(target, units, index, extractor, config=None):
    config = config or {}
    limit = config.get("criminal_max_units", 100)
    char_limit = config.get("criminal_max_chars", 1600)
    if (type(limit) is not int or not 1 <= limit <= 1000 or
            type(char_limit) is not int or not 100 <= char_limit <= 20000):
        raise ValueError("criminal_max_units 必须为 1..1000，criminal_max_chars 为 100..20000")
    hits = index.lookup_tokens(target)
    ordered = sorted(units, key=lambda u: (-hits.get(u.unit_id, 0), u.source_file,
                                          u.sheet_name or "", u.row_num or 0,
                                          u.page_num or 0, u.paragraph_num or 0))
    selected = ordered[:limit]
    file_hashes = {}
    for filename in dict.fromkeys(u.source_file for u in units):
        file_hashes[filename] = sha256_file(filename)
    ledger, nodes, timeline = [], [], []
    group_counts = Counter()
    truncated = []
    for unit in selected:
        eid, nid = "ev_" + unit.unit_id, "src_" + unit.unit_id
        # Exact copies share a group. Different text is NOT presumed independent.
        fingerprint = hashlib.sha256(" ".join(unit.raw_text.split()).encode()).hexdigest()
        group = "copy_" + fingerprint[:16]
        group_counts[group] += 1
        text = unit.raw_text[:char_limit]
        if len(unit.raw_text) > char_limit:
            truncated.append(eid)
        ledger.append({"evidence_id": eid, "node_id": nid,
                       "kind": "source_statement", "verification": "unverified",
                       "source_location": _location(unit), "value": text,
                       "content_sha256": fingerprint,
                       "file_sha256": file_hashes[unit.source_file],
                       "dependency_group": group, "independence": "unknown",
                       "collection_time": None, "collector": None,
                       "custody_status": "not_provided", "original_or_copy": "unknown",
                       "ocr_confidence": unit.ocr_confidence,
                       "relevance_score": hits.get(unit.unit_id, 0),
                       "text_truncated": len(unit.raw_text) > char_limit})
        nodes.append({"node_id": nid, "node_type": "source",
                      "label": f"{Path(unit.source_file).name} #{unit.row_num or unit.paragraph_num or unit.page_num}",
                      "source_file": unit.source_file, "file_type": unit.file_type,
                      "row_num": unit.row_num, "page_num": unit.page_num,
                      "paragraph_num": unit.paragraph_num, "sheet_name": unit.sheet_name,
                      "field_names": unit.field_names,
                      "field_values": [str(v) for v in unit.field_values],
                      "raw_text_snippet": text, "confidence": None,
                      "is_rule": unit.is_rule, "metadata": {"evidence_id": eid}})
        entities, events = extractor.extract_from_unit(unit)
        entries = [event.to_dict() for event in events]
        covered_times = {event["time"] for event in entries}
        # A date mention remains useful even without a recognized business verb.
        # Label it as a mention, never invent an event or attach it to another fact.
        entries.extend({"time": entity.name, "time_normalized": entity.value,
                        "event": entity.raw_text, "event_type": "time_mention",
                        "source": _location(unit)}
                       for entity in entities
                       if entity.entity_type == "date" and entity.name not in covered_times)
        for entry in entries:
            entry.update({"evidence_ids": [eid], "time_basis": "source_claim",
                          "clock_offset_seconds": None, "timezone": None,
                          "precision": "unverified", "confidence": None})
            timeline.append(entry)
    # Preserve every source event; chronological display is not a causal graph.
    timeline.sort(key=lambda t: (t.get("time_normalized") or t.get("time", ""),
                                 t["evidence_ids"][0]))
    missing = [
        {"type": "provenance", "reason": "核实原始记录、采集人员/时间、保管流转与污染/二次转移可能；文件哈希不证明内容真实。"},
        {"type": "agent_analysis", "reason": "待 Agent 读取刑侦参考，构建竞争假说、反证矩阵与可执行调查计划。"},
    ]
    if len(units) > limit or truncated:
        missing.append({"type": "context_truncated", "reason": "须按来源补读遗漏材料后再作案件结论。",
                        "omitted_units": len(units) - len(selected),
                        "truncated_evidence_ids": truncated})
    return {"analysis_stage": "evidence_packet_only", "confidence": None,
            "confidence_semantics": "not_assessed; relevance is not evidential strength or guilt probability",
            "evidence": ledger, "chain": {"nodes": nodes, "edges": []},
            "timeline": timeline, "hypotheses": [], "ach_matrix": [],
            "investigation_plan": [], "conclusions": [], "missing": missing,
            "conflicts": [], "source_locations": [_location(u) for u in selected],
            "provenance": {"source_sha256": file_hashes,
                           "exact_copy_groups": {g: n for g, n in group_counts.items() if n > 1},
                           "dependency_status": "nonidentical_sources_may_still_be_dependent"},
            "coverage": {"total_units": len(units), "included_units": len(selected),
                         "omitted_units": len(units) - len(selected),
                         "truncated_evidence_ids": truncated,
                         "selection": "keyword_priority_then_source_order; includes_context"},
            "analysis_requirements": ["references/criminal-workflow.md",
                                      "references/forensic-science.md",
                                      "references/investigation-methods.md"]}
