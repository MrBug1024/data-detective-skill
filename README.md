# Data Detective v3.2

面向 Agent 的数据溯源、实体核查与刑侦分析技能。入口为 [SKILL.md](SKILL.md)；按模式加载参考，输出带来源的证据、竞争解释与可执行的调查计划。

原“事件还原”已升级为**刑侦模式 CRIMINAL**，`--mode event` 保留为兼容别名。专业参考覆盖洛卡德、物证同一认定、LR/贝叶斯、IBE/ACH、行为与心理假说、MO/签名、地理画像、被害人学、日常活动和偏差控制；均附官方或原始研究链接。

新增[面部特征与合成画像工作流](references/facial-composites.md)：从独立目击原话/可见影像整理特征，保留未知、污染风险和冲突，生成按来源分开的局部绘图请求，并记录修订。实际画图由 Agent 调用可用图像工具完成并查看验收；本库不内置人脸生成模型。

DATA/ENTITY现支持跨格式可定位摄取、候选覆盖报告、保守身份分组及显式规则重算。支持范围和预处理要求见[文件摄取](references/file-ingestion.md)，计算计划见[数据与实体工作流](references/data-entity-workflows.md)。

## 运行

```powershell
python -m pip install -r scripts/requirements.txt

# DATA：结果物理行号含表头；多sheet结果用 --sheet 显式指定
python scripts/detective.py trace --result result.xlsx --row 2 --sheet Sheet1 --sources source.xlsx rules.md --out report.json --no-html

# ENTITY：检索提及，保留同名/不同标识的歧义
python scripts/detective.py investigate --mode entity --target 张三 --sources records.csv --out report.json --html wall.html

# DATA：依据实际规则编写计划，核验过滤/连接/聚合与十进制舍入
python scripts/reconcile.py --plan lineage-plan.json --out reconciliation.json

# ENTITY：按报告coverage.next_start继续取尚未返回的命中
python scripts/detective.py investigate --mode entity --target 张三 --sources records.csv --start 100 --limit 100 --out page2.json --no-html

# CRIMINAL：生成来源可追溯的案件包，随后由Agent按SKILL完成研判
python scripts/detective.py investigate --mode criminal --target 现场调查方向 --sources scene.md statements.txt --out packet.json --html wall.html

# 以原模式、原配置和原来源加新材料重新核查
python scripts/detective.py validate --report report.json --new-evidence supplement.csv --out revised.json --no-html

# Agent 按 portrait-input.schema.json 核对、提取原始描述后编译特征规格；不生成图片
python scripts/detective.py portrait --case observations.json --out portrait.json --prompt-out portrait-prompt.txt
```

`--sources` 使用明确路径。相对路径以当前目录为基准。若仅有文字描述，Agent 可以直接按刑侦参考分析，或存成 TXT/MD 后索引。

## 输出与边界

| 模式 | 脚本实际完成 | Agent仍需完成 |
|---|---|---|
| DATA | 索引、候选匹配、字段证据/备选/覆盖；reconcile按显式计划重算 | 规则语义/适用版本、原始计算执行血缘验证 |
| ENTITY | 目标提及、按标识声明分组、带来源属性、冲突与行内候选关系 | 跨页同名/别名消歧、有效期核查、真实关系验证 |
| CRIMINAL | 证据账本、文件哈希、精确复制分组、时间声明、覆盖情况 | 竞争假说、ACH矩阵、专业证据解释与调查排序 |

刑侦 CLI 不自动调用大模型，不会直接断定嫌疑人或补出心理画像。`evidence_packet_only` 表示尚待研判。Agent 的完成报告与原始包分开保存，格式见 [criminal-analysis.schema.json](schemas/criminal-analysis.schema.json)。检索分不等于概率；ENTITY/CRIMINAL 未评估的 `confidence` 为 `null`，展示“未评估”。

足迹可支持花纹/尺寸/磨损等比较，不能由单枚鞋印可靠确定年龄、性别、体重；身高估计需要适配且验证过的模型与个体预测区间。心理与地理画像是线索生成工具，不能代替身份或罪责认定。

面部规格中的 `specification_ready` 只表示可交付局部绘图要求，不代表重建真实脸；`render_state=not_generated` 保持实际状态。鞋印、作案手法、心理假说和祖源类别不能确定五官；低清增强生成的细节不能回填为原图证据。未知部位留白，证人冲突不平均，人物左右须核对。引文子串检查不验证语义或记忆准确性。

CSV/TSV/TXT/MD/SQL静态解析使用Python标准库；XLSX依赖openpyxl，保留公式/缓存/地址且不计算公式；DOCX可读正文和表格；数字PDF提取带页码文本，表格列语义须另核。扫描件/图片接入绑定原图SHA256的OCR/视觉转录sidecar；本库不内置OCR引擎，图片尺寸检查使用Pillow。旧DOC明确需要可信转换；XLS需pandas/xlrd。无rapidfuzz时有字符串匹配回退，分数不能跨版本视为相同。没有语义嵌入模型或自动多跳搜索；`max_hops` 为保留配置。

`reconcile.py` 支持精确等值筛选、显式连接基数、scalar/sum/count/min/max、十进制四则与汇总后舍入；拒绝重复唯一键、缺连接对象、任意代码及超预算连接。`reconciled_not_proven_lineage` 表示该计划重算相符，不能代替公式/代码/执行日志证明历史血缘。未知表达式或逐行舍入等规则需另写可审查计算，不能用近似规则替代。

已支持类型解析失败或依赖缺失时会报错终止，避免将“文件没读成功”写成“没有证据”。空文档、扫描页、缺失文件和未支持类型仍须查看 `missing` 及覆盖提示。

`ingestion` 按文件标complete/partial/unavailable；`coverage.ingestion_complete` 与候选搜索覆盖是不同维度。部分PDF、未核OCR、无缓存公式不会假称全部完成。来源与sidecar内容变化都会使索引缓存失效。

## 控制规模与性能

配置可通过 `--config config.json` 传入。`max_candidates` 为单次匹配全局候选上限；`criminal_max_units`/`criminal_max_chars` 默认100/1600；`entity_max_units` 默认100。`coverage` 和 `missing` 显式报告遗漏/截断，结论前须补读决定性材料。命中记录不重新解析；索引缓存按来源版本重用。不能将演示样例速度当作任意规模保证。

DATA默认仍有模糊检索/文档提及检索预算，详见 `coverage.retrieval_limits/search_complete`。ENTITY可指定 `entity_identity_fields`，分组不把共享电话当唯一身份；分页分组只覆盖当前页。`field_aliases` 显式映射改名字段，避免跨角色ID仅因同值相连。

## 测试

```powershell
python -m unittest discover -s tests -v
python test/run_blind_eval.py
python test/run_blind_eval.py --live --env-file tests/.env --out test/results/live.json
```

[盲测说明](test/README.md)：8个合成案件覆盖三种模式。被测模型只收到技能、相关参考与公开案情，隐藏规范仅本地评分；不向模型暴露文件系统。保存每案答卷、证据引用诊断、用时与token，机器检查后仍需独立语义复核。它是单轮文本评测，另有独立Agent前向验证；不把模拟通过数叫真实案件准确率。

另有[6个画像专项盲测案件](test/portrait/README.md)。`test/portrait/check_outputs.py` 可核对保存答卷与公开原文、实际编译结果的一致性；语义判断和实际成图必须另行审查，不能用提示词检查冒充画像效果测试。

本轮变更、113项回归、独立Agent测试与未验证范围见 [v3.1验证报告](outputs/portrait_validation/validation-report.md)。

v3.2的跨格式CLI行为评测可重复运行：`python test/data_entity/run_eval.py --out-dir outputs/data_entity_validation/forward_eval`。它使用合成输入及本地隔离评分，不调用模型，不代表真实业务准确率。最终回归、性能和独立Agent实操见 [v3.2验证报告](outputs/data_entity_validation/validation-report.md)。

主要目录：`scripts/` 可重复执行工具，`references/` 按需专业工作流，`schemas/` 输出/配置约定，`tests/` 自动回归，`test/` 公开案件/隔离评分/模型评测。已有用户输出位于 `outputs/`，不属于基准答案。
