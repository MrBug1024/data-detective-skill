# DATA / ENTITY 跨格式行为评测

用真实合成文件调用 `scripts/detective.py`，检查来源地址、标识精度、歧义保留、截断可见性和缓存更新。它是确定性工具评测，不能代表 Agent 推理水平或真实任务准确率。

```powershell
python test/data_entity/run_eval.py --out-dir outputs/data_entity_eval
python test/data_entity/run_eval.py --case F07_sql --out-dir outputs/sql_recheck
```

若当前 Python 缺少 `openpyxl`、`python-docx`、`reportlab` 或 `Pillow`，相应造数明确记为 `unavailable`，不能计为通过。CLI 还需技能正常依赖。评测不读取 `.env`、不联网、不执行 SQL。

生成目录内 `public/` 只有原始材料及调查请求，`raw/` 保留每次 CLI JSON、标准输出、错误及评分失败详情，`evaluation.json` 保存逐项判断和用时。逐案完成即写检查点；一项失败不阻断后续项。预期位于本地评分器的 `private/expected.json`，不拼入公开请求。

若用于独立 Agent 前向验证，只把生成的 `public/` 与技能所需工具/参考复制到独立工作区，不给该 Agent 本评测目录、`raw/` 或评分结果。单独的目录名不能构成文件系统隔离。本 runner 的评分函数知道预期，不应把它自身执行称为盲测。

图片包含人工可复现转录的 `.ocr.json`、原图 SHA256 及原图坐标框。这验证转录入库/追溯和失效检查，不验证 OCR 引擎识别率。SQL 覆盖静态 CREATE/INSERT 字面量，不代表任意 SQL 血缘分析。DOCX 表格来源保留表格与行，不能虚构 Word 页码。

重算前向样例使用独立的 SQL 账单明细、XLSX 价格表、CSV 结果和 Markdown 规则。真实运行 `scripts/reconcile.py`，核验聚合后舍入、数量统计及逐行来源，同时覆盖错额和重复价格键；计划和原始输入一并保存。算术相等仍标为未证实历史血缘，规则引文的语义适用性需要另行审查。
