"""Behavioral checks for the DATA machine contract and artifact delivery."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from run_contract import assess
from run_contract import Run
from data_wall import generate_data_wall


class ContractTests(unittest.TestCase):
    def test_concurrent_publishers_cannot_mix_status_or_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Run(tmp, 'trace')
            before = (Path(tmp)/'status.json').read_bytes()
            try:
                with self.assertRaisesRegex(ValueError, 'another run'):
                    Run(tmp, 'query')
                self.assertEqual(before, (Path(tmp)/'status.json').read_bytes())
            finally:
                first.close()
            second = Run(tmp, 'query')
            self.assertNotEqual(first.status['run_id'], second.status['run_id'])
            second.close()

    def test_target_evidence_completion_has_its_own_dimension(self):
        report = {'analysis_stage': 'evidence_search', 'related_evidence': [{'quote': 'order ID'}],
                  'coverage': {'retrieval_complete': True, 'truncated': False}}
        self.assertEqual('evidence_located', assess(report)['outcome'])
        self.assertEqual('not_checked', assess(report)['calculation_verification']['status'])
        report['coverage']['retrieval_complete'] = False
        self.assertEqual(2, assess(report)['exit_code'])

    def verified(self):
        return {'analysis_stage': 'deterministic_reconciliation',
                'status': 'reconciled_not_proven_lineage',
                'checks': [{'result_field': 'amount', 'status': 'equal'}],
                'coverage': {'ingestion_complete': True},
                'rule_evidence': [{'quote': 'sum'}], 'ingestion_issues': {}}

    def test_candidates_or_empty_checks_never_become_success(self):
        self.assertEqual(2, assess({'analysis_stage': 'candidate_links'})['exit_code'])
        report = self.verified()
        report['checks'] = []
        self.assertEqual('partial', assess(report)['state'])

    def test_verified_computation_is_not_historical_lineage(self):
        state = assess(self.verified())
        self.assertEqual(0, state['exit_code'])
        self.assertEqual('not_proven', state['lineage_status'])

    def test_located_sources_do_not_require_a_historical_execution_log(self):
        report = {'analysis_stage': 'source_trace',
                  'result_rows': [{'row_num': 4}, {'row_num': 5}],
                  'coverage': {'requested_physical_rows': [4, 5], 'ingestion_complete': True},
                  'source_localization': {'status': 'located', 'located_rows': 2},
                  'calculation_verification': {'status': 'not_checked'},
                  'rule_applicability': {'status': 'not_verified'},
                  'historical_lineage': {'status': 'not_proven'}}
        state = assess(report)
        self.assertEqual('sources_located', state['outcome'])
        self.assertEqual(0, state['exit_code'])
        self.assertEqual('not_checked', state['calculation_verification']['status'])
        self.assertEqual('not_proven', state['historical_lineage']['status'])
        # A missing requested result or incomplete scan still needs attention.
        report['result_rows'].pop()
        self.assertEqual(2, assess(report)['exit_code'])
        report['result_rows'].append({'row_num': 5})
        report['coverage']['ingestion_complete'] = False
        self.assertEqual(2, assess(report)['exit_code'])
        report['source_localization']['status'] = 'ambiguous'
        self.assertEqual('ambiguous_sources', assess(report)['outcome'])

    def test_all_unavailable_sources_fail_but_partial_work_is_preserved(self):
        report = {'analysis_stage': 'source_trace', 'result_rows': [{'row_num': 2, 'source_matches': []}],
                  'source_localization': {'status': 'not_found'},
                  'coverage': {'source_files_requested': 2, 'source_files_unavailable': 2,
                               'requested_physical_rows': [2], 'ingestion_complete': False}}
        self.assertEqual('failed', assess(report)['state'])
        self.assertEqual(1, assess(report)['exit_code'])
        report['coverage']['source_files_unavailable'] = 1
        self.assertEqual('partial', assess(report)['state'])
        self.assertEqual(2, assess(report)['exit_code'])

    def test_every_requested_row_and_gap_matters(self):
        report = {'analysis_stage': 'deterministic_reconciliation_batch',
                  'coverage': {'requested_physical_rows': [2, 3]},
                  'results': [{'row': row, 'status': 'processed', 'report': self.verified()} for row in (2, 3)]}
        self.assertEqual(0, assess(report)['exit_code'])
        for mutate in (lambda x: x['results'].pop(),
                       lambda x: x['results'][1].update(row=2),
                       lambda x: x['results'][1]['report'].update(missing=['dictionary']),
                       lambda x: x['results'][1].update(status='error')):
            bad = copy.deepcopy(report)
            mutate(bad)
            self.assertEqual(2, assess(bad)['exit_code'])
        report['results'][1]['report']['checks'][0]['status'] = 'mismatch'
        self.assertEqual('mismatch', assess(report)['outcome'])

    def test_query_empty_or_truncated_is_not_complete(self):
        report = {'analysis_stage': 'exact_extraction', 'rows': [{'row': 2}],
                  'coverage': {'scan_complete': True, 'filter_evaluation_complete': True,
                               'truncated': False, 'matched_rows': 1}}
        self.assertEqual('extraction_complete', assess(report)['outcome'])
        report['coverage']['truncated'] = True
        self.assertEqual(2, assess(report)['exit_code'])
        report['coverage'].update(truncated=False, matched_rows=0)
        self.assertEqual('no_matches', assess(report)['outcome'])

    def call(self, *args):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/data_run.py'), *map(str, args)],
                                capture_output=True, text=True, encoding='utf-8')
        return result, json.loads(result.stdout)

    def test_cli_success_partial_failure_and_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root/'input.json'
            for i, value in enumerate((self.verified(), {'analysis_stage': 'candidate_links'})):
                report.write_text(json.dumps(value), encoding='utf-8')
                out = root/f'run{i}'
                proc, status = self.call('report', '--report', report, '--out-dir', out)
                self.assertEqual((0, 2)[i], proc.returncode, proc.stderr)
                self.assertEqual(status['run_id'], json.loads((out/'status.json').read_text())['run_id'])
                self.assertTrue((out/'wall.html').exists())
                for ref in status['artifacts'].values():
                    self.assertEqual(ref['sha256'], hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest())
                events = [json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
                self.assertEqual('finished', events[-1]['phase'])
            proc, status = self.call('reconcile', '--plan', root/'missing.json', '--out-dir', root/'failed')
            self.assertEqual(1, proc.returncode)
            self.assertEqual('failed', status['state'])
            self.assertEqual('failed', status['execution'])
            self.assertTrue((root/'failed'/'wall.html').exists())

    def test_output_cannot_overwrite_plan_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'report.json'
            original = '{"preserve": true}'
            source.write_text(original, encoding='utf-8')
            plan = root/'plan.json'
            plan.write_text(json.dumps({'result': {'file': 'report.json'}}), encoding='utf-8')
            proc = subprocess.run([sys.executable, str(ROOT/'scripts/data_run.py'), 'reconcile',
                '--plan', str(plan), '--out-dir', str(root)], capture_output=True)
            self.assertNotEqual(0, proc.returncode)
            self.assertEqual(original, source.read_text())

    def test_wall_embedded_evidence_cannot_close_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'wall.html'
            generate_data_wall({'status': 'error', 'error': '</script><script>alert(1)</script>'}, path)
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('<script>alert(1)', text)
            self.assertIn('\\u003c/script', text)
            self.assertNotIn('innerHTML', text)

    def test_wall_write_error_still_publishes_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root/'input.json'
            report.write_text(json.dumps(self.verified()), encoding='utf-8')
            out = root/'run'
            (out/'wall.html').mkdir(parents=True)
            proc, status = self.call('report', '--report', report, '--out-dir', out)
            self.assertEqual(1, proc.returncode)
            saved = json.loads((out/'status.json').read_text())
            self.assertEqual('finished', saved['phase'])
            self.assertEqual('failed', saved['state'])
            self.assertEqual(status['run_id'], saved['run_id'])
            self.assertNotIn('wall', saved['artifacts'])

    def test_streaming_decimal_query_and_missing_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source.jsonl'
            source.write_text('{"id":"001","amount":0.1234567890123456789}\n', encoding='utf-8')
            keys = root/'keys.json'
            keys.write_text('[{"amount":0.1234567890123456789}]', encoding='utf-8')
            proc, status = self.call('query', '--file', source, '--keys-json', keys,
                '--columns', 'id', 'amount', '--out-dir', root/'good')
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual('extraction_complete', status['outcome'])
            saved = json.loads((root/'good'/'report.json').read_text())
            self.assertEqual('0.1234567890123456789', saved['records'][0]['values']['amount'])
            proc, status = self.call('query', '--file', source, '--columns', 'absent', '--out-dir', root/'bad')
            self.assertEqual(2, proc.returncode, proc.stderr)
            self.assertEqual('partial', status['state'])

    def test_all_batch_rows_error_is_failed(self):
        report = {'analysis_stage': 'deterministic_reconciliation_batch',
                  'coverage': {'requested_physical_rows': [999, 1000]},
                  'results': [{'row': r, 'status': 'error'} for r in [999, 1000]]}
        self.assertEqual(1, assess(report)['exit_code'])
        self.assertEqual('failed', assess(report)['state'])


if __name__ == '__main__':
    unittest.main()
