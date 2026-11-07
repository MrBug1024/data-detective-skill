"""Conservative, cited record grouping; never an automatic identity decision."""
import hashlib
import re
from collections import defaultdict
from itertools import combinations, islice

from indexer import value_text
from matcher import is_identifier_field
from document_entities import identifier_namespace, _structured_scope


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
        scope = _structured_scope(dict(zip(unit.field_names, unit.field_values)))
        key = ((tuple(sorted((c["field"], c["value"]) for c in claims)), tuple(sorted(scope['values'].items())))
               if claims and not scope['ambiguous'] else (("unresolved_record", unit.unit_id),))
        gid = "candidate_" + hashlib.sha256(repr(key).encode()).hexdigest()[:16]
        group = groups.setdefault(gid, {"candidate_id": gid, "name_mention": target,
            "identity_status": "unverified_identifier_group" if claims else "unresolved_record",
            "scope": scope,
            "identifier_claims": [], "attribute_claims": [], "evidence_ids": []})
        group["identifier_claims"].extend(claims)
        group["evidence_ids"].append("ev_" + unit.unit_id)
        group["attribute_claims"].extend({"field": f, "value": value_text(v),
            "evidence_id": "ev_" + unit.unit_id} for f, v in zip(unit.field_names, unit.field_values) if v is not None)
        for claim in claims:
            identifiers[claim["field"]].add(claim["value"])
    return list(groups.values()), identifiers, selected_fields


def candidate_mention_links(mentions, *, max_pairs=10000):
    """Block on exact typed values, then preserve scope/claim conflicts.

    These are candidates for lookup or review; the function does not union
    entities or infer a calibrated match probability. Missing scope stays an
    uncertainty instead of silently making identifiers globally unique.
    """
    if type(max_pairs) is not int or max_pairs < 1:
        raise ValueError('max_pairs must be a positive integer')
    blocks = defaultdict(list)
    contexts = defaultdict(lambda: defaultdict(set))
    excluded = defaultdict(int)
    for mention in mentions:
        namespace = mention.get('identifier_namespace')
        if mention.get('entity_type') != 'identifier' or not namespace or namespace == 'unspecified':
            excluded['non_identifying_or_unspecified_namespace'] += 1
            continue
        value = mention.get('normalized_value')
        if not isinstance(value, str) or not value:
            excluded['invalid_identifier_value'] += 1
            continue
        blocks[(namespace, value)].append(mention)
        context = mention.get('context', {}).get('context_id')
        if context:
            contexts[context][namespace].add(value)
    pairs, omitted, considered = [], 0, 0
    for (namespace, value), block in blocks.items():
        possible = len(block) * (len(block) - 1) // 2
        considered += possible
        room = max_pairs - len(pairs)
        omitted += max(0, possible - room)
        if room <= 0:
            continue
        for left, right in islice(combinations(block, 2), room):
            lscope, rscope = left.get('scope', {}), right.get('scope', {})
            lv, rv = lscope.get('values', {}), rscope.get('values', {})
            shared = set(lv) & set(rv)
            conflicts = [{'scope': key, 'left': lv[key], 'right': rv[key]} for key in shared if lv[key] != rv[key]]
            lcontext = contexts.get(left.get('context', {}).get('context_id'), {})
            rcontext = contexts.get(right.get('context', {}).get('context_id'), {})
            context_conflicts = [{'namespace': key, 'left': sorted(lcontext[key]), 'right': sorted(rcontext[key])}
                                 for key in set(lcontext) & set(rcontext) if key != namespace and lcontext[key].isdisjoint(rcontext[key])]
            lclaim = left.get('assertion', {}).get('status', 'unassessed')
            rclaim = right.get('assertion', {}).get('status', 'unassessed')
            same_context = bool(left.get('context', {}).get('context_id') and
                                left.get('context', {}).get('context_id') == right.get('context', {}).get('context_id'))
            if conflicts or lscope.get('ambiguous') or rscope.get('ambiguous'):
                status = 'scope_conflict'
            elif context_conflicts:
                status = 'context_conflict'
            elif lclaim != 'affirmed' or rclaim != 'affirmed':
                status = 'nonaffirmative_claim'
            elif left.get('ocr', {}).get('review_required') or right.get('ocr', {}).get('review_required'):
                status = 'ocr_review_required'
            elif not same_context and (not shared or set(lv) != set(rv)):
                status = 'scope_unresolved'
            else:
                status = 'compatible_identifier_claims'
            reference = [left['mention_id'], right['mention_id']]
            same_snapshot = bool(left.get('source_sha256') and left.get('source_sha256') == right.get('source_sha256'))
            pairs.append({'candidate_id': 'link_' + hashlib.sha256(repr(reference).encode()).hexdigest()[:20],
                          'left_mention': left['mention_id'], 'right_mention': right['mention_id'],
                          'identifier_namespace': namespace, 'literal_value': value, 'status': status,
                          'scope_conflicts': conflicts, 'context_conflicts': context_conflicts,
                          'can_seed_lookup': status == 'compatible_identifier_claims',
                          'identity_verified': False, 'identity_merge_allowed': False,
                          'independence': 'same_source_snapshot' if same_snapshot else 'not_assessed',
                          'evidence_locations': [left.get('source_location'), right.get('source_location')]})
    return {'pairs': pairs, 'coverage': {'blocking': 'exact_typed_literal_identifier',
                'input_mentions': len(mentions), 'blocks': len(blocks), 'candidate_pairs': considered,
                'returned_pairs': len(pairs), 'omitted_pairs': omitted, 'complete': not omitted,
                'excluded_mentions': dict(excluded)},
            'issues': ([{'code': 'entity_pair_budget', 'severity': 'warning', 'omitted_pairs': omitted}] if omitted else [])}
