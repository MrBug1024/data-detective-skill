"""Trace data sources, inspect evidence and verify calculations with explicit completion feedback.

Exit 0: requested stage passed; 2: unresolved matches/gaps/mismatches; 1: execution failure.
Neither exit 0 nor a rendered wall proves historical lineage.
"""
from __future__ import annotations
import argparse
import contextlib
from decimal import Decimal
import json
from pathlib import Path
import sys

from run_contract import Run, atomic_json


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), parse_float=Decimal)


def load_filter(path):
    # Exact decimal query tokens must survive JSON serialization and never
    # round through binary floats. Identifiers should be quoted by the caller.
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), parse_float=str) if path else None


def execute(args, run):
    if args.operation == 'prepare':
        from xlsx_snapshot import prepare_snapshot
        run.event('preparing_source_snapshot')
        report = prepare_snapshot(args.file, index_dir=args.index_dir, sheet=args.sheet,
            header_row=args.header_row, key_columns=args.key_columns,
            max_snapshot_bytes=args.max_snapshot_bytes,
            progress=lambda event: run.event(event.get('phase', 'preparing_source_snapshot'),
                **{key: value for key, value in event.items() if key != 'phase'}))
        report['analysis_stage'] = 'source_index_preparation'
        return report
    if args.operation == 'reconcile':
        from reconcile import verify_plan, verify_plan_batch
        plan = load(args.plan)
        run.event('reconciling', requested_rows=len(args.rows) if args.rows else 1)
        return (verify_plan_batch(plan, args.rows, Path(args.plan).resolve().parent)
                if args.rows else verify_plan(plan, Path(args.plan).resolve().parent))
    if args.operation == 'trace':
        progress = lambda event: run.event(event.get('phase', 'tracing'),
            **{key: value for key, value in event.items() if key != 'phase'})
        run.event('profiling_sources', source_files=len(args.sources))
        if args.target is not None:
            from evidence_retrieval import explore_evidence
            from trace_wall import attach_trace_graph
            return attach_trace_graph(explore_evidence(args.target, args.sources, cache_dir=args.cache_dir,
                limit=args.evidence_limit, progress=progress, config=load(args.config) if args.config else None))
        from trace_pipeline import trace_sources
        return trace_sources(args.result, args.sources, row=args.row,
            rows='all' if args.all_rows else args.rows, result_sheet=args.sheet,
            config=load(args.config) if args.config else None, cache_dir=args.cache_dir,
            progress=progress)
    if args.operation == 'query':
        run.event('extracting')
        if Path(args.file).suffix.lower() in ('.csv', '.tsv', '.jsonl', '.ndjson'):
            if args.index_dir:
                raise ValueError('--index-dir applies to prepared XLSX sources only')
            if args.sheet is not None or args.header_row != 1:
                raise ValueError('--sheet/--header-row are XLSX options; adapt text headers explicitly')
            from record_query import query_records
            report = query_records(args.file, where=load_filter(args.where_json),
                key_rows=load_filter(args.keys_json), columns=args.columns, limit=args.limit,
                encoding=args.encoding, delimiter=args.delimiter,
                max_return_cells=args.max_return_cells, max_return_bytes=args.max_return_bytes,
                progress=lambda event: run.event('scanning', **event))
            report['analysis_stage'] = 'exact_extraction'
            return report
        if Path(args.file).suffix.lower() != '.xlsx':
            raise ValueError('query supports XLSX/CSV/TSV/JSONL/NDJSON; use trace for small mixed sources')
        from xlsx_query import query_xlsx
        report = query_xlsx(args.file, sheet=args.sheet, header_row=args.header_row,
            where=load_filter(args.where_json), key_rows=load_filter(args.keys_json),
            columns=args.columns, limit=args.limit, max_return_cells=args.max_return_cells,
            shared_string_memory_bytes=args.shared_string_memory_bytes,
            snapshot_dir=args.index_dir,
            cache_dir=None if args.no_cache else args.cache_dir,
            progress=lambda count: run.event('scanning', scanned_rows=count))
        report['analysis_stage'] = 'exact_extraction'
        return report
    run.event('reading_existing_report')
    report = json.loads(Path(args.report).read_text(encoding='utf-8-sig'))
    if not isinstance(report, dict):
        raise ValueError('Report must be a JSON object')
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    query = sub.add_parser('query', help='Batch exact XLSX/CSV/TSV/JSONL extraction, not lineage verification')
    query.add_argument('--file', required=True)
    query.add_argument('--where-json')
    query.add_argument('--keys-json')
    query.add_argument('--columns', nargs='+')
    query.add_argument('--sheet')
    query.add_argument('--header-row', type=int, default=1)
    query.add_argument('--limit', type=int)
    query.add_argument('--max-return-cells', type=int, default=500000)
    query.add_argument('--max-return-bytes', type=int, default=64 * 1024 * 1024,
                       help='CSV/JSONL retained output byte budget')
    query.add_argument('--shared-string-memory-bytes', type=int, default=16 * 1024 * 1024,
                       help='XLSX shared string retained-value budget')
    query.add_argument('--encoding', default='utf-8-sig', help='CSV encoding; JSONL requires UTF-8')
    query.add_argument('--delimiter', help='CSV delimiter')
    query.add_argument('--no-cache', action='store_true')
    query.add_argument('--cache-dir', default='.dd_cache/xlsx_query')
    query.add_argument('--index-dir', help='Use an explicitly prepared source snapshot for new keys/projections')
    prepare = sub.add_parser('prepare', help='Prepare a reusable source snapshot and exact key indexes')
    prepare.add_argument('--file', required=True)
    prepare.add_argument('--index-dir', default='.dd_cache/source_snapshots')
    prepare.add_argument('--sheet')
    prepare.add_argument('--header-row', type=int, default=1)
    prepare.add_argument('--key-columns', nargs='*', default=[])
    prepare.add_argument('--max-snapshot-bytes', type=int, default=2 * 1024 ** 3)
    rec = sub.add_parser('reconcile', help='Recompute an explicit source-cited plan')
    rec.add_argument('--plan', required=True)
    rec.add_argument('--rows', nargs='+', type=int)
    trace = sub.add_parser('trace', help='Profile headers, batch source lookup, compare fields, retrieve cited rules')
    subject = trace.add_mutually_exclusive_group(required=True)
    subject.add_argument('--result', help='Structured result file to trace')
    subject.add_argument('--target', help='Identifier or description to locate in the supplied sources')
    scope = trace.add_mutually_exclusive_group()
    scope.add_argument('--row', type=int, help='Physical result row (default: first data row after detected header)')
    scope.add_argument('--rows', nargs='+', type=int, help='Explicit physical result rows')
    scope.add_argument('--all-rows', action='store_true', help='Trace every result data row in one batch')
    trace.add_argument('--sources', nargs='+', default=[])
    trace.add_argument('--source-dir', action='append', default=[], help='Read source files in this directory; excludes the result and temporary files')
    trace.add_argument('--recursive', action='store_true', help='Include subdirectories of --source-dir')
    trace.add_argument('--sheet')
    trace.add_argument('--config', help='Optional JSON: explicit headers, key_map and field_map when inference is ambiguous')
    trace.add_argument('--cache-dir', default='.dd_cache')
    trace.add_argument('--evidence-limit', type=int, default=30, help='Maximum related passages for --target; truncation is reported')
    render = sub.add_parser('report', help='Classify and display an existing report; does not reverify sources')
    render.add_argument('--report', required=True)
    for command in (query, rec, trace, render, prepare):
        command.add_argument('--out-dir', type=Path, required=True)
        command.add_argument('--no-html', action='store_true')
    args = parser.parse_args(argv)
    if args.operation == 'trace':
        if args.target is not None and (args.row is not None or args.rows or args.all_rows or args.sheet):
            parser.error('--row/--rows/--all-rows/--sheet require --result')
        extensions = {'.xlsx', '.xls', '.csv', '.tsv', '.json', '.jsonl', '.ndjson',
                      '.txt', '.md', '.pdf', '.docx', '.sql', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}
        discovered = list(args.sources)
        for name in args.source_dir:
            directory = Path(name)
            if not directory.is_dir():
                parser.error(f'Source directory does not exist: {directory}')
            for source in sorted(directory.rglob('*') if args.recursive else directory.iterdir()):
                if (source.is_file() and source.suffix.lower() in extensions
                        and not source.name.startswith(('~$', '.'))
                        and not source.name.endswith('.ocr.json')
                        and (not args.result or source.resolve() != Path(args.result).resolve())
                        and args.out_dir.resolve() not in source.resolve().parents
                        and not any(part.startswith('.') for part in source.relative_to(directory).parts)):
                    discovered.append(str(source))
        args.sources = list(dict.fromkeys(str(Path(p).resolve()) for p in discovered))
        if not args.sources:
            parser.error('trace needs --sources or a nonempty --source-dir')
    # Never replace a supplied input with a generated artifact.
    targets = {(args.out_dir / name).resolve() for name in ('report.json', 'wall.html', 'status.json', 'events.jsonl')}
    inputs = [getattr(args, name, None) for name in ('file', 'plan', 'report', 'result', 'where_json', 'keys_json', 'config')]
    inputs.extend(getattr(args, 'sources', []))
    if args.operation == 'reconcile':
        try:
            plan = load(args.plan)
            def referenced_files(value):
                if isinstance(value, dict):
                    if isinstance(value.get('file'), str):
                        yield str(Path(args.plan).resolve().parent / value['file'])
                    for child in value.values():
                        yield from referenced_files(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from referenced_files(child)
            inputs.extend(referenced_files(plan))
        except (OSError, ValueError):
            pass  # The execution path records the actual parse/read failure.
    if any(Path(p).resolve() in targets for p in inputs if p):
        parser.error('Output artifact paths must not overlap input files; choose a separate --out-dir')
    run = None
    try:
        run = Run(args.out_dir, args.operation)
        with contextlib.redirect_stdout(sys.stderr):
            report = execute(args, run)
        run.event('writing_report')
        atomic_json(args.out_dir / 'report.json', report)
        artifacts = {'report': args.out_dir / 'report.json'}
        if not args.no_html:
            run.event('rendering_wall')
            if report.get('analysis_stage') in ('source_trace', 'evidence_search'):
                from trace_wall import generate_trace_wall
                generate_trace_wall(report, args.out_dir / 'wall.html')
            else:
                from data_wall import generate_data_wall
                generate_data_wall(report, args.out_dir / 'wall.html')
            artifacts['wall'] = args.out_dir / 'wall.html'
        status = run.finish(report, artifacts)
    except (Exception, KeyboardInterrupt) as exc:
        error = {'code': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'execution_error',
                 'type': type(exc).__name__, 'message': str(exc)[:1000]}
        report = {'status': 'error', 'error': error, 'confidence': None}
        artifacts = {}
        if run:
            try:
                atomic_json(args.out_dir / 'report.json', report)
                artifacts['report'] = args.out_dir / 'report.json'
                if not args.no_html:
                    try:
                        from data_wall import generate_data_wall
                        generate_data_wall(report, args.out_dir / 'wall.html')
                        artifacts['wall'] = args.out_dir / 'wall.html'
                    except Exception as wall_error:
                        error['wall_error_type'] = type(wall_error).__name__
                status = run.finish(report, artifacts, error)
            except Exception as output_error:
                status = {**run.status, 'state': 'failed', 'execution': 'failed', 'exit_code': 1,
                          'outcome': 'output_error', 'phase': 'finished', 'error': error, 'artifacts': {},
                          'agent_action': 'repair_input_or_environment_then_retry',
                          'output_error_type': type(output_error).__name__}
                try:
                    atomic_json(args.out_dir / 'status.json', status)
                except OSError:
                    pass  # stdout remains the last available failure channel.
        else:
            status = {'state': 'failed', 'execution': 'failed', 'exit_code': 1,
                      'outcome': 'output_error', 'error': error, 'artifacts': {}}
    finally:
        if run:
            run.close()
    print(json.dumps({'event': 'data_run_finished', **status}, ensure_ascii=False, allow_nan=False))
    return status['exit_code']


if __name__ == '__main__':
    sys.exit(main())
