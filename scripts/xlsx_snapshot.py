"""Persistent, source-hash-bound XLSX row snapshots with exact-key indexes.

Rows retain compressed original XML, not expanded JSON. Index creation is
explicit; queries can add a missing key index using this snapshot, without
reopening/decompressing the source worksheet. A snapshot is evidence storage,
not a claim about historical execution or the truth of source values.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from zipfile import ZipFile
import zlib

import xlsx_query as xq

VERSION = 'xlsx-snapshot-3'
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_INDEX_COLUMNS = 16


class SnapshotUnavailable(ValueError):
    """The exact source/version has no complete snapshot in this directory."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _token(value):
    return _json(xq._exact(value))


def _signature(file, digest, sheet, header_row):
    return {'version': VERSION, 'reader_version': xq.READER_VERSION, 'file': os.path.normcase(str(file)),
            'source_sha256': digest, 'sheet': sheet, 'header_row': header_row}


def _path(index_dir, signature):
    return Path(index_dir).expanduser().resolve() / (hashlib.sha256(_json(signature).encode('utf-8')).hexdigest() + '.sqlite')


def _connect(path, readonly=False):
    connection = (sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30) if readonly else
                  sqlite3.connect(str(path), timeout=30))
    connection.execute('PRAGMA cache_size=-8192')
    connection.execute('PRAGMA temp_store=FILE')
    if not readonly:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=FULL')
    return connection


@contextmanager
def _lock(path):
    # SQLite's OS-backed transaction lock is released even after process death.
    # Separate small locks allow different source snapshots to build in parallel.
    lock = _connect(path.with_suffix('.lock.sqlite'))
    try:
        lock.execute('CREATE TABLE IF NOT EXISTS mutex (id INTEGER PRIMARY KEY)')
        lock.commit()
        lock.execute('BEGIN IMMEDIATE')
        yield
        lock.rollback()
    finally:
        lock.close()


def _manifest(connection, signature):
    try:
        meta = {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM meta')}
    except (sqlite3.Error, ValueError) as exc:
        raise SnapshotUnavailable('Snapshot metadata is unavailable or invalid') from exc
    if meta.get('signature') != signature or meta.get('state') != 'complete':
        raise SnapshotUnavailable('Snapshot source, reader version or completion marker does not match')
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='key_lookup'").fetchone() is None:
        raise SnapshotUnavailable('Snapshot lookup index is incomplete; rebuild the snapshot')
    if connection.execute('SELECT count(*) FROM rows').fetchone()[0] != meta.get('row_count'):
        raise SnapshotUnavailable('Snapshot row coverage is incomplete')
    return meta


def _resolve(file, index_dir, sheet, header_row):
    file = Path(file).expanduser().resolve()
    if type(header_row) is not int or header_row < 1:
        raise ValueError('header_row must be a positive physical row')
    digest = xq._source_digest(file)
    # Bind to the caller's selection, including None for an unambiguous sheet.
    signature = _signature(file, digest, sheet, header_row)
    return file, signature, _path(index_dir, signature)


def _cleanup_incomplete(path):
    # Caller holds this signature's OS-backed lock. A surviving private build
    # can only belong to a process that exited before atomic publication.
    for temporary in path.parent.glob('.' + path.stem + '-*.building'):
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + '-journal').unlink(missing_ok=True)


class _Strings:
    def __init__(self, connection, maximum=1024 * 1024):
        self.connection, self.maximum = connection, maximum
        self.hot, self.retained = OrderedDict(), 0
        self.count = connection.execute('SELECT count(*) FROM strings').fetchone()[0]

    def __len__(self): return self.count

    def __getitem__(self, index):
        if not 0 <= index < self.count: raise IndexError(index)
        if index in self.hot:
            value = self.hot.pop(index)
            self.hot[index] = value
            return value
        row = self.connection.execute('SELECT value FROM strings WHERE id=?', (index,)).fetchone()
        if row is None: raise IndexError(index)
        value = row[0]
        size = len(value.encode('utf-8')) + 128
        if size <= self.maximum:
            while self.hot and self.retained + size > self.maximum:
                _, old = self.hot.popitem(last=False)
                self.retained -= len(old.encode('utf-8')) + 128
            self.hot[index] = value
            self.retained += size
        return value


def _selector(column):
    target = column.encode('ascii') + rb'[1-9][0-9]*'
    return re.compile(rb'<c(?:\s+' + xq._ATTRIBUTE + rb')*?\s+r\s*=\s*(?:"' + target + rb'"|\'' + target + rb'\')(?:\s+' + xq._ATTRIBUTE + rb')*\s*(?:/>|>.*?</c\s*>)', re.DOTALL)


def _decode_selected(raw, number, root, columns, strings, styles, formats, epoch, selectors=None):
    """Decode projected cells only; uncertain lexical layouts use the XML row."""
    selectors = selectors or {column: _selector(column) for column in columns}
    selected, full = {}, None
    for column in columns:
        address = f'{column}{number}'
        found = selectors[column].search(raw)
        fallback = b'&' in raw and re.search(rb'\sr\s*=\s*(?:"[^"]*&|\'[^\']*&)', raw)
        quoted = address.encode('ascii')
        fallback = fallback or raw.count(b'"' + quoted + b'"') + raw.count(b"'" + quoted + b"'") > 1
        if found:
            previous = raw[:found.start()]
            last = previous[previous.rfind(b'<'):].strip()
            fallback = fallback or not (last.startswith(b'<row') or last.startswith(b'</c') or
                                        (last.startswith(b'<c') and last.endswith(b'/>')))
        if fallback or (b':' in raw[:raw.find(b'>')] and raw.startswith(b'<ns')):
            if full is None: full = xq._row_cells(xq._parse_frame(raw, root), number)
            element = full.get(column)
        elif found:
            fragment = root + f'<row r="{number}">'.encode() + found.group() + b'</row></worksheet>'
            element = xq.ET.fromstring(fragment)[0][0]
        else:
            element = None
        cell = xq._blank_cell(address) if element is None else xq._cell(element, strings, styles, formats, epoch)
        if cell['address'] != address: raise ValueError('Indexed cell does not belong to its physical row')
        selected[column] = cell
    return selected


def _available(cell):
    return not ((xq._has_formula(cell) and not cell['formula_cached']) or cell['data_type'] == 'e')


def _has_issue(cell):
    return (not _available(cell) or cell.get('formula_state') in
            ('shared_formula_unresolved', 'formula_expression_unavailable'))


def _budget(connection, maximum):
    size = connection.execute('PRAGMA page_count').fetchone()[0] * connection.execute('PRAGMA page_size').fetchone()[0]
    if size > maximum:
        raise ValueError('Snapshot disk budget exceeded; incomplete snapshot was not published')


def _indexes(connection, meta, fields, progress=None):
    fields = list(dict.fromkeys(fields))
    unknown = set(fields) - set(meta['header'])
    if unknown: raise ValueError('Unknown index fields: ' + ', '.join(sorted(unknown)))
    done = set()
    count_index = (' INDEXED BY key_coverage_count' if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='key_coverage_count'").fetchone() else '')
    for name, count, issue_count in connection.execute('SELECT field,rows_indexed,issue_count FROM indexed_columns'):
        if count != meta['row_count'] or connection.execute('SELECT count(*) FROM keys' + count_index + ' WHERE field=?', (name,)).fetchone()[0] != count:
            raise SnapshotUnavailable('Snapshot key index coverage is incomplete; rebuild the snapshot')
        if connection.execute('SELECT count(*) FROM key_issues WHERE field=?', (name,)).fetchone()[0] != issue_count:
            raise SnapshotUnavailable('Snapshot filter issue index coverage is incomplete; rebuild the snapshot')
        done.add(name)
    missing = [field for field in fields if field not in done]
    if not missing: return []
    if len(done | set(fields)) > MAX_INDEX_COLUMNS:
        raise ValueError(f'More than {MAX_INDEX_COLUMNS} indexed columns; use a smaller declared key set')
    strings = _Strings(connection)
    formats = {int(key): value for key, value in meta['formats'].items()}
    columns = [meta['header'][field] for field in missing]
    selectors = {column: _selector(column) for column in columns}
    root = meta['root'].encode('utf-8')
    connection.execute('BEGIN IMMEDIATE')
    try:
        for field in missing:
            connection.execute('DELETE FROM keys WHERE field=?', (field,))
            connection.execute('DELETE FROM key_issues WHERE field=?', (field,))
            connection.execute('DELETE FROM indexed_columns WHERE field=?', (field,))
        count = 0
        issue_counts = {field: 0 for field in missing}
        for number, blob, checksum in connection.execute('SELECT row_num,xml,sha256 FROM rows ORDER BY row_num'):
            raw = zlib.decompress(blob)
            if hashlib.sha256(raw).hexdigest() != checksum: raise ValueError('Snapshot row checksum mismatch')
            cells = _decode_selected(raw, number, root, columns, strings, meta['styles'], formats, meta['epoch'], selectors)
            connection.executemany('INSERT INTO keys(field,value,row_num,available) VALUES(?,?,?,?)',
                [(field, _token(cells[column]['value']), number, int(_available(cells[column])))
                 for field, column in zip(missing, columns)])
            issue_rows = [(field, number) for field, column in zip(missing, columns) if _has_issue(cells[column])]
            connection.executemany('INSERT INTO key_issues(field,row_num) VALUES(?,?)', issue_rows)
            for field, _ in issue_rows: issue_counts[field] += 1
            count += 1
            if count % 10000 == 0:
                _budget(connection, meta['max_snapshot_bytes'])
                if progress: progress({'stage': 'indexing_snapshot', 'rows': count, 'field_count': len(missing)})
        if count != meta['row_count']: raise ValueError('Snapshot index did not cover every stored row')
        connection.executemany('INSERT INTO indexed_columns(field,rows_indexed,issue_count) VALUES(?,?,?)',
                               [(f, count, issue_counts[f]) for f in missing])
        _budget(connection, meta['max_snapshot_bytes'])
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return missing


def _build_initial_lookup_index(connection, maximum, progress=None):
    """Bulk-build the secondary tree only inside an unpublished initial file."""
    _budget(connection, maximum)
    page_size = connection.execute('PRAGMA page_size').fetchone()[0]
    # A bulk index build cannot call the periodic per-row budget hook. Let
    # SQLite stop allocation at the same database-page budget while it sorts.
    connection.execute(f'PRAGMA max_page_count={maximum // page_size}')
    if progress: progress({'stage': 'building_lookup_index'})
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('CREATE INDEX key_lookup ON keys(field,available,value,row_num)')
        connection.execute('CREATE INDEX key_coverage_count ON keys(field)')
        _budget(connection, maximum)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _ensure_coverage_index(connection, maximum, progress=None):
    """Compatibly upgrade a complete v3 database once, preserving exact counts.

    Values are intentionally absent from this secondary tree. On WITHOUT ROWID
    keys, SQLite appends the row_num primary-key component automatically.
    """
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='key_coverage_count'").fetchone():
        return {'status': 'available', 'created': False, 'elapsed_seconds': 0.0}
    started = time.perf_counter()
    _budget(connection, maximum)
    page_size = connection.execute('PRAGMA page_size').fetchone()[0]
    connection.execute(f'PRAGMA max_page_count={maximum // page_size}')
    if progress: progress({'stage': 'upgrading_coverage_index'})
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('CREATE INDEX key_coverage_count ON keys(field)')
        _budget(connection, maximum)
        build_seconds = time.perf_counter() - started
        if progress: progress({'stage': 'verifying_coverage_index'})
        verifying = time.perf_counter()
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Snapshot database integrity check failed during count-index upgrade')
        verification_seconds = time.perf_counter() - verifying
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL:
            # The old complete v3 database remains usable with its slower full
            # counts when a performance-only index cannot fit. Never remove a
            # coverage check or exceed the declared database-page budget.
            return {'status': 'unavailable_within_disk_budget', 'created': False,
                    'elapsed_seconds': round(time.perf_counter() - started, 6)}
        raise
    except BaseException:
        connection.rollback()
        raise
    return {'status': 'upgraded', 'created': True, 'integrity_check': 'ok',
            'build_seconds': round(build_seconds, 6), 'verification_seconds': round(verification_seconds, 6),
            'elapsed_seconds': round(time.perf_counter() - started, 6)}


def prepare_snapshot(file, *, index_dir, sheet=None, header_row=1, key_columns=(),
                     max_snapshot_bytes=DEFAULT_MAX_BYTES, progress=None):
    """Build a complete compact snapshot once; reuse it across keys/projections."""
    started = time.perf_counter()
    if type(max_snapshot_bytes) is not int or max_snapshot_bytes < 4096:
        raise ValueError('max_snapshot_bytes must be an integer of at least 4096')
    if not isinstance(key_columns, (list, tuple)) or any(not isinstance(f, str) or not f for f in key_columns):
        raise ValueError('key_columns must be a list of nonempty field names')
    if len(set(key_columns)) > MAX_INDEX_COLUMNS:
        raise ValueError(f'More than {MAX_INDEX_COLUMNS} declared index columns')
    file, signature, path = _resolve(file, index_dir, sheet, header_row)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    reused = False
    maintenance = {'status': 'included_in_initial_build', 'created': True}
    with _lock(path):
        _cleanup_incomplete(path)
        if path.is_file():
            with closing(_connect(path)) as connection:
                meta = _manifest(connection, signature)
                _budget(connection, max_snapshot_bytes)
                # A reuse call must respect its own budget as well as the
                # original snapshot's declared growth ceiling.
                meta = dict(meta, max_snapshot_bytes=min(meta['max_snapshot_bytes'], max_snapshot_bytes))
                maintenance = _ensure_coverage_index(connection, meta['max_snapshot_bytes'], progress)
                _indexes(connection, meta, key_columns, progress)
            reused = True
        else:
            fd, name = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.building', dir=path.parent)
            os.close(fd)
            temporary = Path(name)
            connection = _connect(temporary)
            try:
                connection.executescript('''
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE rows(row_num INTEGER PRIMARY KEY,xml BLOB NOT NULL,sha256 TEXT NOT NULL);
                    CREATE INDEX row_count_only ON rows(row_num);
                    CREATE TABLE strings(id INTEGER PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE keys(field TEXT NOT NULL,value TEXT NOT NULL,row_num INTEGER NOT NULL,available INTEGER NOT NULL,
                                      PRIMARY KEY(field,row_num)) WITHOUT ROWID;
                    CREATE TABLE key_issues(field TEXT NOT NULL,row_num INTEGER NOT NULL,PRIMARY KEY(field,row_num)) WITHOUT ROWID;
                    CREATE TABLE indexed_columns(field TEXT PRIMARY KEY,rows_indexed INTEGER NOT NULL,issue_count INTEGER NOT NULL);
                ''')
                connection.execute('INSERT INTO meta VALUES(?,?)', ('state', _json('building')))
                count, header, root = 0, None, None
                with ZipFile(file) as archive:
                    sheets, epoch = xq._relationships(archive)
                    if sheet is None:
                        if len(sheets) != 1: raise ValueError('Multiple worksheets: specify sheet explicitly')
                        selected_sheet, member = sheets[0]
                    else:
                        found = [entry for entry in sheets if entry[0] == sheet]
                        if not found: raise ValueError('Requested sheet not found')
                        selected_sheet, member = found[0]
                    styles, formats = xq._styles(archive)
                    if 'xl/sharedStrings.xml' in archive.namelist():
                        with archive.open('xl/sharedStrings.xml') as stream:
                            for index, element in enumerate(xq._elements(stream, xq.M + 'si')):
                                connection.execute('INSERT INTO strings VALUES(?,?)', (index, xq._text(element)))
                                if index % 10000 == 0: _budget(connection, max_snapshot_bytes)
                    strings = _Strings(connection)
                    last = 0
                    with xq._ParallelXMLValidation(file, member, archive.getinfo(member).file_size, True) as validation:
                        with archive.open(member) as stream:
                            for number, raw, opening in xq._framed_rows(stream, validate=not validation.parallel):
                                if number <= last: raise ValueError('Worksheet physical rows must increase')
                                last = number
                                if number < header_row: continue
                                if number == header_row:
                                    header = {}
                                    for column, element in xq._row_cells(xq._parse_frame(raw, opening), number).items():
                                        value = xq._cell(element, strings, styles, formats, epoch)['value']
                                        if value is None or not str(value).strip(): raise ValueError('Selected header contains an empty label')
                                        label = str(value).strip()
                                        if label in header: raise ValueError('Selected header labels are duplicated')
                                        header[label] = column
                                    root = opening.decode('utf-8')
                                    if set(key_columns) - set(header): raise ValueError('Unknown declared index fields')
                                    continue
                                if header is None: raise ValueError('Selected header physical row is absent')
                                if not re.search(rb'<(?:v|is|f)(?=[\s/>])', raw): continue
                                connection.execute('INSERT INTO rows VALUES(?,?,?)',
                                    (number, zlib.compress(raw, level=1), hashlib.sha256(raw).hexdigest()))
                                count += 1
                                if count % 10000 == 0:
                                    _budget(connection, max_snapshot_bytes)
                                    if progress: progress({'stage': 'preparing_snapshot', 'rows': count})
                    if not header: raise ValueError('No usable header in snapshot')
                if xq._source_digest(file) != signature['source_sha256']:
                    raise ValueError('Source changed during snapshot construction')
                meta = {'signature': signature, 'state': 'complete', 'row_count': count,
                        'sheet': selected_sheet, 'header': header, 'root': root, 'styles': styles,
                        'formats': formats, 'epoch': epoch, 'max_snapshot_bytes': max_snapshot_bytes,
                        'validation': 'complete_worksheet_xml;header_and_indexed_cells;source_sha256_before_after'}
                connection.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [(k, _json(v)) for k, v in meta.items()])
                _budget(connection, max_snapshot_bytes)
                connection.commit()
                _indexes(connection, meta, key_columns, progress)
                _build_initial_lookup_index(connection, max_snapshot_bytes, progress)
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Snapshot database integrity check failed')
                connection.close()
                os.replace(temporary, path)
                temporary = None
            finally:
                connection.close()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                    Path(str(temporary) + '-journal').unlink(missing_ok=True)
    if xq._source_digest(file) != signature['source_sha256']:
        raise ValueError('Source changed before snapshot became available')
    with closing(_connect(path, readonly=True)) as connection:
        meta = _manifest(connection, signature)
        indexed = [row[0] for row in connection.execute('SELECT field FROM indexed_columns ORDER BY field')]
    return {'file': str(file), 'sheet': meta['sheet'], 'source_sha256': signature['source_sha256'],
            'snapshot_path': str(path), 'snapshot_bytes': path.stat().st_size, 'rows': meta['row_count'],
            'indexed_columns': indexed, 'reused': reused, 'complete': True,
            'coverage_index_maintenance': maintenance,
            'elapsed_seconds': round(time.perf_counter() - started, 6)}


def query_snapshot(file, *, index_dir, sheet=None, header_row=1, where=None, key_rows=None,
                   columns=None, limit=None, max_return_cells=500000, progress=None):
    """Query an explicitly prepared snapshot; missing snapshots fail clearly."""
    started = time.perf_counter()
    if where is not None and (not isinstance(where, dict) or any(not isinstance(k, str) or not k or
            not isinstance(v, list) or not v or any(isinstance(item, (list, dict)) for item in v) for k, v in where.items())):
        raise ValueError('where must map fields to nonempty scalar value lists')
    if columns is not None and (not isinstance(columns, (list, tuple)) or not columns or
            len(set(columns)) != len(columns) or any(not isinstance(c, str) or not c for c in columns)):
        raise ValueError('columns must be distinct nonempty field names')
    if limit is not None and (type(limit) is not int or limit < 0): raise ValueError('limit must be nonnegative')
    if type(max_return_cells) is not int or max_return_cells < 1: raise ValueError('max_return_cells must be positive')
    key_rows = xq._canonical_key_rows(key_rows)
    allowed = {name: {xq._exact(v) for v in values} for name, values in (where or {}).items()}
    tuple_fields = sorted(key_rows[0]) if key_rows else []
    tuples = {tuple(xq._exact(item[name]) for name in tuple_fields) for item in key_rows or []}
    unions = {name: {xq._exact(item[name]) for item in key_rows} for name in tuple_fields}
    filter_fields = list(dict.fromkeys([*allowed, *tuple_fields]))
    file, signature, path = _resolve(file, index_dir, sheet, header_row)
    if not path.is_file(): raise SnapshotUnavailable('No complete snapshot for the current source hash; run prepare_snapshot first')
    with _lock(path):
        connection = _connect(path)
        try:
            meta = _manifest(connection, signature)
            unknown = (set(filter_fields) | set(columns or [])) - set(meta['header'])
            if unknown: raise ValueError('Unknown query fields: ' + ', '.join(sorted(unknown)))
            maintenance = _ensure_coverage_index(connection, meta['max_snapshot_bytes'], progress)
            new_indexes = _indexes(connection, meta, filter_fields, progress)
        finally:
            connection.close()
    connection = _connect(path, readonly=True)
    try:
        meta = _manifest(connection, signature)
        requested = list(columns) if columns is not None else list(meta['header'])
        strings = _Strings(connection)
        formats = {int(k): v for k, v in meta['formats'].items()}
        root = meta['root'].encode('utf-8')
        anchor = None
        if filter_fields:
            first = filter_fields[0]
            first_values = allowed[first] if first in allowed else unions[first]
            connection.execute('CREATE TEMP TABLE requested_values(field TEXT,value TEXT,PRIMARY KEY(field,value)) WITHOUT ROWID')
            connection.execute('CREATE TEMP TABLE requested_fields(field TEXT PRIMARY KEY)')
            connection.executemany('INSERT INTO requested_fields VALUES(?)', [(field,) for field in filter_fields])
            estimates = {}
            for field in filter_fields:
                values = allowed.get(field, unions.get(field, set()))
                if field in allowed and field in unions: values = values & unions[field]
                connection.executemany('INSERT INTO requested_values VALUES(?,?)', [(field, _token(v)) for v in values])
                estimates[field] = connection.execute('''SELECT count(*) FROM keys WHERE field=? AND available=1
                    AND value IN (SELECT value FROM requested_values WHERE field=?)''', (field, field)).fetchone()[0]
            anchor = min(estimates, key=estimates.get)
            connection.execute('CREATE TEMP TABLE first_values(value TEXT PRIMARY KEY)')
            connection.executemany('INSERT INTO first_values VALUES(?)', [(_token(v),) for v in first_values])
            # Choose the smallest candidate set, but retain every problematic
            # filter cell that the original first-field prefilter could reach.
            # Otherwise switching anchors could falsely claim complete formula
            # evaluation, or drop a cached formula's unresolved expression.
            query = '''SELECT r.row_num,r.xml,r.sha256 FROM rows r JOIN
                (SELECT row_num FROM keys WHERE field=? AND available=1
                   AND value IN (SELECT value FROM requested_values WHERE field=?)
                 UNION SELECT row_num FROM key_issues
                   WHERE field IN (SELECT field FROM requested_fields)) wanted
                ON r.row_num=wanted.row_num
                WHERE EXISTS (SELECT 1 FROM keys f WHERE f.field=? AND f.row_num=r.row_num
                   AND (f.available=0 OR f.value IN (SELECT value FROM first_values)))
                ORDER BY r.row_num'''
            iterator = connection.execute(query, (anchor, anchor, first))
        else:
            iterator = connection.execute('SELECT row_num,xml,sha256 FROM rows ORDER BY row_num')
        rows, matched, examined, unavailable = [], 0, 0, 0
        issues, counts = [], {}
        selectors = {meta['header'][field]: _selector(meta['header'][field]) for field in filter_fields}

        def issue(code, number, address):
            counts[code] = counts.get(code, 0) + 1
            if len(issues) < 100: issues.append({'code': code, 'row_num': number, 'address': address})

        for number, blob, checksum in iterator:
            raw = zlib.decompress(blob)
            if hashlib.sha256(raw).hexdigest() != checksum: raise ValueError('Snapshot row checksum mismatch')
            examined += 1
            decoded = _decode_selected(raw, number, root, [meta['header'][f] for f in filter_fields],
                                       strings, meta['styles'], formats, meta['epoch'], selectors)
            seen = set()

            def read(field):
                column = meta['header'][field]
                if column not in decoded:
                    decoded.update(_decode_selected(raw, number, root, [column], strings, meta['styles'], formats, meta['epoch']))
                cell = decoded[column]
                if field not in seen:
                    if xq._has_formula(cell) and not cell['formula_cached']: issue('formula_missing_cache', number, cell['address'])
                    if cell.get('formula_state') in ('shared_formula_unresolved', 'formula_expression_unavailable'):
                        issue(cell['formula_state'], number, cell['address'])
                    if cell['data_type'] == 'e': issue('excel_error_cell', number, cell['address'])
                    seen.add(field)
                return cell

            accepted = True
            for name, values in allowed.items():
                cell = read(name)
                if not _available(cell): unavailable += 1; accepted = False; break
                if xq._exact(cell['value']) not in values: accepted = False; break
            if not accepted: continue
            if tuple_fields:
                values = []
                for name in tuple_fields:
                    cell = read(name)
                    if not _available(cell): unavailable += 1; accepted = False; break
                    if xq._exact(cell['value']) not in unions[name]: accepted = False; break
                    values.append(xq._exact(cell['value']))
                if not accepted or tuple(values) not in tuples: continue
            matched += 1
            if limit is None or len(rows) < limit:
                xq._check_return_cell_budget(len(rows) + 1, len(requested), max_return_cells)
                all_cells = xq._row_cells(xq._parse_frame(raw, root), number)
                if any(column not in set(meta['header'].values()) and any(c.find(xq.M + tag) is not None for tag in ('v', 'is', 'f'))
                       for column, c in all_cells.items()): raise ValueError('Data without a header in a returned snapshot row')
                for field in requested:
                    column = meta['header'][field]
                    if column not in decoded:
                        element = all_cells.get(column)
                        decoded[column] = (xq._blank_cell(f'{column}{number}') if element is None else
                                           xq._cell(element, strings, meta['styles'], formats, meta['epoch']))
                cells = {field: read(field) for field in requested}
                rows.append({'row_num': number, 'values': {f: c['value'] for f, c in cells.items()}, 'cells': cells})
            if progress and examined % 10000 == 0: progress({'stage': 'querying_snapshot', 'rows_examined': examined, 'matched_rows': matched})
        unresolved = sum(counts.get(code, 0) for code in ('shared_formula_unresolved', 'formula_expression_unavailable'))
        result = {'file': str(file), 'sheet': meta['sheet'], 'header_row': header_row,
            'source_sha256': signature['source_sha256'], 'columns': requested, 'where': where or {}, 'rows': rows,
            'coverage': {'scanned_rows': meta['row_count'], 'matched_rows': matched, 'returned_rows': len(rows),
                'returned_cells': len(rows) * len(requested), 'max_return_cells': max_return_cells,
                'truncated': len(rows) < matched, 'scan_complete': True, 'filter_evaluation_complete': unavailable == 0,
                'unavailable_filter_rows': unavailable, 'expression_provenance_complete': unresolved == 0,
                'unresolved_formula_expressions': unresolved, 'structure_validation_scope': 'header_filter_cells_and_returned_rows',
                'xml_well_formedness': 'complete_worksheet', 'issue_counts': counts,
                'issue_details_truncated': sum(counts.values()) > len(issues),
                'scan_mode': 'complete_source_snapshot_index', 'rows_examined_this_query': examined,
                'index_anchor_field': anchor},
            'issues': issues, 'reader': 'persistent_xml_snapshot', 'date_epoch': '1904' if meta['epoch'] else '1900',
            'snapshot': {'path': str(path), 'version': VERSION, 'complete': True,
                         'newly_indexed_columns': new_indexes, 'source_hash_verified': True,
                         'coverage_index_maintenance': maintenance},
            'cache': {'enabled': True, 'hit': True, 'kind': 'source_snapshot', 'path': str(path)}}
        if key_rows is not None: result['key_rows'] = key_rows
    finally:
        connection.close()
    if xq._source_digest(file) != signature['source_sha256']:
        raise ValueError('Source changed during snapshot query; discard result')
    result['elapsed_seconds'] = round(time.perf_counter() - started, 6)
    return result
