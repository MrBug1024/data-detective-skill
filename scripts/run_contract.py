"""Machine-readable lineage completion contract; an artifact does not establish a claim."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def artifact(path):
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': str(path), 'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}


def assess(report):
    """Conservative stage result, independent from historical lineage claims."""
    base = {'state': 'partial', 'outcome': 'incomplete', 'exit_code': 2,
            'lineage_status': 'not_proven', 'agent_action': 'inspect_coverage_and_missing_evidence'}
    if report.get('status') in ('error', 'invalid_or_unavailable'):
        return {**base, 'state': 'failed', 'outcome': 'execution_error', 'exit_code': 1,
                'agent_action': 'repair_input_or_environment_then_retry'}
    stage = report.get('analysis_stage')
    if stage == 'source_index_preparation':
        if report.get('complete') is True and report.get('snapshot_path') and report.get('source_sha256'):
            return {**base, 'state': 'completed', 'outcome': 'index_prepared', 'exit_code': 0,
                    'agent_action': 'trace_or_query_with_prepared_source_index'}
        return base
    if stage == 'evidence_search':
        evidence = report.get('related_evidence', report.get('evidence', []))
        sources = report.get('sources', [])
        cov = report.get('coverage', {})
        result = {**base, 'source_localization': {'status': 'evidence_only' if evidence else 'not_found'},
                  'document_localization': {'status': 'passages_found' if evidence else 'not_found'},
                  'calculation_verification': {'status': 'not_checked'},
                  'rule_applicability': {'status': 'not_verified'},
                  'historical_lineage': {'status': 'not_proven'}}
        if sources and all(source.get('status') in ('unavailable', 'error', 'failed') for source in sources):
            return {**result, 'state': 'failed', 'outcome': 'execution_error', 'exit_code': 1,
                    'agent_action': 'repair_unavailable_sources_then_retry'}
        if not evidence:
            return {**result, 'outcome': 'no_matches', 'agent_action': 'review_identifier_scope_or_add_sources'}
        if isinstance(report.get('source_trace'), dict):
            traced = assess(report['source_trace'])
            for dimension in ('source_localization', 'calculation_verification', 'rule_applicability', 'historical_lineage'):
                if dimension in traced:
                    result[dimension] = traced[dimension]
            if (traced['outcome'] == 'sources_located' and cov.get('retrieval_complete') is True
                    and cov.get('truncated') is False):
                return {**result, 'state': 'completed', 'outcome': 'sources_located', 'exit_code': 0,
                        'agent_action': 'deliver_cited_document_bridges_and_located_records'}
            return {**result, 'outcome': 'partial_source_trace',
                    'agent_action': 'review_document_bridge_scope_and_remaining_source_matches'}
        if cov.get('retrieval_complete') is True and cov.get('truncated') is False:
            return {**result, 'state': 'completed', 'outcome': 'evidence_located', 'exit_code': 0,
                    'agent_action': 'review_cited_passages_and_confirm_linking_fields'}
        return {**result, 'outcome': 'source_evidence_found',
                'agent_action': 'inspect_evidence_coverage_and_resolve_remaining_sources'}
    if stage == 'source_trace':
        localization = report.get('source_localization', {})
        if isinstance(localization, str):
            localization = {'status': localization}
        dimensions = {name: report.get(name, {'status': 'not_checked'}) for name in (
            'source_localization', 'document_localization', 'calculation_verification', 'rule_applicability', 'historical_lineage')}
        result = {**base, **dimensions, 'agent_action': 'inspect_source_matches_and_next_actions'}
        if report.get('lineage_dependencies', {}).get('references_resolved') is False:
            return {**result, 'outcome': 'partial_source_trace',
                    'agent_action': 'repair_unresolved_evidence_references'}
        rows = report.get('result_rows', [])
        cov = report.get('coverage', {})
        if (cov.get('source_files_requested', 0) > 0
                and cov.get('source_files_unavailable') == cov['source_files_requested']
                and not any(row.get('source_matches') or row.get('document_evidence') for row in rows)
                and not report.get('related_evidence')):
            return {**result, 'state': 'failed', 'outcome': 'execution_error', 'exit_code': 1,
                    'agent_action': 'repair_unavailable_sources_then_retry'}
        requested = cov.get('requested_physical_rows')
        covered = (bool(rows) and isinstance(requested, list)
                   and [item.get('row_num') for item in rows] == requested)
        if (covered and localization.get('status') == 'located'
                and cov.get('ingestion_complete') is True):
            # Source correspondence is useful and independently complete. Missing
            # original code/rule dates cannot erase successfully located records.
            return {**result, 'state': 'completed', 'outcome': 'sources_located', 'exit_code': 0,
                    'agent_action': 'deliver_source_chain_and_review_unverified_transformations'}
        outcome = {'ambiguous': 'ambiguous_sources', 'not_found': 'no_matches', 'evidence_only': 'source_evidence_found'}.get(
            localization.get('status'), 'partial_source_trace')
        return {**result, 'outcome': outcome}
    if stage == 'deterministic_reconciliation_batch':
        items = report.get('results', [])
        requested = report.get('coverage', {}).get('requested_physical_rows')
        if not items or not isinstance(requested, list) or len(items) != len(requested):
            return base
        if any(item.get('row') != row for item, row in zip(items, requested)):
            return base
        states = [assess(item.get('report', {})) if item.get('status') == 'processed'
                  else {**base, 'state': 'failed'} for item in items]
        if all(s['state'] == 'failed' for s in states):
            return {**base, 'state': 'failed', 'outcome': 'execution_error', 'exit_code': 1,
                    'agent_action': 'repair_input_or_environment_then_retry'}
        if all(s['outcome'] == 'verified_computation' for s in states):
            return {**base, 'state': 'completed', 'outcome': 'verified_computation', 'exit_code': 0,
                    'agent_action': 'deliver_recalculation_with_lineage_limitations'}
        if any(s['outcome'] == 'mismatch' for s in states):
            return {**base, 'outcome': 'mismatch', 'agent_action': 'review_mismatches_and_remaining_gaps'}
        return base
    if stage == 'deterministic_reconciliation':
        checks = report.get('checks', [])
        if any(c.get('status') == 'mismatch' for c in checks):
            return {**base, 'outcome': 'mismatch', 'agent_action': 'review_mismatches'}
        if (checks and all(c.get('status') == 'equal' for c in checks)
                and report.get('status') == 'reconciled_not_proven_lineage'
                and report.get('coverage', {}).get('ingestion_complete') is True
                and report.get('rule_evidence') and not report.get('ingestion_issues')
                and not report.get('missing')):
            return {**base, 'state': 'completed', 'outcome': 'verified_computation', 'exit_code': 0,
                    'agent_action': 'deliver_recalculation_with_lineage_limitations'}
        return base
    if stage == 'candidate_links':
        return {**base, 'outcome': 'candidates_only',
                'agent_action': 'verify_field_mapping_join_cardinality_and_recompute'}
    if stage == 'exact_extraction':
        cov = report.get('coverage', {})
        if (cov.get('scan_complete') is True and cov.get('filter_evaluation_complete') is True
                and cov.get('truncated') is False and cov.get('projection_complete') is not False
                and not any(i.get('severity') in ('warning', 'error') for i in report.get('issues', []))):
            count = cov.get('matched_rows', cov.get('rows_matched'))
            if count is None:
                count = len(report.get('rows', []))
            if count == 0:
                return {**base, 'outcome': 'no_matches', 'agent_action': 'review_keys_and_source_scope'}
            return {**base, 'state': 'completed', 'outcome': 'extraction_complete', 'exit_code': 0,
                    'agent_action': 'verify_extracted_evidence_and_recompute'}
        return base
    return base


class Run:
    """Atomic current status plus append-only events. No evidence values in events."""
    def __init__(self, directory, operation):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        # One publisher per output directory. SQLite releases this lock even
        # after a killed process; no stale PID file can permanently block runs.
        self._lock = sqlite3.connect(self.directory / '.run.lock.sqlite', timeout=0.25)
        try:
            self._lock.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            self._lock.close()
            raise ValueError('Output directory is in use by another run; choose a different --out-dir') from exc
        self.started = time.perf_counter()
        self.status = {'schema_version': '1.1', 'run_id': str(uuid.uuid4()),
                       'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat(),
                       'operation': operation, 'state': 'running', 'execution': 'running',
                       'outcome': None, 'exit_code': None, 'lineage_status': 'not_proven',
                       'agent_action': 'wait_for_terminal_status', 'artifacts': {},
                       'status_path': str(self.directory / 'status.json')}
        try:
            self.event('started')
        except BaseException:
            self.close()
            raise

    def close(self):
        lock = getattr(self, '_lock', None)
        if lock is not None:
            lock.close()
            self._lock = None

    def event(self, phase, **metrics):
        self.status.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat(),
                           elapsed_seconds=round(time.perf_counter() - self.started, 3))
        item = {k: self.status[k] for k in ('run_id', 'phase', 'updated_at', 'elapsed_seconds')}
        item.update(metrics)
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + '\n')
        atomic_json(self.directory / 'status.json', self.status)

    def finish(self, report, artifacts, error=None):
        self.status.update(assess(report))
        self.status['execution'] = 'failed' if error or self.status['state'] == 'failed' else 'succeeded'
        self.status['artifacts'] = {name: artifact(path) for name, path in artifacts.items()}
        if error:
            self.status.update(state='failed', outcome='execution_error', exit_code=1,
                               agent_action='repair_input_or_environment_then_retry', error=error)
        self.event('finished', state=self.status['state'], outcome=self.status['outcome'])
        self.close()
        return self.status
