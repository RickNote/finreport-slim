---
name: finreport-slim
description: 从A股上市保险公司定期报告（年报、中报、偿付能力报告）PDF中提取指定内容并生成精简Markdown，供LLM分析财务数据。支持两种模式：(1) 按章节提取（管理层经营分析、合并资产负债表、合并利润表、财务报表附注，适用于年报和中报）；(2) 按主题关键词筛选（偿付能力等自定义主题，适用于所有报告类型）。当用户需要分析保险公司年报/中报/偿付能力报告、提取财务报表数据、对比多家公司经营指标时触发。
---

# finreport-slim

`<skill_dir>` = 本 SKILL.md 所在目录的绝对路径。所有脚本在 `<skill_dir>/scripts/`。

参数完整说明见 [references/commands.md](references/commands.md)。
公司特异性问题与踩坑记录见 [references/dev-notes.md](references/dev-notes.md)。

---

## 判断报告类型

根据 PDF 文件名判断，走不同提取路径：

| 文件名包含 | 报告类型 | 提取路径 |
|-----------|---------|---------|
| `年度报告` 或 `年报` | 年报 | 章节提取（下方流程A） |
| `中期报告` 或 `半年度报告` 或 `中报` | 中报 | 章节提取（同流程A，结构与年报基本一致） |
| `偿付能力` | 偿付能力报告 | 主题关键词筛选（下方流程B，theme=solvency） |

无法从文件名判断时，询问用户。

---

## 流程A：按章节提取（年报 / 中报）

配置在 `scripts/sections.py` 的 `SECTION_TYPES`。

### 第一步：PDF 转换（pages.json 存在则跳过）

`<pdf_dir>` = PDF 所在目录，`<stem>` = PDF 文件名去掉 `.pdf`。

```bash
python <skill_dir>/scripts/convert.py "<pdf_path>" "<pdf_dir>"
```

### 第二步：解析目录

```bash
python <skill_dir>/scripts/finreport_scope.py toc-scan \
  --pages-json "<pdf_dir>/<stem>/<stem>.pages.json" \
  --output-dir "<pdf_dir>/<stem>"
```

### 第三步：提取章节

按顺序提取，因为 financial-notes 依赖前两个的输出。

```bash
# 管理层经营分析
python <skill_dir>/scripts/finreport_scope.py section-slim \
  --pages-json "<pdf_dir>/<stem>/<stem>.pages.json" \
  --toc-json "<pdf_dir>/<stem>/<stem>.toc.json" \
  --output-dir "<pdf_dir>/<stem>" \
  --section management-discussion

# 合并资产负债表
python <skill_dir>/scripts/finreport_scope.py section-slim \
  ... --section balance-sheet

# 合并利润表
python <skill_dir>/scripts/finreport_scope.py section-slim \
  ... --section income-statement

# 财务报表附注（按科目筛选，需要前两个的输出）
python <skill_dir>/scripts/finreport_scope.py section-slim \
  ... --section financial-notes \
  --ref-slim "<pdf_dir>/<stem>/<stem>.slim.balance_sheet.md" \
              "<pdf_dir>/<stem>/<stem>.slim.income_statement.md"
```

输出：`<stem>.slim.<section_slug>.md` ← 提供给 LLM

### 章节列表

| section | 说明 |
|---------|------|
| `management-discussion` | 管理层经营分析/讨论与分析/业绩综述 |
| `balance-sheet` | 合并资产负债表 |
| `income-statement` | 合并利润表 |
| `financial-notes` | 财务报表附注（配合 `--ref-slim` 只提取引用的子项） |

---

## 流程B：按主题关键词筛选（偿付能力报告 / 自定义主题）

配置在 `scripts/themes.py` 的 `THEME_PRESETS`。

### 第一步：PDF 转换（同流程A）

### 第二步：选择主题

偿付能力报告直接使用 `solvency` 主题。
其他报告读取 `THEME_PRESETS` 列出所有主题让用户选择。
等待用户回复后再执行。若无合适主题，参考 `scripts/themes.example.py` 格式添加。

### 第三步：提取 + 生成精简文档

```bash
python <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "<pdf_dir>/<stem>/<stem>.pages.json" \
  --output-dir "<pdf_dir>/<stem>" \
  --theme <theme-name>

python <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "<pdf_dir>/<stem>/<stem>.pages.json" \
  --theme-json "<pdf_dir>/<stem>/<stem>.<theme_slug>.json" \
  --output-dir "<pdf_dir>/<stem>"
```

---

## 常用调参

| 问题 | 操作 |
|------|------|
| 附注提取太多 | 加 `--ref-slim`（流程A）或 `--min-score 15`（流程B） |
| 附注提取不全 | `--min-score 5`（流程B） |
| 口径说明离表格较远 | `--context 2`（流程B slim，默认 1） |
| 章节范围过大 | `--max-pages 50`（流程A） |
