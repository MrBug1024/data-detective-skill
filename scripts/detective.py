"""Professional data lineage API and alias of the single data_run command line."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


class LineageReport(dict):
    """Serializable report with lightweight accessors for existing Python callers."""
    def to_dict(self):
        return dict(self)

    def save_json(self, path):
        from run_contract import atomic_json
        atomic_json(path, self)

    @property
    def nodes(self):
        return [SimpleNamespace(**item) for item in self.get('chain', {}).get('nodes', [])]

    @property
    def edges(self):
        return [SimpleNamespace(**item) for item in self.get('chain', {}).get('edges', [])]

    @property
    def confidence(self):
        return None

    @property
    def missing(self):
        return self.get('issues', []) + self.get('source_localization', {}).get('unmatched_sources', [])


class DataDetective:
    """Use the same bounded lineage pipeline from Python or the CLI."""
    def __init__(self, config_file=None, cache_dir='.dd_cache', verbose=False):
        self.config = json.loads(Path(config_file).read_text(encoding='utf-8-sig')) if config_file else {}
        self.cache_dir, self.verbose = cache_dir, verbose

    def trace(self, result_file, result_row=None, source_files=(), output_json=None,
              output_html=None, result_sheet=None, *, rows=None):
        from trace_pipeline import trace_sources
        report = LineageReport(trace_sources(result_file, source_files, row=result_row, rows=rows,
            result_sheet=result_sheet, config=self.config, cache_dir=self.cache_dir))
        if output_json:
            report.save_json(output_json)
        if output_html:
            from trace_wall import generate_trace_wall
            generate_trace_wall(report, output_html)
        return report

    def find_sources(self, target, source_files, *, limit=30):
        from evidence_retrieval import explore_evidence
        from trace_wall import attach_trace_graph
        return LineageReport(attach_trace_graph(explore_evidence(target, source_files,
            cache_dir=self.cache_dir, limit=limit, config=self.config)))

    @staticmethod
    def summary(report):
        status = report.get('source_localization', {}).get('status', 'not_assessed')
        return f"来源定位: {status}；节点: {len(report.nodes)}；待处理: {len(report.missing)}"


def main(argv=None):
    from data_run import main as run
    return run(argv)


if __name__ == '__main__':
    raise SystemExit(main())
