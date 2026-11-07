"""Generate and trace a real BIFF XLS; optional pandas/xlrd/xlwt are required."""
import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--dependency-dir', type=Path)
    args = parser.parse_args()
    if args.dependency_dir:
        sys.path.insert(0, str(args.dependency_dir.resolve()))
    import xlwt
    import xlrd
    import pandas
    sys.path.insert(0, str(ROOT / 'scripts'))
    from trace_pipeline import trace_sources
    args.out.mkdir(parents=True, exist_ok=True)
    source = args.out / 'legacy_batch.xls'
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('Manufacturing')
    matrix = [['batch_id', 'material_id', 'mass'], ['BATCH-0007', 'MAT-9', 12.5],
              ['BATCH-0007', 'MAT-8', 20.0], ['BATCH-0008', 'MAT-9', 12.5]]
    for row, values in enumerate(matrix):
        for column, value in enumerate(values):
            sheet.write(row, column, value)
    workbook.save(str(source))
    result = args.out / 'result.csv'
    with result.open('w', encoding='utf-8', newline='') as handle:
        csv.writer(handle).writerows([matrix[0], matrix[1]])
    packet = trace_sources(result, [source], rows='all', cache_dir=args.out / 'cache',
                           config={'sources': {source.name: {'keys': ['batch_id', 'material_id'],
                                                            'columns': ['batch_id', 'material_id', 'mass']}}})
    matches = packet['result_rows'][0]['source_matches']
    checks = {'localized': packet['source_localization']['status'] == 'located',
              'correct_physical_location': len(matches) == 1 and matches[0]['row_num'] == 2 and matches[0]['sheet'] == 'Manufacturing',
              'leading_zero_string_preserved': len(matches) == 1 and matches[0]['values']['batch_id'] == 'BATCH-0007',
              'full_ingestion': packet['coverage']['ingestion_complete']}
    (args.out / 'report.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'format': 'BIFF XLS', 'synthetic_only': True, 'checks': checks,
               'dependencies': {'xlrd': xlrd.__version__, 'xlwt': xlwt.__VERSION__, 'pandas': pandas.__version__},
               'passed': all(checks.values())}
    (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
