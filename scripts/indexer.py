"""
indexer.py — 为业务源文件构建可快速查询的索引
支持：精确值索引、倒排索引（词级）、规则节点索引
"""
import os
import json
import hashlib
import pickle
import re
import math
from collections import defaultdict
from typing import List, Dict, Any, Optional, Set
from pathlib import Path

from parsers import AddressableUnit, FileParser
from ingestion_support import sha256_file


def value_text(value: Any) -> str:
    """Normalize numeric scalars without stripping meaningful ID zero prefixes."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value).strip()


class SourceIndex:
    """
    业务源数据索引，支持：
    - exact_index: value → [unit_id, ...]  精确值匹配
    - token_index: token → [unit_id, ...]  词级倒排索引
    - field_index: fieldname → [unit_id, ...]  字段名索引
    - rule_units: 规则节点列表
    - id_map: unit_id → AddressableUnit
    """

    def __init__(self):
        self.exact_index: Dict[str, List[str]] = defaultdict(list)  # value → [unit_id]
        self.token_index: Dict[str, List[str]] = defaultdict(list)  # token → [unit_id]
        self.field_index: Dict[str, List[str]] = defaultdict(list)  # fieldname → [unit_id]
        self.file_units: Dict[str, List[str]] = defaultdict(list)   # filepath → [unit_id]
        self.rule_units: List[str] = []                              # [unit_id] for rules
        self.id_map: Dict[str, AddressableUnit] = {}
        self.ingestion_status: Dict[str, Dict[str, Any]] = {}

    def add_unit(self, unit: AddressableUnit):
        uid = unit.unit_id
        if uid in self.id_map:
            return
        self.id_map[uid] = unit
        self.file_units[os.path.normcase(os.path.abspath(unit.source_file))].append(uid)

        if unit.is_rule:
            self.rule_units.append(uid)

        # 建立精确值索引（对每个非空字段值）
        exact_values = set()
        tokens = set()
        for val in unit.field_values:
            if val is None:
                continue
            v_str = value_text(val)
            if v_str:
                # 精确匹配
                exact_values.add(v_str)
                # 小写精确
                v_lower = v_str.lower()
                if v_lower != v_str:
                    exact_values.add(v_lower)

                # Token级索引
                tokens.update(t for t in _tokenize(v_str) if len(t) >= 2)
        for value in sorted(exact_values):
            self.exact_index[value].append(uid)
        for token in sorted(tokens):
            self.token_index[token].append(uid)

        # 字段名索引
        for fname in unit.field_names:
            fn_norm = fname.lower().strip()
            if fn_norm:
                self.field_index[fn_norm].append(uid)

    def lookup_exact(self, value: str) -> List[AddressableUnit]:
        """精确值查找"""
        key = value_text(value)
        uids = set(self.exact_index.get(key, []))
        uids |= set(self.exact_index.get(key.lower(), []))
        return [self.id_map[uid] for uid in sorted(uids) if uid in self.id_map]

    def lookup_tokens(self, text: str, min_token_len: int = 2) -> Dict[str, int]:
        """Token查找，返回 {unit_id: match_count}"""
        tokens = sorted({t for t in _tokenize(text) if len(t) >= min_token_len})
        counts: Dict[str, int] = defaultdict(int)
        for token in tokens:
            for uid in self.token_index.get(token, []):
                counts[uid] += 1
        return dict(counts)

    def get_unit(self, unit_id: str) -> Optional[AddressableUnit]:
        return self.id_map.get(unit_id)

    def get_file_units(self, filepath: str) -> List[AddressableUnit]:
        uids = self.file_units.get(os.path.normcase(os.path.abspath(filepath)), [])
        return [self.id_map[uid] for uid in uids if uid in self.id_map]

    def get_rule_units(self) -> List[AddressableUnit]:
        return [self.id_map[uid] for uid in self.rule_units if uid in self.id_map]

    def stats(self) -> Dict[str, Any]:
        return {
            "total_units": len(self.id_map),
            "total_files": len(self.file_units),
            "rule_units": len(self.rule_units),
            "exact_index_keys": len(self.exact_index),
            "token_index_keys": len(self.token_index),
        }

    def save(self, path: str):
        with open(path, 'wb') as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: str) -> 'SourceIndex':
        with open(path, 'rb') as f:
            return pickle.load(f)


def _tokenize(text: str) -> List[str]:
    """Literal tokens plus Chinese 2/3-grams; never detach digits from IDs.

    Grams support names embedded in unsegmented Chinese text. They retrieve
    candidates only: overlap is not a semantic or identity determination.
    """
    result = set()
    for token in re.findall(r'[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*|[\u4e00-\u9fff]+', str(text)):
        token = token.lower()
        if len(token) <= 128:
            result.add(token)
        if re.fullmatch(r'[\u4e00-\u9fff]+', token):
            for width in (2, 3):
                result.update(token[i:i + width] for i in range(len(token) - width + 1))
    return sorted(result)


def build_index(source_files: List[str], cache_dir: Optional[str] = None) -> SourceIndex:
    """
    构建源文件索引
    cache_dir: 若指定，则尝试加载缓存（按文件修改时间）
    """
    # 计算缓存key
    if cache_dir:
        cache_key = _cache_key(source_files)
        cache_path = os.path.join(cache_dir, f"index_{cache_key}.pkl")
        if os.path.exists(cache_path):
            try:
                idx = SourceIndex.load(cache_path)
                print(f"[INFO] Loaded index from cache: {cache_path}")
                return idx
            except Exception as e:
                print(f"[WARN] Cache load failed: {e}, rebuilding...")

    parser = FileParser()
    index = SourceIndex()

    seen_files = set()
    for filepath in source_files:
        identity = os.path.normcase(os.path.abspath(filepath))
        if identity in seen_files:
            continue
        seen_files.add(identity)
        if not os.path.exists(filepath):
            print(f"[WARN] File not found: {filepath}")
            index.ingestion_status[identity] = {'status': 'unavailable', 'units': 0,
                                               'issues': [{'code': 'missing_file', 'severity': 'error', 'message': '来源文件不存在，未检索'}]}
            continue
        units = parser.parse(filepath)
        issues = parser.last_diagnostics
        status = 'unavailable' if not units else 'partial' if any(i.get('severity') in ('warning', 'error') for i in issues) else 'complete'
        index.ingestion_status[identity] = {'status': status, 'units': len(units), 'issues': issues}
        for unit in units:
            index.add_unit(unit)

    print(f"[INFO] Index built: {json.dumps(index.stats())}")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        index.save(cache_path)
        print(f"[INFO] Index cached to: {cache_path}")

    return index


def _cache_key(source_files: List[str]) -> str:
    """基于规范路径、精确时间戳、大小及内容哈希计算缓存key。"""
    # Bump when parser/tokenization behavior changes; old indexes must not leak
    # obsolete matching behavior into a revised engine.
    parts = ["index-v10-multiformat-structured-rows-source-bound-ocr"]
    paths = {os.path.normcase(os.path.abspath(p)) for p in source_files}
    # Sidecar presence, removal and edits are part of the actual source
    # snapshot; a valid original-file cache must not hide changed OCR text.
    paths |= {p + '.ocr.json' for p in paths}
    for fp in sorted(paths):
        stat = os.stat(fp) if os.path.exists(fp) else None
        digest = 'missing'
        if stat and os.path.isfile(fp):
            digest = sha256_file(fp)
        parts.append(f"{fp}:{stat.st_mtime_ns if stat else 0}:{stat.st_size if stat else 0}:{digest}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]
