"""Cross-query snapshot evidence, fail-closed publication and recovery checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test_xlsx_query as fixtures
import xlsx_query as xq
import xlsx_snapshot as xs

text_cell, number_cell, row = fixtures.text_cell, fixtures.number_cell, fixtures.row


class SnapshotTests(unittest.TestCase):
    workbook = fixtures.StreamingQueryTests.workbook
    simple = fixtures.StreamingQueryTests.simple

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.index_dir = self.root / 'index'

    def prepare(self, path, keys=('ID',), **kwargs):
        return xs.prepare_snapshot(path, index_dir=self.index_dir, key_columns=keys, **kwargs)

    def query(self, path, **kwargs):
        return xs.query_snapshot(path, index_dir=self.index_dir, **kwargs)

    def assert_evidence_equal(self, path, **kwargs):
        indexed, scanned = self.query(path, **kwargs), xq.query_xlsx(path, **kwargs)
        self.assertEqual(indexed['rows'], scanned['rows'])
        self.assertEqual(indexed['source_sha256'], scanned['source_sha256'])
        self.assertEqual(indexed['issues'], scanned['issues'])
        for field in ('scanned_rows', 'matched_rows', 'returned_rows', 'returned_cells',
                      'truncated', 'scan_complete', 'filter_evaluation_complete',
                      'unavailable_filter_rows', 'expression_provenance_complete'):
            self.assertEqual(indexed['coverage'][field], scanned['coverage'][field], field)
        return indexed

    def test_new_keys_and_projections_reuse_rows_without_opening_source_zip(self):
        path = self.simple()
        before = path.read_bytes()
        built = self.prepare(path)
        self.assertTrue(built['complete'])
        self.assertFalse(built['reused'])
        self.assertEqual(built['rows'], 3)
        expected = xq.query_xlsx(path, where={'ID': ['7']}, columns=['Amount'])
        with mock.patch.object(xs, 'ZipFile', side_effect=AssertionError('No source ZIP reads')):
            result = xq.query_xlsx(path, snapshot_dir=self.index_dir,
                                   where={'ID': ['7']}, columns=['Amount'])
        self.assertEqual(result['rows'], expected['rows'])
        self.assertEqual(result['coverage']['rows_examined_this_query'], 1)
        self.assertEqual(result['cache']['kind'], 'source_snapshot')
        self.assertEqual(path.read_bytes(), before)
        self.assert_evidence_equal(path, where={'ID': ['0007']}, columns=['Type', 'Amount', 'ID'])

    def test_initial_build_defers_secondary_tree_but_incremental_keeps_it(self):
        path = self.simple()
        observations = []
        original = xs._indexes

        def observe(connection, *args, **kwargs):
            observations.append(connection.execute("SELECT count(*) FROM sqlite_master WHERE name='key_lookup'").fetchone()[0])
            return original(connection, *args, **kwargs)

        with mock.patch.object(xs, '_indexes', side_effect=observe):
            built = self.prepare(path)
            self.prepare(path, keys=('Type',))
        self.assertEqual(observations, [0, 1])
        with closing(sqlite3.connect(built['snapshot_path'])) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='key_lookup'").fetchone()[0], 1)
        self.assert_evidence_equal(path, where={'Type': ['fee']})

    def test_bulk_lookup_build_failure_never_publishes(self):
        path = self.simple()
        with mock.patch.object(xs, '_build_initial_lookup_index', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError): self.prepare(path)
        self.assertEqual(list(self.index_dir.glob('*.building')), [])
        self.assertEqual([p for p in self.index_dir.glob('*.sqlite') if not p.name.endswith('.lock.sqlite')], [])
        self.assertTrue(self.prepare(path)['complete'])

    def test_deleted_secondary_tree_is_not_a_complete_snapshot(self):
        path = self.simple()
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db: db.execute('DROP INDEX key_lookup')
        with self.assertRaisesRegex(xs.SnapshotUnavailable, 'lookup index is incomplete'): self.query(path)

    def test_no_initial_keys_still_publishes_complete_lookup_schema(self):
        path = self.simple()
        built = self.prepare(path, keys=())
        self.assertEqual(built['indexed_columns'], [])
        self.assert_evidence_equal(path, where={'ID': ['0007']})

    def test_v3_count_index_upgrade_is_atomic_and_not_repeated(self):
        path = self.simple()
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            db.execute('DROP INDEX key_coverage_count')
        stages = []
        result = self.query(path, where={'ID': ['0007']}, progress=stages.append)
        self.assertTrue(result['snapshot']['coverage_index_maintenance']['created'])
        self.assertEqual(result['snapshot']['coverage_index_maintenance']['integrity_check'], 'ok')
        self.assertEqual([item['stage'] for item in stages], ['upgrading_coverage_index', 'verifying_coverage_index'])
        stages.clear()
        repeated = self.query(path, where={'ID': ['7']}, progress=stages.append)
        self.assertFalse(repeated['snapshot']['coverage_index_maintenance']['created'])
        self.assertEqual(stages, [])
        self.assertEqual(xs.VERSION, 'xlsx-snapshot-3')
        self.assert_evidence_equal(path, where={'ID': ['0007']})

    def test_interrupted_count_index_upgrade_preserves_legacy_complete_snapshot(self):
        path = self.simple()
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db: db.execute('DROP INDEX key_coverage_count')
        def interrupt(progress):
            if progress['stage'] == 'verifying_coverage_index': raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): self.query(path, where={'ID': ['0007']}, progress=interrupt)
        with closing(sqlite3.connect(built['snapshot_path'])) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='key_coverage_count'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM keys WHERE field=?', ('ID',)).fetchone()[0], 3)
        self.assert_evidence_equal(path, where={'ID': ['0007']})

    def test_count_index_upgrade_keeps_missing_key_detection(self):
        path = self.simple()
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            db.execute('DROP INDEX key_coverage_count')
            db.execute('DELETE FROM keys WHERE field=? AND row_num=?', ('ID', 2))
        with self.assertRaisesRegex(xs.SnapshotUnavailable, 'key index coverage'):
            self.query(path, where={'ID': ['0007']})

    def test_composite_keys_do_not_form_a_cross_product_and_new_index_uses_snapshot(self):
        path = self.simple()
        self.prepare(path)
        args = {'key_rows': [{'ID': '0007', 'Type': 'refund'}, {'ID': '7', 'Type': 'fee'}],
                'columns': ['ID', 'Type']}
        expected = xq.query_xlsx(path, **args)
        with mock.patch.object(xs, 'ZipFile', side_effect=AssertionError('No source ZIP reads')):
            result = self.query(path, **args)
        self.assertEqual(result['rows'], expected['rows'])
        self.assertEqual([r['row_num'] for r in result['rows']], [8, 12])
        self.assertEqual(result['snapshot']['newly_indexed_columns'], ['Type'])
        self.assertEqual(self.query(path, **args)['snapshot']['newly_indexed_columns'], [])
        self.assert_evidence_equal(path, where={'Amount': ['-2.50', '9.00']}, **args)

    def test_composite_anchor_uses_selectivity_and_preserves_filter_issues(self):
        path = self.workbook([
            row(1, text_cell('A1', 'Group'), text_cell('B1', 'ID')),
            *[row(i, text_cell(f'A{i}', 'broad'), text_cell(f'B{i}', f'id-{i}')) for i in range(2, 102)],
            row(102, number_cell('A102', None, formula='1+1'), text_cell('B102', 'other')),
            row(103, '<c r="A103"><f t="shared" si="0"/><v>broad</v></c>', text_cell('B103', 'other')),
            row(104, text_cell('A104', 'outside'), '<c r="B104" t="e"><v>#N/A</v></c>'),
        ])
        self.prepare(path, keys=('Group', 'ID'))
        result = self.assert_evidence_equal(path, key_rows=[{'Group': 'broad', 'ID': 'id-7'}])
        self.assertEqual(result['coverage']['index_anchor_field'], 'ID')
        self.assertEqual(result['coverage']['rows_examined_this_query'], 3)
        self.assertFalse(result['coverage']['filter_evaluation_complete'])
        self.assertFalse(result['coverage']['expression_provenance_complete'])
        # A contradictory where/tuple combination still retains unavailable
        # formula diagnostics instead of treating the empty candidate set as a
        # complete evaluation.
        self.assert_evidence_equal(path, where={'Group': ['broad']},
                                   key_rows=[{'Group': 'outside', 'ID': 'id-7'}])

    def test_empty_issue_index_is_not_trusted_after_filter_diagnostic_loss(self):
        path = self.workbook([row(1, text_cell('A1', 'ID')),
                              row(2, number_cell('A2', None, formula='1+1'))])
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            db.execute('DELETE FROM key_issues')
        with self.assertRaisesRegex(xs.SnapshotUnavailable, 'filter issue index coverage'):
            self.query(path, where={'ID': ['x']})

    def test_dates_formulas_decimal_bytes_and_shared_string_keys_remain_exact(self):
        path = self.workbook([
            row(1, text_cell('A1', 'ID'), text_cell('B1', 'Date'), text_cell('C1', 'Amount')),
            row(2, '<c r="A2" t="s"><v>0</v></c>', number_cell('B2', '1', 2),
                number_cell('C2', '123.450000000000001', formula='100+23.45')),
            row(8, number_cell('A8', '7', 1), number_cell('B8', '2', 2),
                number_cell('C8', None, formula='1+1')),
        ], strings='<si><r><t>00</t></r><r><t>07</t></r><rPh><t>ignored</t></rPh></si>', epoch_1904=True)
        self.prepare(path)
        result = self.assert_evidence_equal(path, where={'ID': ['0007']})
        self.assertEqual(result['rows'][0]['cells']['Date']['excel_epoch'], '1904')
        self.assertEqual(result['rows'][0]['cells']['Amount']['raw_value'], '123.450000000000001')
        self.assert_evidence_equal(path, where={'Amount': [None]})

    def test_header_and_physical_locations_are_not_renumbered(self):
        path = self.workbook([row(1, text_cell('A1', 'Report title')),
                              row(3, text_cell('A3', 'ID'), text_cell('B3', 'Value')),
                              row(26, text_cell('A26', 'x'), number_cell('B26', '3'))])
        self.prepare(path, header_row=3)
        result = self.assert_evidence_equal(path, header_row=3, where={'ID': ['x']})
        self.assertEqual(result['rows'][0]['cells']['Value']['address'], 'B26')
        with self.assertRaises(xs.SnapshotUnavailable): self.query(path, header_row=1)

    def test_limits_preserve_match_coverage_and_cell_budget_fails(self):
        path = self.simple()
        self.prepare(path)
        self.assert_evidence_equal(path, where={'ID': ['0007', '7']}, limit=1)
        result = self.assert_evidence_equal(path, limit=0)
        self.assertEqual(result['coverage']['matched_rows'], 3)
        with self.assertRaises(ValueError): self.query(path, max_return_cells=4)

    def test_source_change_with_same_size_and_mtime_invalidates_snapshot(self):
        path = self.simple()
        self.prepare(path)
        previous = path.stat()
        data = path.read_bytes()
        # ZIP_STORED test fixtures: replace equal-sized bytes and update CRCs by rebuilding.
        with ZipFile(path) as archive: entries = {name: archive.read(name) for name in archive.namelist()}
        entries['xl/worksheets/sheet1.xml'] = entries['xl/worksheets/sheet1.xml'].replace(b'123.450000000000001', b'123.450000000000009')
        with ZipFile(path, 'w') as archive:
            for name, content in entries.items(): archive.writestr(name, content)
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(path.stat().st_size, len(data))
        with self.assertRaises(xs.SnapshotUnavailable): self.query(path, where={'ID': ['0007']})

    def test_missing_incomplete_rows_and_incomplete_key_index_are_rejected(self):
        path = self.simple()
        with self.assertRaises(xs.SnapshotUnavailable): self.query(path)
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            db.execute('DELETE FROM keys WHERE field=? AND row_num=?', ('ID', 2))
        with self.assertRaisesRegex(xs.SnapshotUnavailable, 'key index coverage'):
            self.query(path, where={'ID': ['0007']})
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db: db.execute('DELETE FROM rows WHERE row_num=2')
        with self.assertRaisesRegex(xs.SnapshotUnavailable, 'row coverage'): self.query(path)

    def test_incomplete_completion_marker_and_corrupt_selected_row_are_rejected(self):
        path = self.simple()
        built = self.prepare(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps('building'), 'state'))
        with self.assertRaises(xs.SnapshotUnavailable): self.query(path)
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps('complete'), 'state'))
            db.execute('UPDATE rows SET sha256=? WHERE row_num=2', ('0' * 64,))
        with self.assertRaisesRegex(ValueError, 'checksum'): self.query(path, where={'ID': ['0007']})

    def test_malformed_xml_tail_never_publishes_even_when_keys_match_early(self):
        path = self.simple()
        with ZipFile(path) as archive: entries = {name: archive.read(name) for name in archive.namelist()}
        entries['xl/worksheets/sheet1.xml'] += b'<broken>'
        with ZipFile(path, 'w') as archive:
            for name, content in entries.items(): archive.writestr(name, content)
        with self.assertRaises(ValueError): self.prepare(path)
        self.assertEqual(list(self.index_dir.glob('*.building')), [])
        self.assertEqual([p for p in self.index_dir.glob('*.sqlite') if not p.name.endswith('.lock.sqlite')], [])

    def test_noncanonical_xml_is_explicitly_declined_without_breaking_scan_reader(self):
        path = self.simple()
        with ZipFile(path) as archive: entries = {name: archive.read(name) for name in archive.namelist()}
        entries['xl/worksheets/sheet1.xml'] = entries['xl/worksheets/sheet1.xml'].replace(b'<sheetData>', b'<sheetData><!-- valid comment -->')
        with ZipFile(path, 'w') as archive:
            for name, content in entries.items(): archive.writestr(name, content)
        with self.assertRaisesRegex(ValueError, 'comments'): self.prepare(path)
        self.assertEqual(list(self.index_dir.glob('*.building')), [])
        self.assertEqual(xq.query_xlsx(path, where={'ID': ['0007']})['coverage']['matched_rows'], 2)
        with self.assertRaises(xs.SnapshotUnavailable): self.query(path)

    def test_build_exception_cleans_private_files_and_lock_is_reusable(self):
        path = self.simple()
        with mock.patch.object(xs.zlib, 'compress', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.prepare(path)
        self.assertEqual(list(self.index_dir.glob('*.building')), [])
        self.assertTrue(self.prepare(path)['complete'])

    def test_process_death_releases_lock_and_orphan_build_is_recovered(self):
        path = self.simple()
        marker = self.root / 'private-build-started'
        code = """
from pathlib import Path
import sys,time
sys.path.insert(0, sys.argv[1])
import xlsx_snapshot as xs
original = xs.zlib.compress
def pause(*args, **kwargs):
    Path(sys.argv[4]).write_text('ready')
    time.sleep(30)
    return original(*args, **kwargs)
xs.zlib.compress = pause
xs.prepare_snapshot(sys.argv[2], index_dir=sys.argv[3], key_columns=['ID'])
"""
        process = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'),
                                    str(path), str(self.index_dir), str(marker)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), 'Child did not reach private build stage')
            self.assertEqual([p for p in self.index_dir.glob('*.sqlite') if not p.name.endswith('.lock.sqlite')], [])
        finally:
            process.kill()
            process.communicate(timeout=5)
        self.assertTrue(list(self.index_dir.glob('*.building')))
        self.assertTrue(self.prepare(path)['complete'])
        self.assertEqual(list(self.index_dir.glob('*.building')), [])
        self.assert_evidence_equal(path, where={'ID': ['0007']})

    def test_index_interruption_rolls_back_and_old_index_remains_usable(self):
        path = self.simple()
        built = self.prepare(path)
        original = xs._decode_selected
        count = 0

        def interrupt(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2: raise KeyboardInterrupt
            return original(*args, **kwargs)

        with mock.patch.object(xs, '_decode_selected', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.query(path, where={'Type': ['fee']})
        with closing(sqlite3.connect(built['snapshot_path'])) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM keys WHERE field=?', ('Type',)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM indexed_columns WHERE field=?', ('Type',)).fetchone()[0], 0)
        self.assert_evidence_equal(path, where={'ID': ['0007']})
        self.assert_evidence_equal(path, where={'Type': ['fee']})

    def test_concurrent_prepares_publish_one_snapshot_and_keep_both_key_indexes(self):
        path = self.simple()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.prepare, path, keys=(name,)) for name in ('ID', 'Type')]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(result['reused'] for result in results), [False, True])
        self.assertEqual(results[0]['snapshot_path'], results[1]['snapshot_path'])
        self.assertEqual(self.prepare(path)['indexed_columns'], ['ID', 'Type'])
        self.assert_evidence_equal(path, where={'Type': ['fee']})

    def test_budget_and_source_change_during_build_never_publish(self):
        path = self.simple()
        with self.assertRaisesRegex(ValueError, 'budget'): self.prepare(path, max_snapshot_bytes=4096)
        digest = xq._source_digest(path)
        with mock.patch.object(xq, '_source_digest', side_effect=[digest, '0' * 64]):
            with self.assertRaisesRegex(ValueError, 'Source changed'): self.prepare(path)
        self.assertEqual([p for p in self.index_dir.glob('*.sqlite') if not p.name.endswith('.lock.sqlite')], [])
        self.assertEqual(list(self.index_dir.glob('*.building')), [])

    def test_reused_snapshot_does_not_ignore_requested_disk_budget(self):
        path = self.simple()
        self.prepare(path)
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.prepare(path, max_snapshot_bytes=4096)
        self.assert_evidence_equal(path, where={'ID': ['0007']})

    def test_false_y_invalid_arguments_do_not_reuse_snapshot(self):
        path = self.simple()
        self.prepare(path)
        for kwargs in ({'columns': []}, {'where': []}, {'key_rows': []}, {'limit': False},
                       {'max_return_cells': False}, {'header_row': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.query(path, **kwargs)


if __name__ == '__main__': unittest.main()
