#!/usr/bin/env python3
"""
detective.py — 调查引擎统一 CLI 入口 v3
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from investigator import Investigator
from wall_renderer import generate_wall


class DataDetective:
    """Compatibility facade for the original DATA API and trace.py."""

    def __init__(self, config_file=None, cache_dir='.dd_cache', verbose=False):
        self.config = config_from_file(config_file)
        self.cache_dir = cache_dir
        self.verbose = verbose

    def trace(self, result_file, result_row, source_files, output_json=None, output_html=None, result_sheet=None):
        from parsers import FileParser
        from indexer import build_index
        from matcher import MatchConfig
        from linker import ChainLinker
        unit = FileParser().parse_result_row(result_file, result_row, sheet_name=result_sheet)
        if unit is None:
            raise ValueError(f'结果文件 {result_file} 中未找到第 {result_row} 行')
        index = build_index(source_files, self.cache_dir)
        chain = ChainLinker(index, MatchConfig(self.config)).build_chain(unit, source_files)
        if output_json:
            chain.save_json(output_json)
        if output_html:
            from html_generator import generate_html
            generate_html(chain, output_html)
        return chain

    def validate(self, result_file, result_row, source_files, existing_chain_file, result_sheet=None):
        with open(existing_chain_file, encoding='utf-8-sig') as stream:
            old = json.load(stream)
        chain = self.trace(result_file, result_row, source_files, result_sheet=result_sheet or old.get('result_ref', {}).get('sheet'))
        old_score = old.get('confidence', 0)
        delta = chain.confidence - old_score
        status = 'confidence_increased' if delta > .05 else 'confidence_decreased' if delta < -.05 else 'unchanged'
        return {'validation_status': status, 'previous_confidence': old_score,
                'new_confidence': chain.confidence,
                'message': '重跑字段匹配；匹配分变化不代表结论已获证实'}

    @staticmethod
    def summary(chain):
        return f'匹配分（非概率）: {chain.confidence:.3f}\n节点: {len(chain.nodes)}；缺失: {len(chain.missing)}；冲突: {len(chain.conflicts)}'


def cmd_trace(args):
    """数据溯源模式"""
    inv = Investigator(
        config_from_file(args.config),
        cache_dir=args.cache_dir,
        verbose=args.verbose
    )
    report = inv.trace_data(
        result_file=args.result,
        result_row=args.row,
        source_files=args.sources,
        result_sheet=args.sheet,
    )
    _output(report, args)


def cmd_investigate(args):
    """实体/事件/自动调查模式"""
    config = config_from_file(args.config)
    if args.start is not None or args.limit is not None:
        if args.mode != 'entity':
            raise ValueError('--start/--limit 仅用于显式 entity 模式')
        if args.start is not None: config['entity_start'] = args.start
        if args.limit is not None: config['entity_max_units'] = args.limit
    inv = Investigator(
        config,
        cache_dir=args.cache_dir,
        verbose=args.verbose
    )
    mode = args.mode.lower()
    if mode == 'entity':
        report = inv.investigate_entity(args.target, args.sources)
    elif mode in ('criminal', 'event'):
        report = inv.investigate_criminal(args.target, args.sources)
    else:  # auto
        report = inv.investigate_auto(args.target, args.sources)
    _output(report, args)


def cmd_validate(args):
    """验证结论"""
    with open(args.report, encoding='utf-8-sig') as f:
        existing = json.load(f)
    print(f"[INFO] Validating report {args.report} with {args.new_evidence}")
    # Re-run investigation with new evidence added to sources
    config = config_from_file(args.config) if args.config else existing.get('_config', {})
    inv = Investigator(config, cache_dir=args.cache_dir, verbose=args.verbose)
    sources = list(dict.fromkeys(existing.get('_source_files', []) + [args.new_evidence]))
    target = existing.get('target', '')
    inv_type = existing.get('investigation_type', 'auto')
    if 'data' in inv_type and existing.get('result_ref'):
        ref = existing['result_ref']
        report = inv.trace_data(ref['file'], ref['row'], sources, result_sheet=ref.get('sheet'))
    elif 'entity' in inv_type:
        report = inv.investigate_entity(target, sources)
    elif 'criminal' in inv_type or 'event' in inv_type:
        report = inv.investigate_criminal(target, sources)
    else:
        report = inv.investigate_auto(target, sources)

    # Compare confidence
    old_conf = existing.get('confidence')
    new_conf = report.confidence
    print(f"\n📊 验证结果:")
    if isinstance(old_conf, (int, float)) and isinstance(new_conf, (int, float)):
        print(f"   匹配分: {old_conf:.3f} → {new_conf:.3f}；变化不代表结论已获证实")
    else:
        print('   置信度: 未评估；已重新整理全部提供的证据')
    _output(report, args)


def cmd_portrait(args):
    """Compile appearance observations; drawing is a separate tool operation."""
    from portrait import run
    try:
        report = run(args.case, args.out, args.prompt_out, args.previous)
    except (ValueError, OSError, TypeError) as exc:
        raise SystemExit(f'portrait: {exc}')
    print(f"{report['status']}; render_state={report['render_state']}; output={args.out}")


def _output(report, args):
    data = report.to_dict()
    # Print summary
    conf = data.get('confidence')
    label = '证据整理完成，待 Agent 研判' if data.get('analysis_stage') == 'evidence_packet_only' else '候选检索完成'
    print(f"\n✅ {label}")
    print(f"   调查类型: {data.get('investigation_type','?')}")
    print(f"   目标:     {data.get('target','?')}")
    print(f"   匹配分:   {conf:.3f}（非概率）" if isinstance(conf, (int, float)) else '   置信度:   未评估')
    if data.get('analysis_stage'):
        print(f"   分析阶段: {data['analysis_stage']}")
    print(f"   节点数:   {len(data.get('chain',{}).get('nodes',[]))}")
    print(f"   证据条数: {len(data.get('evidence',[]))}")
    print(f"   时间线:   {len(data.get('timeline',[]))} 条时间记录")
    print(f"   结论数:   {len(data.get('conclusions',[]))}")
    if data.get('anomalies'):
        print(f"   ⚠️ 异常:   {len(data['anomalies'])} 处")
    if data.get('missing'):
        print(f"   缺失:     {len(data['missing'])} 项")

    if not args.no_json:
        report.save_json(args.out)
        print(f"   📄 JSON → {args.out}")

    if not args.no_html:
        generate_wall(data, args.html)
        print(f"   🌐 调查墙 → {args.html}")
        print(f"      → 在浏览器中打开查看交互式马赛克调查墙")

    # Print conclusions
    for c in data.get('conclusions', []):
        icon = {'confirmed': '✅', 'refuted': '❌'}.get(c.get('status'), '🔶')
        print(f"\n   {icon} [{c.get('status','?').upper()}] {c.get('statement','')}")
        score = c.get('confidence')
        print(f"      匹配分: {score:.3f}（非概率）" if isinstance(score, (int, float)) else '      置信度: 未评估')
        if c.get('missing_evidence'):
            for m in c['missing_evidence'][:2]:
                print(f"      待补充: {m}")


def config_from_file(path: str) -> dict:
    if path:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    return {}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(
        description="🔍 调查引擎 — 数据溯源 · 实体调查 · 刑侦模式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 数据溯源
  python detective.py trace --result R1.xlsx --row 2 --sources A.xlsx B.xlsx rules.md

  # 实体调查
  python detective.py investigate --mode entity --target "张伟华" --sources *.xlsx

  # 刑侦模式（event 为兼容别名）
  python detective.py investigate --mode criminal --target "2023-07-20异常订单" --sources *.xlsx *.md

  # 自动模式
  python detective.py investigate --mode auto --target "调查C000500客户" --sources *.xlsx

  # 刑侦面部特征规格（由 Agent 先核对原始描述；本命令不生成图片）
  python detective.py portrait --case observations.json --out portrait.json --prompt-out portrait-prompt.txt
        """
    )
    sub = parser.add_subparsers(dest='cmd')

    # trace
    p_trace = sub.add_parser('trace', help='数据溯源：从结果行反向追踪业务数据')
    p_trace.add_argument('--result', required=True)
    p_trace.add_argument('--row', type=int, default=2)
    p_trace.add_argument('--sheet', help='结果工作表名称；多工作表文件必须明确指定')
    p_trace.add_argument('--sources', nargs='+', required=True)
    _add_common(p_trace)

    # investigate
    p_inv = sub.add_parser('investigate', help='多维调查：实体/刑侦/自动')
    p_inv.add_argument('--mode', default='auto', choices=['entity','criminal','event','auto'])
    p_inv.add_argument('--target', required=True)
    p_inv.add_argument('--sources', nargs='+', required=True)
    p_inv.add_argument('--start', type=int, help='ENTITY命中单元起始偏移，从0开始')
    p_inv.add_argument('--limit', type=int, help='ENTITY本页单元数，1..1000')
    _add_common(p_inv)

    # validate
    p_val = sub.add_parser('validate', help='验证已有结论')
    p_val.add_argument('--report', required=True)
    p_val.add_argument('--new-evidence', required=True)
    _add_common(p_val)

    p_portrait = sub.add_parser('portrait', help='编译来源可追溯的面部特征规格与绘图请求，不推测长相')
    p_portrait.add_argument('--case', required=True)
    p_portrait.add_argument('--out', required=True)
    p_portrait.add_argument('--prompt-out')
    p_portrait.add_argument('--previous', help='保留旧版并记录变化')

    args = parser.parse_args()

    if args.cmd == 'trace':
        cmd_trace(args)
    elif args.cmd == 'investigate':
        cmd_investigate(args)
    elif args.cmd == 'validate':
        cmd_validate(args)
    elif args.cmd == 'portrait':
        cmd_portrait(args)
    else:
        parser.print_help()


def _add_common(p):
    p.add_argument('--out', default='report.json')
    p.add_argument('--html', default='wall.html')
    p.add_argument('--config', default=None)
    p.add_argument('--cache-dir', default='.dd_cache')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--no-json', action='store_true')
    p.add_argument('--no-html', action='store_true')


if __name__ == '__main__':
    main()
