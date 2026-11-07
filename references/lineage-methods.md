# 数据溯源方法与验证

## 来源、依赖和执行记录

[W3C PROV](https://www.w3.org/TR/prov-primer/)区分数据实体、处理活动和参与者。这里据此区分原件快照、重构关系和实际执行的验证步骤；没有原始作业日志时不捏造历史处理活动。

[OpenLineage字段血缘](https://openlineage.io/docs/spec/facets/dataset-facets/column_lineage_facet/)区分直接取值/变换/聚合与连接、筛选等间接依赖。报告保留这一区别，避免把决定某行是否入选的键误称为金额值来源。共同依赖共享引用，避免给每个输出字段复制整份来源。

[Provenance Semirings](https://courses.cs.washington.edu/courses/cse544/11wi/lectures/tannen-semirings.pdf)将关系运算的来源组合显式化。当前实现借鉴“保留参与记录及运算组合”的原则；不宣称实现完整半环代数，尤其不把差集、否定和任意SQL表达式纳入未经实现的范围。

## 检索、消歧与字段匹配

[Fellegi–Sunter方法的实践与限制](https://www.census.gov/library/working-papers/2000/adrm/rr2000-05.html)要求对匹配/非匹配的比较分布有依据。没有校准标注集时，不把任意权重总分解释为概率。自动接受、待决和拒绝必须可由键、范围、冲突和候选基数解释。

[ONS记录链接流程](https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/populationestimates/methodologies/linkagemethodsforcensus2021inenglandandwales)展示了多次blocking的实际用途。本技能合并每轮可用键，避免缺某个字段就永久丢掉该记录；每轮覆盖和候选集合均须记录，不能用一次未检出证明不存在。

[Schema Matching综述](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/tr-2001-17.pdf)区分名称、实例、结构和约束证据。因此字段改名建议来自多种线索，值向量一致只支持候选映射；低信息常量、空值及不同角色的同值列不能自行确定来源。

在数据粒度上分别考虑记录标识、对象标识、分组键、明细序号和租户范围。缺少必要范围、重复键或有竞争解释时保持待决；已传播的连接后来出现冲突，相关下游证据需要重新评估。

[W3C Web Annotation](https://www.w3.org/TR/annotation-model/)中的原文引用与位置选择器提供互补定位。文档标识保留原文、字符区间与类型；桥接保留每跳原件引用、作用域及肯定/否认/转述状态。同来源明确修订要保留旧陈述并撤销其自动传播资格，不能把新旧版本当独立支持。文本线索仍需到结构表执行实际键查询，禁止用待匹配表自己的信息补足缺键后自证。

## 规模与可复用访问

[SQLite查询规划](https://www.sqlite.org/queryplanner.html)中的B-tree索引用于减少重复全扫；[事务与提交](https://www.sqlite.org/atomiccommit.html)用于避免半成品变成有效缓存。已实现压缩原行XML快照、磁盘共享字符串与按需键索引；按候选数量选择较窄的键索引，同时保留异常筛选单元格的诊断，避免因加速而漏掉不可判定记录。新增索引列的首次构建成本与后续复用分开报告。

[DuckDB的Parquet读取](https://duckdb.org/docs/stable/data/parquet/overview.html)支持列式投影及过滤下推，但Excel公式、格式和单元格证据还需要独立保留。是否引入列式存储由实际读取成本、证据保真和依赖成本决定，不仅比较某次热查询速度。

原件内容SHA、解析语义、表头、查询参数和索引版本共同约束复用。完整性失败、输入变化、返回预算或不完整构建须明确报告。线程/进程并行只用于可独立验证的步骤，产物发布必须等待所有必需检查通过。

## 多场景验收

验证覆盖结构化与文档材料、电商/物流/财务/实验/制造等不同粒度，以及医院真实数据。每个场景保留原始输入、预期定位和判定依据，检查TP/FP/FN、未决项、字段覆盖、引用有效性及变化后的缓存失效。

关键反例包括同名不同ID、跨命名空间ID碰撞、重复复合键、缺范围、晚到冲突、空值/常量误当支持、互补缺键、改名列等值竞争、文档否认或转述、OCR不确定及损坏尾部。评测结果只描述实际覆盖的样本，不外推为任意材料的保证。

速度分别报告空应用缓存、索引构建、已有索引的新键/新列查询和重复查询；操作系统文件缓存是否清空须明确。比较前后完整证据内容与候选集合，不能用少读、截断或降低校验要求换取表面速度。
