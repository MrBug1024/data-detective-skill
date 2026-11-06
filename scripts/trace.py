#!/usr/bin/env python3
"""
trace.py — 数据链路侦探 CLI 入口
用法: python trace.py --result R1.xlsx --row 1 --sources A.xlsx B.xlsx --out chain.json --html wall.html
"""
import sys
import os
import json
import argparse

# 确保同目录的模块可以被import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from detective import DataDetective


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(
        description="🔍 数据链路侦探 — 从结果数据反向溯源到业务数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python trace.py --result R1.xlsx --row 1 --sources A.xlsx B.xlsx C.xlsx D.xlsx rules.md --out chain.json --html wall.html
  python trace.py --result R1.xlsx --row 1 --sources A.xlsx B.xlsx --validate previous_chain.json
  python trace.py --result R1.xlsx --row 1 --sources A.xlsx B.xlsx --config config.json --verbose
        """
    )
    parser.add_argument('--result', required=True, help='结果表文件路径 (xlsx/csv)')
    parser.add_argument('--row', type=int, required=True, help='结果表目标行号（1-based，第2行=第1数据行）')
    parser.add_argument('--sheet', help='结果工作表名称；多工作表文件必须明确指定')
    parser.add_argument('--sources', nargs='+', required=True, help='业务源文件列表 (xlsx/csv/pdf/docx/txt/md)')
    parser.add_argument('--out', default='chain.json', help='输出链路JSON路径 (默认: chain.json)')
    parser.add_argument('--html', default='wall.html', help='输出HTML调查墙路径 (默认: wall.html)')
    parser.add_argument('--config', help='配置文件路径 (JSON格式)')
    parser.add_argument('--validate', help='用于验证的已有链路JSON文件')
    parser.add_argument('--cache-dir', default='.dd_cache', help='索引缓存目录')
    parser.add_argument('--verbose', action='store_true', help='详细输出')
    parser.add_argument('--summary', action='store_true', help='打印人类可读摘要')
    parser.add_argument('--no-html', action='store_true', help='不生成HTML报告')
    parser.add_argument('--no-json', action='store_true', help='不输出JSON文件')

    args = parser.parse_args()

    # 检查文件存在
    if not os.path.exists(args.result):
        print(f"❌ 结果文件不存在: {args.result}", file=sys.stderr)
        sys.exit(1)

    missing_sources = [f for f in args.sources if not os.path.exists(f)]
    if missing_sources:
        print(f"⚠️  以下源文件不存在: {missing_sources}", file=sys.stderr)

    # 创建侦探
    detective = DataDetective(
        config_file=args.config,
        cache_dir=args.cache_dir,
        verbose=args.verbose,
    )

    try:
        # 验证模式
        if args.validate:
            if not os.path.exists(args.validate):
                print(f"❌ 验证文件不存在: {args.validate}", file=sys.stderr)
                sys.exit(1)
            print(f"🔍 验证模式: {args.result} 第{args.row}行 vs {args.validate}")
            result = detective.validate(
                result_file=args.result,
                result_row=args.row,
                source_files=args.sources,
                existing_chain_file=args.validate,
                result_sheet=args.sheet,
            )
            print(f"\n📊 验证结果:")
            print(f"  状态: {result['validation_status']}")
            print(f"  置信度变化: {result['previous_confidence']:.2f} → {result['new_confidence']:.2f}")
            print(f"  {result['message']}")
            return

        # 溯源模式
        print(f"🔍 开始溯源: {args.result} 第{args.row}行")
        print(f"   源文件: {', '.join(os.path.basename(f) for f in args.sources)}")

        chain = detective.trace(
            result_file=args.result,
            result_row=args.row,
            source_files=args.sources,
            output_json=None if args.no_json else args.out,
            output_html=None if args.no_html else args.html,
            result_sheet=args.sheet,
        )

        # 摘要输出
        if args.summary or args.verbose:
            print("\n" + detective.summary(chain))
        else:
            # 简洁输出
            print(f"\n✅ 溯源完成!")
            print(f"   链路ID: {chain.chain_id}")
            print(f"   整体置信度: {chain.confidence*100:.1f}%")
            print(f"   关联节点: {len(chain.nodes)-1} 个")
            print(f"   关联边: {len(chain.edges)} 条")
            if chain.missing:
                print(f"   ⚠️  缺失项: {len(chain.missing)}")
            if chain.conflicts:
                print(f"   ❌ 冲突项: {len(chain.conflicts)}")
            if not args.no_json:
                print(f"   📄 JSON: {args.out}")
            if not args.no_html:
                print(f"   🌐 HTML调查墙: {args.html}")
                print(f"      → 在浏览器中打开 {args.html} 查看交互式调查墙")

    except ValueError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        if args.verbose:
            import traceback
            traceback.print_exc()
        print(f"❌ 意外错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
