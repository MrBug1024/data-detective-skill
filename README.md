# Data Detective · 数据侦探

[![CI](https://github.com/MrBug1024/data-detective-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/MrBug1024/data-detective-skill/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

面向 Agent 的数据溯源、实体核查与刑侦分析 skill。把分散的表格、文档和现场描述整理成**可定位的证据、可比较的解释和可执行的核查计划**。

支持 Codex 等遵循 Agent Skills 格式的宿主；提供 Claude Code 的本地目录安装。技能名为 **`data-detective`**，版本 **3.2.3**，入口为 [SKILL.md](SKILL.md)。Python CLI 负责确定性检索与整理，Agent 继续完成规则核验和分析。

| 模式 | 适用任务 | 输出 |
|---|---|---|
| DATA 数据溯源 | 对账、字段来源、关联规则、异常计算 | 来源位置、候选链路、覆盖缺口、显式规则重算 |
| ENTITY 实体核查 | 人物/组织提及、同名消歧、关系核查 | 标识分组、来源属性、歧义与冲突 |
| CRIMINAL 刑侦分析 | 案情梳理、证据解释、时间线重建 | 证据账本、竞争假说、调查优先级 |

另含[面部特征与合成画像工作流](references/facial-composites.md)。本库编译可追溯的特征规格；实际生成图片需要宿主提供图像工具。

## 通过 GitHub 链接安装

### Codex：复制给 Agent

将下面这句话发送给支持 `skill-installer` 的 Codex：

```text
使用 $skill-installer 从 https://github.com/MrBug1024/data-detective-skill 安装技能：仓库内路径为 .，安装名称为 data-detective。
```

根目录就是 skill，必须将仓库根路径 `.` 和安装名 `data-detective` 一并指定。安装器的默认目录由当前 Codex 环境决定；可在下一轮输入 `$data-detective` 使用，未出现时重启宿主。参见 [Codex 官方 skills 文档](https://learn.chatgpt.com/docs/build-skills)。

`skill-installer` 只复制技能文件。运行 CLI 前，为实际使用的 Python 安装技能目录中的 `scripts/requirements.txt`；仅文字分析无需这些 Python 依赖。

### 本地安装：Windows / macOS / Linux

需要 Git 与 **Python 3.10+**。以下命令可在 PowerShell 或常见 Unix shell 中执行；`python` 不存在时使用本机的 `python3`。

```sh
git clone https://github.com/MrBug1024/data-detective-skill.git
cd data-detective-skill
python -m pip install -r scripts/requirements.txt
python scripts/install_skill.py --target codex
```

建议在自己的 Python 虚拟环境中安装依赖；运行 CLI 时使用同一环境的解释器。安装脚本仅复制技能文件，不调用 pip、不联网，也不修改宿主配置。

| 目标 | 命令 | 安装位置 |
|---|---|---|
| Codex | `python scripts/install_skill.py --target codex` | `~/.agents/skills/data-detective/` |
| Claude Code | `python scripts/install_skill.py --target claude` | `~/.claude/skills/data-detective/` |
| 自定义 skills 目录 | `python scripts/install_skill.py --dest /path/to/skills` | `/path/to/skills/data-detective/` |

Windows 上 `~` 对应当前用户目录。自定义路径有空格时加引号；`--dest` 指定承载技能的 **skills 目录**（例如 `~/.agents/skills`），脚本会添加 `data-detective` 子目录。已有同名目录时会报错退出，更新前先备份并移走原安装；卸载时移除该技能目录即可。

安装仅复制运行资源，开发测试、演示文件、运行结果、缓存及 `.env` 不会进入精简安装。

### 下载 ZIP 安装

正式版本的精简包见 [GitHub Releases](https://github.com/MrBug1024/data-detective-skill/releases)。下载 `data-detective.zip`，解压后将其中的 `data-detective/` 放到宿主的 skills 目录，再为执行 CLI 的 Python 安装包内 `scripts/requirements.txt`。

GitHub 的 “Source code” 是完整源码快照；Release 中的 `data-detective.zip` 才是精简技能包。尚无 Release 时使用上面的仓库链接或本地安装方式。

## 使用

安装后向 Codex 发送：

```text
$data-detective 核查这份结果表的第 2 行，结合我提供的源表和规则，列出字段来源、计算依据和无法确认的部分。
```

```text
$data-detective 梳理这些记录中“张三”的提及，区分同名人物，保留标识冲突，并给出需要补证的关系。
```

```text
$data-detective 根据我提供的现场描述和证词重建时间线，比较竞争解释，给出最优先的三项核查行动。
```

仅文字分析可以直接交给 Agent；运行本地 CLI 无需 API key。实际分析始终使用用户提供或已授权的材料。

### 独立 CLI

在源码根目录运行以下命令；安装后在自己的工作目录执行，并将 `scripts/` 替换为技能安装目录下的绝对路径。`--sources` 接收具体文件路径，不能依赖 shell 自动展开通配符。

大型 XLSX 请先按稳定键使用 `scripts/xlsx_query.py` 定向读取，再连接主表、核对字段和重算。`trace` 是内存候选检索工具，不能直接替代整批大表核验。含标题行的工作簿通过 `--header-row` 显式指定真实表头；详见[大表定向溯源](references/data-entity-workflows.md#大型-xlsx-与整份结果溯源)。

定向查询一次传入整批稳定键，并通过 `--columns` 限制返回字段。CLI 默认按源文件内容哈希复用 `.dd_cache/xlsx_query/` 中的查询结果；`--no-cache` 可禁用，文件内容变化会失效。读取支持快速路径与兼容回退，保留原行号、单元格和完整扫描统计。

```sh
# DATA：物理行号包含表头；多 sheet 时明确指定工作表
python scripts/detective.py trace --result result.xlsx --row 2 --sheet Sheet1 --sources source.xlsx rules.md --out report.json --no-html

# ENTITY：检索提及并保留身份歧义
python scripts/detective.py investigate --mode entity --target 张三 --sources records.csv note.md --out report.json --html wall.html

# CRIMINAL：生成证据包，随后由 Agent 完成研判
python scripts/detective.py investigate --mode criminal --target 核验现场线索 --sources scene.md statements.txt --out packet.json --html wall.html

# 按原模式、配置和来源加入新材料重新核查
python scripts/detective.py validate --report report.json --new-evidence supplement.csv --out revised.json --no-html

# 按明确规则重算，计划格式见 references/data-entity-workflows.md
python scripts/reconcile.py --plan lineage-plan.json --out reconciliation.json

# 编译面部特征规格；此命令不生成图片
python scripts/detective.py portrait --case observations.json --out portrait.json --prompt-out portrait-prompt.txt
```

可用源码内的合成样例快速验证：

```sh
python scripts/detective.py trace --result examples/R1.xlsx --row 2 --sources examples/A.xlsx examples/B.xlsx examples/C.xlsx examples/D.xlsx examples/rules.md --out demo-report.json --no-html
```

## 支持范围

支持 CSV/TSV/XLSX/TXT/MD、数字 PDF、DOCX 正文与表格，以及 SQL 静态字面量。扫描件和图片通过绑定原件哈希的 OCR/视觉转录 sidecar 接入；本库不自带 OCR 引擎。旧 DOC 需可信转换；旧 XLS 另装 `xlrd`。详情见[文件摄取与覆盖](references/file-ingestion.md)。

- 检索分用于排序候选，不能视为正确率或有罪概率。同名不自动合并为同人。
- `evidence_packet_only` 表示证据整理包，Agent 仍需完成竞争假说、证据解释与调查计划，并另存分析报告。
- `reconciled_not_proven_lineage` 仅证明指定计划重算相符；历史血缘仍需公式、代码或执行日志。
- 检查 `coverage`、`missing` 及截断提示；ENTITY 可使用 `--start` / `--limit` 分页，不能只读首批就声称完整排查。
- 面部规格与心理、地理画像只能生成待检验线索。低清增强生成的细节不能回填为原图证据。

模式细节见[数据与实体工作流](references/data-entity-workflows.md)、[刑侦工作流](references/criminal-workflow.md)及 [JSON schemas](schemas/)。旧 `--mode event` 保留为 `criminal` 的兼容别名。

## 开发与验证

```sh
python -m pip install -r requirements-dev.txt
python scripts/package_skill.py --check
python -m unittest discover -s tests -v
python scripts/package_skill.py --out dist/data-detective.zip
```

打包采用运行资源白名单，并生成 SHA-256 校验文件。CI 在 Windows 与 Linux 执行结构检查、回归测试和打包；测试只运行本地合成数据，不调用模型服务。

另有[合成案件盲测](test/README.md)、[画像专项评测](test/portrait/README.md)与[跨格式行为评测](test/data_entity/README.md)。只有显式 `--live` 的模型盲测会联网并使用配置的模型服务；这些评测不构成真实案件准确率承诺。评分夹具不放进精简包，独立 Agent 测试需另外隔离评分材料。

```text
SKILL.md                技能入口与调用范围
agents/openai.yaml      Codex 展示名称和默认提示
scripts/                CLI、安装器、打包器
references/             按需加载的专业工作流
schemas/                输入、输出与配置约定
examples/               可复现的合成样例（仅源码）
tests/、test/            回归测试与评测材料（仅源码）
.github/workflows/      CI 与 Release 自动化（仅源码）
```

## 发布版本

先更新 `SKILL.md` 的 `metadata.version` 与本页版本号，运行上述检查并提交。推送与版本一致的标签即可触发 Release：

```sh
git tag v3.2.3
git push origin main
git push origin v3.2.3
```

也可以在 GitHub Actions 的 Release 工作流中手动填写已存在标签对应的版本发布。工作流核对版本、在 Python 3.10 和 3.13 上执行验证和测试，然后上传精简 ZIP 与 SHA-256；已存在的 Release 不会被覆盖。固定版本安装时可将 GitHub 链接指定为 `https://github.com/MrBug1024/data-detective-skill/tree/v3.2.3`，仓库内路径仍为 `.`，安装名称仍为 `data-detective`。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。
