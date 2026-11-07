# Data Detective · 专业数据溯源

面向 Agent 的数据溯源 skill。输入结果表、业务标识或目标描述，在表格、JSON、数据库导出与文档中定位原始记录，核对字段对应、连接和计算规则，交付可复查证据与交互式溯源墙。

技能名 **data-detective**，版本 **4.0.0**。所有入口共用数据溯源流程；实体、日期、金额与标识抽取用于发现关联键和原文位置。

## 使用

```text
使用 $data-detective 追踪这份结果表的全部记录，找到原始来源和规则，解释字段差异，并生成溯源墙。
```

```powershell
# 自动识别表头、按键定位、逐字段核对；默认首条数据
python scripts/data_run.py trace --result result.xlsx --source-dir data --out-dir outputs/trace
# 整份结果一次批量处理
python scripts/data_run.py trace --result result.xlsx --all-rows --sources detail.xlsx summary.csv policy.md --out-dir outputs/batch
# 只有线索时使用同一入口
python scripts/data_run.py trace --target "订单 ORD-2026-001" --source-dir data --out-dir outputs/lookup
# 有明确计算规则时执行计划
python scripts/data_run.py reconcile --plan lineage-plan.json --rows 2 3 --out-dir outputs/check
```

在用户工作目录运行。安装后将 `scripts/` 替换为技能安装目录的绝对路径；输入原件只读，缓存和结果写在工作目录。`--source-dir` 默认当前层，`--recursive` 才包括子目录。复杂任务可显式列来源文件。

每次产生报告、离线溯源墙、机器状态和进度事件。来源定位、文档证据、计算核验、规则适用与历史执行状态分别报告；Agent可以直接从终端最后一个完成事件取得本次产物路径和下一步动作。

## 证据和规模

- 稳定标识、复合键与作用域优先；字段值、名称和文本只作为各自层次的证据。多跳保留实际连接键来自哪条记录，冲突与竞争解释不丢弃。
- 文件内容SHA、工作表/物理行/单元格、JSON Pointer，以及文档页/段/字符区间均可回查。原文复制不增加独立证据份数，文档指令不会被执行。
- 表头探查按样本提取共享字符串，大表合并查询，不为每个结果行反复全扫。重复跨键查询可使用显式准备的源快照索引；首次构建与查询成本分别记录。
- 返回预算、坏记录、损坏XML、读取失败、未覆盖范围和缓存失效均明确报告。拒绝或待决是可操作状态，不能用模糊相似覆盖精确标识冲突。
- 明确规则下使用Decimal重算，保留来源记录、差额与舍入条件；计算相符不等于证明历史上执行过同一处理。

结构化输入涵盖XLSX、CSV/TSV、JSON/JSONL/NDJSON及静态SQL导出；预算内旧XLS通过可选xlrd读取。文档覆盖TXT/MD、DOCX、数字PDF，扫描件通过绑定原件的OCR sidecar接入。不执行任意SQL，不默认调用外部模型或联网服务。

详见 [技能入口](SKILL.md)、[配置和溯源工作流](references/data-lineage-workflow.md)、[执行与反馈](references/data-execution.md)、[格式与定位边界](references/file-ingestion.md)、[方法及验证依据](references/lineage-methods.md)。

## 安装

需要Python 3.10+。优先使用项目虚拟环境，并安装同一解释器的依赖：

```powershell
python -m pip install -r scripts/requirements.txt
python scripts/install_skill.py --target codex
# 或指定技能父目录
python scripts/install_skill.py --dest /path/to/skills
```

安装只复制运行资源，不包含真实数据、测试答案、缓存或本地环境。已有同名目录不会被静默覆盖。可用 `python scripts/package_skill.py --out dist/data-detective.zip` 生成带SHA256的便携包。运行Python CLI无需API key。

从源码安装时仓库根目录就是skill：

```text
使用 $skill-installer 从 https://github.com/MrBug1024/data-detective-skill 安装技能，仓库内路径为 .，安装名称为 data-detective。
```

## 验证与开发

```powershell
python -m pip install -r requirements-dev.txt
python scripts/package_skill.py --check
python -m unittest discover -s tests -v
python test/lineage/run_eval.py --help
python scripts/benchmark.py --help
```

[多场景评测](test/README.md)保留原始输入、独立预期和完整产物，逐项检查误连、漏连、歧义、覆盖、引用及冷/热性能。实际测量只说明所覆盖的数据，不能把某个小样例得分解释为所有材料上的准确率。

4.x将 `detective.py` 作为统一命令入口的别名，`trace.py` 等价于 `data_run.py trace`。Python的 `DataDetective.trace` 同样进入通用管线，输出的匹配分不再伪装为置信概率。批处理请消费 `status.json` 和退出码，不依赖旧版控制台文案。
