#!/bin/bash
set -e
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "========================================"
echo "🔍 刑侦调查引擎 — 完整演示"
echo "========================================"

echo ""
echo "📦 安装依赖..."
pip install -r "$SKILL_DIR/scripts/requirements.txt" -q

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 演示1：数据溯源模式"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "$SKILL_DIR/examples"
python generate_examples.py
python "$SKILL_DIR/scripts/detective.py" trace \
    --result "$SKILL_DIR/examples/R1.xlsx" \
    --row 2 \
    --sources "$SKILL_DIR/examples/A.xlsx" "$SKILL_DIR/examples/B.xlsx" \
              "$SKILL_DIR/examples/C.xlsx" "$SKILL_DIR/examples/D.xlsx" \
              "$SKILL_DIR/examples/rules.md" \
    --out "$SKILL_DIR/examples/data_report.json" \
    --html "$SKILL_DIR/examples/data_wall.html"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🕵️  演示2：实体调查模式"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "$SKILL_DIR/examples/entity_case"
python generate_entity_case.py
python "$SKILL_DIR/scripts/detective.py" investigate \
    --mode entity --target "张伟华" \
    --sources contracts.xlsx profiles.xlsx transactions.xlsx background.md \
    --out entity_report.json \
    --html entity_wall.html

echo ""
echo "========================================"
echo "✅ 全部演示完成！"
echo ""
echo "📁 输出文件："
echo "   examples/data_wall.html   — 数据溯源调查墙"
echo "   examples/entity_case/entity_wall.html — 实体调查墙"
echo ""
echo "→ 用浏览器打开上述 HTML 文件即可查看交互式调查墙"
echo "========================================"
