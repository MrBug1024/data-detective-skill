"""Conservative, cited record grouping; never an automatic identity decision."""
import hashlib
import re
from collections import defaultdict

from indexer import value_text
from matcher import is_identifier_field


def identity_fields(units, target, configured=None):
    fields = sorted({f for u in units for f in u.field_names})
    if configured is not None:
        if not isinstance(configured, list) or any(not isinstance(f, str) or not f.strip() for f in configured):
            raise ValueError("entity_identity_fields 必须为字段名列表")
        return configured
    # An identifier target explicitly selects its own namespace. Names select
    # only person/organization identifiers, never order/payment IDs by proximity.
    exact_fields = {f for u in units for f, v in zip(u.field_names, u.field_values)
                    if v is not None and value_text(v).casefold() == target.casefold()
                    and is_identifier_field(f)}
    if exact_fields:
        return sorted(exact_fields)
    personal = re.compile(r"^(?:身份证(?:号|号码)?|证件(?:号|号码|编号)|统一社会信用(?:代码|号)?|注册号|客户(?:ID|编号|号)|用户(?:ID|编号|号)|员工(?:ID|编号|号)|工号)$|"
                          r"(?:^|_)(?:person|customer|user|employee|company|organization|org)_?(?:id|code|number)$", re.I)
    return [f for f in fields if personal.search(f)]


def candidate_groups(units, target, configured=None):
    selected_fields = identity_fields(units, target, configured)
    groups = {}
    identifiers = defaultdict(set)
    for unit in units:
        claims = [{"field": f, "value": value_text(v), "evidence_id": "ev_" + unit.unit_id}
                  for f, v in zip(unit.field_names, unit.field_values)
                  if f in selected_fields and v is not None and value_text(v)]
        key = tuple(sorted((c["field"], c["value"]) for c in claims)) or (("unresolved_record", unit.unit_id),)
        gid = "candidate_" + hashlib.sha256(repr(key).encode()).hexdigest()[:16]
        group = groups.setdefault(gid, {"candidate_id": gid, "name_mention": target,
            "identity_status": "unverified_identifier_group" if claims else "unresolved_record",
            "identifier_claims": [], "attribute_claims": [], "evidence_ids": []})
        group["identifier_claims"].extend(claims)
        group["evidence_ids"].append("ev_" + unit.unit_id)
        group["attribute_claims"].extend({"field": f, "value": value_text(v),
            "evidence_id": "ev_" + unit.unit_id} for f, v in zip(unit.field_names, unit.field_values) if v is not None)
        for claim in claims:
            identifiers[claim["field"]].add(claim["value"])
    return list(groups.values()), identifiers, selected_fields
