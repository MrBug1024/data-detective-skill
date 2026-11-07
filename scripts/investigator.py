"""Source-addressable extraction helpers retained for library compatibility."""
import datetime
import json
import os
import re
import uuid
import hashlib
from pathlib import Path

from parsers import FileParser
from indexer import build_index
from matcher import MatchConfig
from entity_extractor import EntityExtractor
from entity_resolution import candidate_groups
from ingestion_support import sha256_file


def unit_location(unit):
    """Keep parser-specific addresses (cells, SQL table, OCR box) with the citation."""
    return {"file": unit.source_file, "sheet": unit.sheet_name,
            "row": unit.row_num, "page": unit.page_num,
            "paragraph": unit.paragraph_num, "char_offset": unit.char_offset,
            "metadata": unit.metadata, "ocr_confidence": unit.ocr_confidence}


class Investigation:
    def __init__(self):
        self.investigation_id = str(uuid.uuid4())
        self.target = ""
        self.timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.confidence = None
        self.confidence_semantics = "not_assessed"
        self.conclusions = []
        self.entities = []
        self.relationships = []
        self.timeline = []
        self.chain = {"nodes": [], "edges": []}
        self.evidence = []
        self.anomalies = []
        self.missing = []
        self.conflicts = []
        self.source_locations = []
        self._source_files = []

    def to_dict(self):
        def serialize(value):
            if hasattr(value, "to_dict"):
                return serialize(value.to_dict())
            if isinstance(value, dict):
                return {k: serialize(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [serialize(v) for v in value]
            return value
        return serialize(vars(self))

    def save_json(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2, default=str, allow_nan=False)


class Investigator:
    def __init__(self, config=None, cache_dir=".dd_cache", verbose=False):
        self.config = config or {}
        self.cache_dir = cache_dir
        self.verbose = verbose
        self.match_config = MatchConfig(self.config)
        self.fp = FileParser()
        self.extractor = EntityExtractor()
        self._index = None
        self._source_files = []
        self._all_units = []
        self._source_issues = []

    def load_sources(self, source_files):
        # Re-enter the index cache on every request to notice changed source files.
        # Reuse parsed index units rather than parsing each file a second time.
        self._source_files = list(dict.fromkeys(os.path.abspath(p) for p in source_files))
        self._index = build_index(self._source_files, cache_dir=self.cache_dir)
        self._all_units = list(self._index.id_map.values())
        self._source_issues = []
        statuses = getattr(self._index, "ingestion_status", {})
        for filename in self._source_files:
            if not os.path.isfile(filename):
                self._source_issues.append({"type": "missing_source", "reason": filename})
            elif not self._index.get_file_units(filename):
                self._source_issues.append({"type": "empty_or_unsupported_source", "reason": filename})
            status = statuses.get(filename, statuses.get(os.path.normcase(filename), {}))
            if status.get("status") in ("partial", "unavailable"):
                self._source_issues.append({"type": "ingestion_incomplete", "reason": filename,
                                            "status": status["status"], "issues": status.get("issues", [])})
        return self._index

    def _report(self, target):
        inv = Investigation()
        inv.target = target
        inv._source_files = list(self._source_files)
        inv._config = self.config
        inv.missing = list(self._source_issues)
        inv.ingestion = getattr(self._index, "ingestion_status", {})
        snapshots = {}
        derived = {}
        for filename in self._source_files:
            if os.path.isfile(filename):
                snapshots[filename] = sha256_file(filename)
                sidecar = Path(filename + '.ocr.json')
                if sidecar.is_file():
                    derived[str(sidecar)] = sha256_file(sidecar)
        inv.provenance = {"source_sha256": snapshots,
                          "derived_source_sha256": derived,
                          "dependency_status": "hashes_verify_bytes_not_truth_or_independence"}
        return inv

    def trace_data(self, result_file, result_row, source_files, result_sheet=None):
        from trace_pipeline import trace_sources
        inv = Investigation()
        inv.__dict__.update(trace_sources(result_file, source_files, row=result_row,
            result_sheet=result_sheet, cache_dir=self.cache_dir, config=self.config))
        return inv

    @staticmethod
    def _timeline(events):
        unique = {(t.get("time", ""), t.get("event", ""), t.get("source", "")): t for t in events}
        return sorted(unique.values(), key=lambda t: t.get("time_normalized") or t.get("time", ""))

    @staticmethod
    def _contains(target, text):
        if re.fullmatch(r"[\w .@-]+", target, flags=re.ASCII):
            return re.search(r"(?<![A-Za-z0-9_])" + re.escape(target) + r"(?![A-Za-z0-9_])", text, re.I) is not None
        return target in text

    def investigate_entity(self, target_name, source_files):
        if not target_name.strip():
            raise ValueError("调查目标不能为空")
        self.load_sources(source_files)
        inv = self._report(target_name)
        exact = self._index.lookup_exact(target_name)
        exact_ids = {u.unit_id for u in exact}
        candidates = self._index.lookup_tokens(target_name)
        relevant = {u.unit_id: u for u in exact}
        for uid in candidates:
            unit = self._index.get_unit(uid)
            if unit and self._unit_mentions(target_name, unit):
                relevant[uid] = unit
        units = sorted(relevant.values(), key=lambda u: (u.unit_id not in exact_ids,
                       u.source_file, u.sheet_name or "", u.row_num or 0, u.page_num or 0, u.paragraph_num or 0))
        limit = self.config.get("entity_max_units", 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("entity_max_units 必须为 1..1000")
        start = self.config.get("entity_start", 0)
        char_limit = self.config.get("entity_max_chars", 1600)
        if type(start) is not int or start < 0:
            raise ValueError("entity_start 必须为非负整数")
        if type(char_limit) is not int or not 100 <= char_limit <= 20000:
            raise ValueError("entity_max_chars 必须为 100..20000")
        selected = units[start:start + limit]
        groups, identity_ids, group_fields = candidate_groups(
            selected, target_name, self.config.get("entity_identity_fields"))
        inv.candidate_entities = groups
        inv.identity_grouping = {"fields": group_fields,
            "scope": "returned_page_only", "semantics": "same_identifier_claims_not_verified_identity",
            "identifier_group_count": sum(g['identity_status'] == 'unverified_identifier_group' for g in groups),
            "unresolved_record_count": sum(g['identity_status'] == 'unresolved_record' for g in groups),
            "confirmed_identity_count": 0}
        inv.analysis_stage = "mentions_only_identity_unresolved"
        inv.confidence_semantics = "not_assessed; mention_match_does_not_resolve_identity"
        attrs = {}
        truncated = []
        for unit in selected:
            eid, nid = "ev_" + unit.unit_id, "src_" + unit.unit_id
            location = unit_location(unit)
            if len(unit.raw_text) > char_limit:
                truncated.append(eid)
            inv.source_locations.append(location)
            inv.evidence.append({"evidence_id": eid, "node_id": nid,
                                 "match_type": "direct" if unit.unit_id in exact_ids else "text_mention",
                                 "value": target_name, "matched_value": unit.raw_text[:char_limit],
                                 "source_location": location, "verification": "unverified",
                                 "text_truncated": len(unit.raw_text) > char_limit,
                                 "file_sha256": inv.provenance["source_sha256"].get(unit.source_file),
                                 "content_sha256": hashlib.sha256(unit.raw_text.encode()).hexdigest(),
                                 "independence": "unknown"})
            inv.chain["nodes"].append({"node_id": nid, "node_type": "source",
                                       "label": Path(unit.source_file).name,
                                       "source_file": unit.source_file, "file_type": unit.file_type,
                                       "row_num": unit.row_num, "page_num": unit.page_num,
                                       "paragraph_num": unit.paragraph_num, "sheet_name": unit.sheet_name,
                                       "field_names": unit.field_names,
                                       "field_values": [str(v) for v in unit.field_values],
                                       "raw_text_snippet": unit.raw_text[:char_limit], "confidence": None,
                                       "metadata": unit.metadata})
            entities, events = self.extractor.extract_from_unit(unit)
            for entity in entities:
                claim = entity.to_dict()
                claim.update(confidence_semantics="heuristic_extraction_score_not_identity", verification="unverified")
                inv.entities.append(claim)
            for event in events:
                claim = event.to_dict()
                claim.update(confidence_semantics="heuristic_extraction_score_not_event_truth", verification="unverified")
                inv.timeline.append(claim)
            for relationship in self.extractor.extract_relationships_from_row(unit, entities):
                claim = relationship.to_dict()
                claim.update(confidence=None, evidence_ids=[eid],
                             verification="unverified", relationship_stage="row_claim_or_cooccurrence")
                inv.relationships.append(claim)
            for field, value in zip(unit.field_names, unit.field_values):
                if value is None:
                    continue
                if any(k in field.lower() for k in ("城市", "地址", "电话", "状态", "city", "phone", "等级")):
                    attrs.setdefault(field, []).append({"value": str(value), "evidence_id": eid})
        for field, values in identity_ids.items():
            if len(values) > 1:
                inv.conflicts.append({"type": "identity_unresolved", "field": field,
                                      "candidates": sorted(values),
                                      "reason": "同名记录含不同标识，不能合并为同一实体"})
        if selected:
            inv.entities.insert(0, {"entity_id": "target_entity", "name": target_name,
                                    "type": "unresolved", "confidence": None,
                                    "identity_status": "unresolved", "attribute_claims": attrs})
        inv.timeline = self._timeline(inv.timeline)
        inv.coverage = {"matched_units": len(units), "included_units": len(selected),
                        "omitted_units": len(units) - len(selected), "selection_start": start,
                        "next_start": start + limit if start + limit < len(units) else None,
                        "truncated_evidence_ids": truncated,
                        "ingestion_complete": not self._source_issues}
        if not units:
            inv.missing.append({"type": "no_mentions", "reason": "所供材料未检出目标；不代表目标不存在或无关联。"})
        if len(units) > len(selected) or truncated:
            inv.missing.append({"type": "context_truncated", "reason": "有遗漏记录，须分页补读。"})
        inv.missing.append({"type": "identity_verification", "reason": "用稳定标识及有效时间核实同名/别名；共现不证明真实关系。"})
        inv.conclusions = [{"id": "c1", "statement": f"检出 {len(units)} 处名称/标识提及；已返回 {len(selected)} 处，同一身份尚未认定。",
                            "status": "tentative", "confidence": None,
                            "supporting_evidence": [e["evidence_id"] for e in inv.evidence],
                            "counter_evidence": [], "missing_evidence": [m["reason"] for m in inv.missing]}]
        return inv

    @classmethod
    def _unit_mentions(cls, target, unit):
        # An exact structured name is searchable; a longer name containing the
        # target characters is a different name. Free text remains a mention.
        structured = unit.file_type in ("csv", "tsv", "xlsx", "xls") or any(
            unit.metadata.get(k) for k in ("structured", "structural"))
        if not structured:
            return cls._contains(target, unit.raw_text)
        for field, value in zip(unit.field_names, unit.field_values):
            if value is None:
                continue
            text = str(value)
            if re.search(r"姓名|名称|(?:^|_)name$", field, re.I):
                if target.casefold() == text.strip().casefold():
                    return True
            elif cls._contains(target, text):
                return True
        return False
