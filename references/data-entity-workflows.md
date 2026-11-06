# DATA / ENTITY 工作流

## DATA：可重算的来源关系

1. 锁定结果文件/表/物理行号（含表头），记录文件版本、币种、单位、时区、数据有效时间。读取[格式与覆盖](file-ingestion.md)。Excel显示值、公式、缓存值不同；检查 `metadata.cells`，缺缓存不得当成空值原始事实。SQL同一行多元组要加元组号/偏移定位。
2. 先稳定键，再联合键。ID 保留前导零；跨表的同值不同字段不是主键关系。明确一对一/一对多、键空值和重复率；名称/金额/日期只能生成候选。模糊近似不能覆盖精确标识冲突。
3. 对每个结果字段写出候选源字段和变换（过滤、连接、聚合、舍入、汇率、单位、时效条件）。只允许有业务规则/代码/日志依据的变换；不能为了凑数事后发明公式。
4. 同时报出竞争候选，特别是相同金额/姓名的不同记录。正向重算结果，列明预期值、实得值、差额/容差及容差来源；数值相等仍不能独立证明真实血缘。
5. 已证实的计算关系与“检索到相似行”分开。报告未连接源、键冲突、规则缺失和只匹配部分字段。缺表不能被低分边补齐。`chain.confidence` 是启发式排序分而非血缘正确概率。

性能：一次索引后复用可寻址单元；精确候选先过滤再做昂贵匹配。真实场景测冷/热缓存耗时、行数和候选上限，不把固定小样例的速度外推到任意数据规模。

示例验收：一个结果金额可由两笔相同金额交易解释，但客户/订单标识不同。应保留歧义并索取连接规则；不能只凭金额宣布找到来源。

### 大型 XLSX 与整份结果溯源

先检查工作表维度、前几行、真实表头和解压后的工作表大小。首行可能只是“附件”或标题，不能当作字段行；明确 `header_row`，保留原物理行号。旧 `trace` 会构造全量行对象和索引，超过其解压 XML 预算时应改用定向读取，不能靠提高候选上限解决。

`scripts/xlsx_query.py` 流式扫描一个工作表，仅保留精确筛选命中的记录。先从整份结果收集稳定键的去重集合，写到查询 JSON；同一字段的列表是 OR，不同字段之间是 AND。每个源表按整批键查询一次，不要按结果行重复启动全表扫描。用 `--columns` 只投影本轮连接和核算所需字段；不要将大份逐格证据JSON整体读入模型上下文。明确字段角色，同名字段在不同表中可能指向不同对象，不能直接连接。连接顺序来自当前材料的键关系和规则，不固定源表数量、名称或层数。

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

`trace` 负责找候选；字段改名用配置 `field_aliases`，例如 `{"field_aliases":{"客户ID":["client_ref"]}}`。同值订单ID与客户ID不能靠相似度自动视为同一键。检查 `coverage` 的候选遗漏、键冲突排除数与检索预算；同文件备选行保留为 `candidate_role=alternative`。

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

当前工具不支持任意Excel/SQL表达式、窗口函数、模糊连接、逐行舍入、时效区间/汇率自动选择。遇到这些情况写专门可审查脚本或使用已授权可信计算引擎，保留输入/单位/规则位置和中间值；不能偷换成工具现有算式。`reconciled_not_proven_lineage` 表示所给计划数值相符，缺规则为 `incomplete`，不等为 `mismatch`，不等于历史上执行过该计划。完成DATA分析至少保存：字段映射与键基数、实际规则出处、重算检查、竞争候选/反证、覆盖缺口和血缘结论的证据等级。

数值计算使用50位十进制上下文，除显式汇总舍入外，超过精度或无限循环小数会拒绝计算，不能静默截断。CLI退出码0表示已产出核验报告，可能是mismatch或incomplete，自动流程必须读取JSON的status与checks；无效/不可用计划退出2。

金额常量优先写为JSON字符串；CLI按Decimal读取JSON小数字面量以保留全部位数。直接调用Python API时不能把已损失精度的float当精确常量，须用字符串/Decimal；容差和舍入尺度使用字符串。

检查值的 `difference` 定义为“重算值−结果文件实记值”，正数表示实记值较低；它不是绝对差，也不是实记值减应计值。`difference_definition` 显式记录方向。

## ENTITY：先消歧，再建关系

1. 定义实体类型和调查时间范围；将名称、别名、稳定标识及其来源/有效期分开。
2. 名称精确出现只是 mention，不是 identity。两条同名记录有不同稳定标识时分成候选实体；没有稳定标识时保留未消歧状态。相同共享电话/地址也可能是家庭、代理、办公地址。
3. 属性保留逐条来源声明，禁止后读值覆盖前值。区分真实冲突、历时变化、不同实体、记录错误；不要把变化本身称为异常。
4. 关系应是有方向、有效时间、角色和原文依据的三元组。共现只支持“同记录出现”；任职不等于所有权、收款方不等于实际受益人。区分申报关系和已验证关系。
5. 负面指控引用原文并保留归属、时间和待核实状态；不能从关系网邻接推断参与犯罪。未检出不是不存在。

输出：候选实体分组、稳定标识表、属性声明、可证关系及未决冲突。先列决定身份/关系的最小补证动作，避免扩展成无边界背景搜索。

脚本的 `candidate_entities` 按返回页中的身份标识声明分组，保留所有属性及evidence_id；无身份标识的记录独立保留，不因电话/地址相同合并。可用 `entity_identity_fields` 明确指定字段命名空间；默认不把订单ID、产品ID当人/组织ID。多种标识组合不做传递闭包自动合并，跨页/历史别名由Agent核查。

数组长度不是人数：`identity_grouping.identifier_group_count` 为标识声明分组数，`unresolved_record_count` 是另存的无标识记录数；这些均未认定身份，脚本的confirmed_identity_count保持0。同一订单的多条明细在候选阶段可能分数相近，需要规则重算决定是否共同参与聚合，不能把“近分候选”直接称为来源互斥或数据错误。

用 `--start/--limit` 与 `coverage.next_start` 继续读取；字符截断见 `truncated_evidence_ids`，定向回读原文。关系提取只是行内声明/共现；否认、转述、时间限定和角色需逐条回看，不能从包含“持股”等词就建立肯定所有权。文本姓名子串只支持提及；结构化名称列中“张三丰”不是“张三”的同名记录。完成ENTITY分析需逐候选身份说明支持/矛盾标识、属性时效与关系原文；证据不足时保持未消歧，不强行合并。

## 调查行动的更新方向（两模式共用）

竞争命题应关于实际来源/身份/关系；“目前信息不足”只是评估状态。新取得原始文件只让下一步核实成为可能，不自动支持任一候选。版本不一致降低现有比对依据，版本一致只解决版本问题；二者本身都不能决定哪笔记录为来源。每个预期结果重新检查其是否与对应命题相容、是否能区分其他候选；不能区分就标中性/未知。不要为了让每项行动有支持/削弱标签而制造方向。
