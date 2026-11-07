# 多格式来源与覆盖核验

跨文件溯源前读取本文件。先检查 `ingestion`、`missing` 和 `coverage`；某个来源未能读取与检索不到目标是不同结果。解析失败会明确报告，部分读取会保留具体缺口。`complete` 仅指当前解析器没有报告遗漏，不证明内容真实或布局恢复正确。

| 格式 | 实际读取与地址 | 必须检查的边界 |
|---|---|---|
| CSV / TSV | UTF-8/BOM、UTF-16 BOM、严格GB18030回退；逗号/制表符/分号/竖线；物理起止行、记录号、原始记录 | 多行单元格行号不是第几条记录；空/重复表头及多出列报错；不自动猜业务表头 |
| JSON / JSONL / NDJSON | 对象或对象数组；JSONL逐行定位，物理行和字节跨度；逐字段JSON Pointer、数字原文与类型 | 拒绝重复键和NaN/Infinity。嵌套对象/数组保留JSON文本，不猜连接粒度；大JSONL用流式定向筛选，JSON整体数组须先按原位置转换或切分 |
| XLSX | 工作表、物理行、字段对应单元格地址；公式原文、缓存值、数字格式 | `data_run.py trace` 自动识别标题行后的真实表头，歧义时配置 `header_row`；大表自动按稳定键批量扫描。低层检查可用 `source_profile.py` / `xlsx_query.py --inspect`。不执行公式，缺缓存标partial；纯零格式保留显示前导零及底层值，不能恢复Excel此前已丢失的精度 |
| XLS | pandas/xlrd表值读取 | 旧格式不提供与XLSX等价的公式/格式证明；需xlrd，复杂表优先可信转换 |
| TXT / MD | 原文段落、物理起始行、字符偏移、编码；长段按8000字符切片、128字符重叠 | 重叠片段不是独立证据，保留段号/跨度去重；Markdown表格仍作为文本，计算前保留原文另行结构化 |
| DOCX | 正文段落和表格按主体顺序；表号、表行、列、OOXML路径 | 表头可能只是推断；不能编造页码。嵌套表、文本框、页眉页脚等缺口报partial；修订/脚注/绘图内容仍须专门检查 |
| DOC | 明确标unavailable | 用可信Word/LibreOffice转换DOCX并登记原件与转换件；本库不执行宏、不假称已读取 |
| 数字PDF | 页码、页内提取文本的段落/偏移 | PDF表格不自动恢复列语义；多栏/顺序/数字需回看页面。含图页或无可提取文字页会报告缺口 |
| 扫描PDF / PNG / JPG / TIFF / BMP / WEBP | 接入已授权OCR或视觉转录sidecar；原件SHA256、页号、区域框、图像尺寸、引擎与识别分 | 本库无OCR引擎。无转录明确unavailable；关键ID/金额须回看原图。接入测试不等于OCR准确率测试 |
| SQL | 静态解析CREATE列定义与INSERT VALUES字面量；表名、声明号、元组号、物理行、字符跨度 | 不连接数据库、不执行SQL。SELECT/CTE/存储过程保留原文；表达式/方言转义/尾部子句标缺口；输入声明不等于最终数据库状态 |

依赖见 `scripts/requirements.txt`。数字PDF需pdfplumber，DOCX需python-docx，XLSX需openpyxl；缺依赖不能降级成“没有证据”。不要因为扩展名受支持就省略覆盖检查。

## 扫描与图片接入

由可用且已授权的OCR/视觉工具先生成 `<原文件完整路径>.ocr.json`，保留原图。坐标是**转录所用渲染图像的像素**，左上角原点；不直接拿PDF点坐标冒充像素。示例：

```json
{
  "source_sha256": "原文件字节的64位SHA256十六进制",
  "engine": "实际使用的引擎或人工视觉转录名称/版本",
  "pages": [{
    "page_num": 1, "width": 1200, "height": 1600,
    "regions": [{"text": "订单ID=00017；金额=125.30", "bbox": [80, 100, 600, 180], "confidence": null}]
  }]
}
```

分数未知填null，不由模型编造分数。缺页仍未完成。哈希不匹配、越界区域或无效分数会拒绝摄取。sidecar增改删会使索引缓存失效。转录依然是派生声明；重复导出/OCR不构成独立印证。

## 定向补读与规模

- CSV/TSV/JSONL/NDJSON大文件用 `record_query.py` 或统一入口 `data_run.py query`，批量稳定键和完整复合键筛选，原物理行不重新编号。仅返回投影字段；缺字段不同于null。`--limit`只限制返回，仍读到尾部检查损坏记录和覆盖。JSONL固定UTF-8；CSV默认UTF-8，可显式`--encoding gb18030`或`utf-16`和`--delimiter`，不静默猜编码。
- 流式记录查询默认返回500,000单元格/64MiB，单JSONL记录8MiB（CSV按解码字符计），累计不同字段20,000。超预算明确失败；前后SHA256检测扫描期间来源变化。该路径没有持久化查询缓存，重复查询仍全扫。通用内存解析文本上限64MiB、JSON最多100,000记录/500,000字段；不要通过无限提高预算回到整表内存索引。
- 检查 `projection_complete` 与 `filter_evaluation_complete`；缺筛选/投影字段不能当无匹配。低层`record_query.py`退出0/3/2分别表示complete/partial/failed；消费统一0/2/1机器协议应使用`data_run.py`。

- 结构化追源检查 `coverage.source_queries`、`source_scan_complete`、`source_truncated` 和每行候选历史。默认每表最多3组有用键；上限可配置到8，达到预算不等于证明不存在。多候选、缺作用域或后到冲突必须保留未决状态。
- 原文线索检索检查 `coverage.retrieval_complete`、`truncated`、`query_terms_truncated` 与逐来源状态。默认返回最多30条相关证据；可以配置单元、字符与术语预算，命中超过上限时明确不完整。实体与桥接候选只覆盖实际返回原文；扩大数量不保证语义关系正确。
- `read_evidence.py` 只能分页**报告中已经保存的证据**；报告未收录的记录必须重新检索/读取原始来源。
- 全部来源内容哈希与OCR sidecar参与缓存版本；报告哈希便于复核字节版本。缓存是本地派生文件，不能拿来源材料提供的pickle替代本地生成的缓存。
