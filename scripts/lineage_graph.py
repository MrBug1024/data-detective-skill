"""Compact, typed dependencies from evidence actually recorded by the pipeline.

DIRECT/INDIRECT distinguish value correspondence from lookup keys. These are
reconstructions, not a PROV/OpenLineage conformance claim or execution history.
"""
from __future__ import annotations
import hashlib
import json


def seed_reference(seed):
    """One address per typed claim and actual anchor path, not per document."""
    identity = {key: seed.get(key) for key in ('evidence_id', 'identifier_namespace', 'value',
        'scope', 'bridge_id', 'anchor', 'provenance')}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return 'seed_' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]


def build_dependencies(report):
    rows = report.get('result_rows', [])
    records = {m['source_ref']: m for row in rows for m in row.get('source_matches', [])}
    fields, joins, issues, documents = [], [], [], {}
    for mapping in report.get('field_mappings', []):
        row_num = mapping.get('result_row')
        for source in mapping.get('sources', []):
            ref = source.get('source_ref')
            if ref not in records:
                issues.append({'code': 'unknown_source_reference', 'source_ref': ref})
                continue
            fields.append({'result_row': row_num, 'result_field': mapping.get('result_field'),
                'source_ref': ref, 'source_field': source.get('source_field'),
                'source_address': source.get('address'), 'dependency_type': 'DIRECT',
                'relationship': 'value_correspondence', 'status': mapping.get('status'),
                'historical_derivation': 'not_proven'})
    for row in rows:
        row_refs = {m['source_ref'] for m in row.get('source_matches', [])}
        for seed in row.get('document_lookup_seeds', []):
            if seed.get('can_seed_lookup') is True and seed.get('evidence_id') and seed.get('source_location'):
                reference = seed.get('seed_ref') or seed_reference(seed)
                documents[reference] = {'source_location': seed['source_location'], 'evidence_id': seed['evidence_id'],
                    'identifier_namespace': seed.get('identifier_namespace'), 'value': seed.get('value'), 'scope': seed.get('scope'),
                    'bridge_id': seed.get('bridge_id'), 'provenance': seed.get('provenance'),
                    'anchor_key_provenance': seed.get('anchor_key_provenance'),
                    'relationship': 'document_claim_used_for_lookup'}
        for match in row.get('source_matches', []):
            for key in match.get('key_provenance', []):
                for origin in key.get('origins', []):
                    ref = origin.get('origin')
                    if ref != 'result' and ref not in row_refs and ref not in documents:
                        issues.append({'code': 'unresolved_key_origin', 'source_ref': ref,
                                       'result_row': row.get('row_num')})
                        continue
                    joins.append({'result_row': row.get('row_num'), 'source_ref': match['source_ref'],
                        'source_field': key.get('source_field'), 'known_source_ref': ref,
                        'known_field': origin.get('field'), 'dependency_type': 'INDIRECT',
                        'known_identifier_namespace': origin.get('identifier_namespace'),
                        'relationship': 'reconstructed_key_join', 'status': match.get('status'),
                        'known_reference': key.get('known_reference'), 'historical_derivation': 'not_proven'})
    return {'schema_version': '1.0', 'semantics': 'reconstructed_record_and_field_correspondence',
            'field_dependencies': fields, 'key_dependencies': joins, 'document_origins': documents, 'issues': issues,
            'references_resolved': not issues, 'historical_lineage': 'not_proven'}
