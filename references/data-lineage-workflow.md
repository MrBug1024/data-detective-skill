# 数据溯源工作流

## 可重算的来源关系

执行入口、机器完成反馈和大数据内存边界见[执行参考](data-execution.md)。保留本文件的字段/连接/规则要求，不能用运行成功状态替代业务证据。

1. 锁定结果文件/表/物理行号（含表头），记录文件版本、币种、单位、时区、数据有效时间。读取[格式与覆盖](file-ingestion.md)。Excel显示值、公式、缓存值不同；检查 `metadata.cells`，缺缓存不得当成空值原始事实。SQL同一行多元组要加元组号/偏移定位。
2. 先稳定键，再联合键。ID 保留前导零；跨表的同值不同字段不是主键关系。明确一对一/一对多、键空值和重复率；名称/金额/日期只能生成候选。模糊近似不能覆盖精确标识冲突。
3. 对每个结果字段保留来源和对应状态；直接字段按稳定键、原行和原值对照，改名建议保留推断方法。派生变换（过滤、连接、聚合、舍入、汇率、单位、时效条件）另需规则及实际重算，不能为了凑数事后发明公式。
4. 同时报出竞争候选，特别是相同金额/姓名的不同记录。正向重算结果，列明预期值、实得值、差额/容差及容差来源；数值相等仍不能独立证明真实血缘。
5. 已证实的计算关系与“检索到相似行”分开。报告未连接源、键冲突、规则缺失和只匹配部分字段。缺表不能被低分边补齐。统一入口的 `chain.confidence` 为 null，各结论按证据状态说明，不输出未经校准的正确概率。

性能：一次索引后复用可寻址单元；精确候选先过滤再做昂贵匹配。真实场景测冷/热缓存耗时、行数和候选上限，不把固定小样例的速度外推到任意数据规模。

示例验收：一个结果金额可由两笔相同金额交易解释，但客户/订单标识不同。应保留歧义并索取连接规则；不能只凭金额宣布找到来源。

### 定向任务的最短执行路径

用户要求首条/指定行时，先用真实表头确定物理行及请求字段集合；只追这些行及连接、聚合或触发条件所需的完整来源组，不扩展成整份结果核验。未给出结构时，先探查表名、维度和少量表头/样本，不读取整表来猜列名。

- 确认实际键角色、联合键、字段映射和计算依据后，合并本轮键与所需列，每个来源工作表按该查询扫描一次。依赖前一表取得键时分阶段；键已知的独立来源可并行，磁盘繁忙或内存紧张时减少并发。默认复用工具按内容哈希验证的查询缓存，仅测首次读取或排查缓存问题时用 `--no-cache`。
- 有材料支持且工具可表达的连接/计算，直接写 `reconcile.py` 计划；批量行共用计划和读取上下文。已提取的证据继续用于字段比较、组内校验及报告，不为每个字段另开扫描。仅对不支持的布局/算式补小型可审查适配，避免重写已有读取、连接与核算工具。
- 紧凑保存两部分：共享来源索引（文件哈希、表/行/单元格与原值），逐字段映射表（结果位置/值、来源引用、变换、核验状态及缺口）。同一源行不逐字段重复展开；派生字段另存必要输入、中间值和实际重算。完整证据留文件，模型只定向读取需要的部分。
- 交付前核对请求字段无漏项、引用可定位、唯一/歧义匹配数、查询覆盖与截断，以及任务所需计算；一次适当核验通过即交付。失败或发现新疑点时只补受影响部分。缺少历史公式、码表或适用规则就标未证实并列所需材料；数值重构不是无限搜索等价公式的理由。

执行日志记录各阶段实际墙钟耗时、扫描/命中行数、缓存状态和总耗时；并行子进程耗时之和不代表总耗时。先报告已定位来源及剩余阶段，报告完成后及时给文件链接，不因可选扩写或重复复核延后交付。以上是执行路径约束，不是任意规模下的速度保证。

### 大型 XLSX 与整份结果溯源

默认运行 `data_run.py trace --all-rows`（仅在用户要求整份结果时），自动检查工作表维度、前几行、真实表头和解压后的工作表大小。首行可能只是“附件”或标题，不能当作字段行；明确 `header_row`，保留原物理行号。新 `data_run.py trace` 与 `detective.py trace` 自动走定向读取。Python的 `DataDetective.trace` 与命令行使用同一管线，不再保留另一个全量索引默认入口。

`scripts/xlsx_query.py` 流式扫描一个工作表，仅保留精确筛选命中的记录。从请求的结果行收集稳定键的去重集合（整份结果任务才取全部结果行），写到查询 JSON；同一字段的列表是 OR，不同字段之间是 AND。每个源表按整批键查询一次，不要按结果行重复启动全表扫描。用 `--columns` 只投影本轮连接和核算所需字段；不要将大份逐格证据JSON整体读入模型上下文。明确字段角色，同名字段在不同表中可能指向不同对象，不能直接连接。连接顺序来自当前材料的键关系和规则，不固定源表数量、名称或层数。

```bash
# 只探查前几行和维度，不全扫工作表
python scripts/xlsx_query.py --file detail.xlsx --inspect --header-rows 5 --out schema.json
# keys.json 例：{"record_key": ["EXAMPLE-A", "EXAMPLE-B"]}；列名取自实际表头
python scripts/xlsx_query.py --file detail.xlsx --where-json keys.json --out matched-detail.json
# 标题占前两行时明确选第3行作为字段行
python scripts/xlsx_query.py --file rules.xlsx --header-row 3 --out rules.json
```

输出包含原文件哈希、工作表、物理行号、单元格地址、原 XML 数字、公式/缓存状态和日期格式信息。数值以精确文本保存，计算时用 Decimal；保留前导零标识。原始空格、空单元格、字面字符串 `"None"`、零不同，只有明确导出转换依据时才将其视为可对应的表现形式。

带精确筛选时，默认 `--reader auto` 使用快速行预筛并验证整份 XML；不支持的序列化布局回退为兼容读取。CLI 默认在当前工作目录的 `.dd_cache/xlsx_query/` 复用已完成查询，可用 `--cache-dir` 改路径或 `--no-cache` 禁用。每次复用前核对源文件的实际 SHA256；缓存同时绑定工作表、表头、筛选、投影、返回上限和读取版本，不能凭文件名或修改时间直接复用。Python API 通过 `cache_dir` 显式启用。

用 `cache.hit` 区分复用，`elapsed_seconds` 记录本次调用，`cold_scan_elapsed_seconds` 记录原始扫描。不要把缓存命中耗时当首次扫描速度；CLI 还需写输出文件，评测时应另计完整运行时间。来源文件变化或筛选变化会重新查询。

`--limit` 只限制返回记录，仍扫描整个工作表以报告命中和截断；检查 `coverage.scan_complete/filter_evaluation_complete/truncated`。`structure_validation_scope` 说明只对表头、筛选单元格和返回记录验证结构，不表示审查了未命中的所有字段。默认返回证据预算为500,000个投影单元格，超预算明确失败且不缓存半成品；通过更精确的键、字段投影或分批缩小范围，资源允许时可显式设 `--max-return-cells`。不要因换一种结构而退回无限制全表驻留内存。

每个结果行至少保存：唯一/歧义匹配数、明细和关联主表位置、字段映射及转换、实际执行的重算、规则原文位置和未决项。逐字段区分直接复制、改名、日期格式化、码值翻译、舍入和派生。代码转名称需要原系统码表；不可把观察到的对应关系冒称字典核验。

涉及重复收费、同日事件或组合条件时，用完整键组内的全部相关明细核对触发条件及对照项，不能只取最高分候选。金额分摊应从原始金额与比例输入重算；显示到两位的比例不一定是原计算使用的完整精度。候选公式即使逐行相符，也应与有原代码/日志支持的历史血缘区分。规则版本与费用/事件发生期间须另核对。

### 结构适配与通用边界

字段名示例不构成默认模式。先确定每行代表订单、交易、事件、版本还是汇总，再明确实际字段映射及唯一/复合键。宽表、长明细、多个工作表和多层关联均由显式计划声明；不能将一个行业的分组条件、字典或公式当作通用规则。

多组复合键必须按完整元组匹配。`where={"tenant":["A","B"],"item":["X","Y"]}` 是两个独立的 IN 条件，包含交叉组合；若实际只要 `(A,X)` 和 `(B,Y)`，应使用 `key_rows=[{"tenant":"A","item":"X"},{"tenant":"B","item":"Y"}]` 或 CLI `--keys-json`，再与 `where` 的额外条件取 AND。

矩形表可用显式 `header_row` 处理标题行；多级合并表头、交叉透视布局、嵌套JSON、动态SQL等不应伪装成普通单表。先按当前材料做可审查的结构化适配，保留原件、原位置、映射和覆盖；未完成适配时明确未核验。文字、DOCX、PDF和SQL的格式边界见[文件摄取](file-ingestion.md)。支持读取文件不等于已支持其中任意业务逻辑。

### 从候选到确定性重算

`data_run.py trace` 自动定位来源并逐字段对照，计算与历史执行状态独立；字段改名用配置 `field_aliases`，例如 `{"field_aliases":{"客户ID":["client_ref"]}}`。同值订单ID与客户ID不能靠相似度自动视为同一键。检查 `coverage` 的候选遗漏、键冲突排除数与检索预算；同文件备选行保留为 `candidate_role=alternative`。

有实际业务规则时，按 [lineage-plan.schema.json](../schemas/lineage-plan.schema.json) 写计划并运行 `python scripts/reconcile.py --plan plan.json --out reconciliation.json`。路径以计划文件目录为基准。下面示例只说明语法，实际字段/舍入/筛选必须来自当前材料：

```json
{
  "version": 1,
  "result": {"file": "result.csv", "row": 2},
  "sources": {"detail": {"file": "detail.sql", "table": "order_lines"}, "price": {"file": "prices.xlsx", "sheet": "Prices"}},
  "select": {"source": "detail", "where": [{"field": "order_id", "result_field": "order_id"}]},
  "joins": [{"source": "price", "left": {"source": "detail", "field": "sku"}, "right_field": "sku", "cardinality": "many_to_one"}],
  "outputs": [{"result_field": "total", "aggregate": "sum", "expression": {"mul": [{"source": "detail", "field": "qty"}, {"source": "price", "field": "unit_price"}]}, "quantize": "0.01", "rounding": "ROUND_HALF_UP", "tolerance": "0"}],
  "rule_evidence": [{"file": "rules.md", "quote": "这里填材料中的实际规则原文"}],
  "max_join_rows": 100000
}
```

支持精确筛选、显式基数连接、scalar/sum/count/min/max、十进制add/sub/mul/div和汇总后显式舍入。表达式是JSON白名单，无eval/SQL执行。重复唯一键、缺连接对象、空值数值或超预算会拒绝核验，不能静默丢行/重复计额。标识比较保留前导零。规则引文只作原文存在检查，Agent仍须核实其语义与适用版本。

复合连接用 `keys` 字段数组声明完整元组，与旧单键 `left/right_field` 语法二选一。例如：

```json
{"source":"rates","keys":[
  {"left":{"source":"lines","field":"tenant"},"right_field":"tenant"},
  {"left":{"source":"lines","field":"item"},"right_field":"sku"}
],"cardinality":"many_to_one"}
```

XLSX 的 result/source 引用可显式提供 `header_row`，或在 `query` 对象中提供 `where`、`key_rows`、`columns`、`limit`、`reader`、`max_return_cells`。这会直接查询原始文件并复用计划目录的 `.dd_cache/xlsx_query/`，将原始行和单元格证据接入重算；不需要导出成丢失原行号的中间CSV。配置不可覆盖 file/sheet/header/cache 的边界。`coverage.source_queries` 保留查询范围和截断/缓存状态；过滤作用域仍需按当前规则审查，不能将数值相等当作业务筛选完整或历史执行已证实。

整份结果沿用同一映射/计算计划时，使用批量入口 `python scripts/reconcile.py --plan plan.json --rows 2 3 4 --out batch.json`，Python 对应 `verify_plan_batch(plan, rows, base_dir)`。整批共享原件读取、哈希和相同来源查询上下文；计划内的来源筛选应包含本批所有需要的键。每个请求行均保留报告或明确错误，不把 `batch_processed` 当作核验通过；读取每行的 `equal/mismatch/incomplete` 及证据等级。

共享/数组等公式不能只看缓存值就声称已还原表达式。从属共享公式保留原 `t/si/ref` 和缺口；缓存值可供数值比对，但无缓存或表达式未解析必须在覆盖和结论中说明。

当前工具不支持任意Excel/SQL表达式、窗口函数、模糊连接、逐行舍入、时效区间/汇率自动选择。遇到这些情况写专门可审查脚本或使用已授权可信计算引擎，保留输入/单位/规则位置和中间值；不能偷换成工具现有算式。`reconciled_not_proven_lineage` 表示所给计划数值相符，缺规则为 `incomplete`，不等为 `mismatch`，不等于历史上执行过该计划。完成溯源分析至少保存：字段映射与键基数、实际规则出处、重算检查、竞争候选/反证、覆盖缺口和血缘结论的证据等级。

数值计算使用50位十进制上下文，除显式汇总舍入外，超过精度或无限循环小数会拒绝计算，不能静默截断。CLI退出码0表示已产出核验报告，可能是mismatch或incomplete，自动流程必须读取JSON的status与checks；无效/不可用计划退出2。

金额常量优先写为JSON字符串；CLI按Decimal读取JSON小数字面量以保留全部位数。直接调用Python API时不能把已损失精度的float当精确常量，须用字符串/Decimal；容差和舍入尺度使用字符串。

检查值的 `difference` 定义为“重算值−结果文件实记值”，正数表示实记值较低；它不是绝对差，也不是实记值减应计值。`difference_definition` 显式记录方向。

