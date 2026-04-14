# finreport-slim 使用说明

从A股上市保险公司定期报告（年报、中报、偿付能力报告）PDF中提取指定内容，生成适合LLM分析的精简Markdown。

## 报告类型判断

根据PDF文件名自动判断：

| 文件名包含 | 类型 | 提取方式 |
|-----------|------|---------|
| `年度报告` / `年报` | 年报 | 按章节提取 |
| `中期报告` / `半年度报告` / `中报` | 中报 | 按章节提取（同年报） |
| `偿付能力` | 偿付能力报告 | 按主题关键词（solvency） |

## 年报/中报工作流

```
PDF → convert → pages.json（永久复用）
                  ↓
             toc-scan → toc.json
                  ↓
             section-slim → .slim.management_discussion.md
                          → .slim.balance_sheet.md
                          → .slim.income_statement.md
                          → .slim.financial_notes.md（按科目筛选）
```

## 偿付能力报告工作流

```
PDF → convert → pages.json
                  ↓
             extract-theme --theme solvency → .solvency.json
                  ↓
             slim → .slim.solvency.md
```

## 按章节提取（年报/中报）

### 1. convert（PDF → pages.json）

把PDF交给MinerU云API解析，生成页级结构数据。每份年报只需转换一次。

```bash
python scripts/convert.py "/path/to/report.pdf" "/path/to/output"
```

输出在 `<output>/<stem>/` 下：
- `<stem>.pages.json` — 核心输入，后续步骤都用它
- `<stem>.md` — 整份年报Markdown备查

### 2. toc-scan（解析目录）

从年报前20页解析目录结构，计算页码偏移量。

```bash
python scripts/finreport_scope.py toc-scan \
  --pages-json "<stem>.pages.json" \
  --output-dir "<output_dir>"
```

输出：`<stem>.toc.json`

支持三种目录格式：
- HTML表格（中国平安）
- 正向文本 `标题 页码`（新华保险）
- 反向文本 `页码 标题`（中国太保）

### 3. section-slim（提取章节）

按顺序提取4个章节。**financial-notes 必须在 balance-sheet 和 income-statement 之后**，因为它用前两者的输出做科目筛选。

```bash
# 管理层经营分析
python scripts/finreport_scope.py section-slim \
  --pages-json "<stem>.pages.json" \
  --toc-json "<stem>.toc.json" \
  --output-dir "<output_dir>" \
  --section management-discussion

# 合并资产负债表
... --section balance-sheet

# 合并利润表
... --section income-statement

# 财务报表附注（按科目筛选）
... --section financial-notes \
  --ref-slim "<stem>.slim.balance_sheet.md" "<stem>.slim.income_statement.md"
```

### section-slim 的定位逻辑

提取分三层fallback：

1. **TOC匹配**：在toc.json中查找匹配的条目，取页码范围
2. **子目录匹配**：财务报告章节中的内嵌子目录（如人保page 119列出各报表页码）
3. **正文扫描**：用body_patterns在全文中找到章节起始页

对于management-discussion，还有额外的**验证步骤**：TOC匹配后检查目标页第一行是否确实包含该章节内容。验证失败则回退到正文扫描。这是因为H+A双重上市公司（人保、人寿）的前半部分和后半部分页码体系不同。

### 附注科目筛选逻辑（--ref-slim）

`--ref-slim` 从资产负债表和利润表的slim文件中解析附注引用编号：

1. 提取主节编号（"附注六"→六，"附注十一"→十一）和子项编号（表格中的1、2、3...）
2. 在附注全文中只保留匹配子项的页面
3. 新华保险等无主节编号的报告，用第一个资产负债表科目的编号作为起始阈值

## 按主题关键词筛选（辅助流程）

适用于非固定章节的自定义提取需求。

### 主题配置

在 `scripts/themes.py` 的 `THEME_PRESETS` 中定义：

```python
"solvency": {
    "description": "偿付能力充足率指标",
    "keywords": ["偿付能力", "核心偿付能力", "综合偿付能力", ...],
}
```

关键词建议4-8个，选目标页高频、其他页低频的词。

### 调参

| 参数 | 默认值 | 调整场景 |
|------|--------|---------|
| `--min-score` | 10 | 漏页→5，噪声多→15或20 |
| `--context` | 1 | 口径说明远→2，只要表格→0 |
| `--scope` | all | 只扫附注区间→notes |

## 输出文件说明

```
<output_dir>/<stem>/
  <stem>.pages.json          # 页级结构（永久复用）
  <stem>.md                  # 全文Markdown（备查）
  <stem>.toc.json            # 目录结构
  <stem>.slim.management_discussion.md  # ★ 管理层分析
  <stem>.slim.balance_sheet.md          # ★ 资产负债表
  <stem>.slim.income_statement.md       # ★ 利润表
  <stem>.slim.financial_notes.md        # ★ 附注（筛选后）
```

带 ★ 的文件直接提供给LLM。

## 文本清洗规则

slim阶段对每页做四层清洗：

1. **HTML表格→Markdown**：处理colspan/rowspan，分组表拆成小表，多层表头压平成组合列名
2. **特殊符号清洗**：修复MinerU残留（`$2 9 . 3 \%$` → `29.3%`）
3. **阅读优化**：清理括号内外多余空格
4. **页头/页脚噪声过滤**：自动检测公司名、年份、页码并移除

额外规则：
- 左上角空白表头在大多数财务表中补成 `项目`
- 空标签汇总行在值列明确时补成 `合计`
- 与前文完全重复的空标签合计行直接去重

遇到新的脏模式优先补 `_clean_special_symbols()`，页脚问题补 `_is_footer_noise_line()`。

## 已验证的公司

| 公司 | 管理层分析 | 资产负债表 | 利润表 | 附注 |
|------|-----------|-----------|--------|------|
| 新华保险 2025 | 27-54 (26p) | 119-120 (2p) | 121-122 (2p) | 60p |
| 中国平安 2025 | 39-72 (34p) | 186-188 (3p) | 189-190 (1p) | 62p |
| 中国人保 2025 | 19-49 (31p) | 126-128 (3p) | 129-131 (3p) | 58p |
| 中国太保 2025 | 33-62 (30p) | 157-158 (2p) | 159-160 (2p) | 39p |
| 中国人寿 2025 | 12-26 (15p) | 88-91 (4p) | 92-95 (4p) | 44p |

## 2026-04 校验说明

已对以下5家公司2025年年报做过回归验证：
- 中国平安
- 中国人保
- 中国太保
- 新华保险
- 中国人寿

覆盖命令：
- `toc-scan`
- `section-slim --section management-discussion`
- `section-slim --section balance-sheet`
- `section-slim --section income-statement`
- `section-slim --section financial-notes --ref-slim ...`

其中：
- 中国平安主要依赖 HTML 目录定位
- 中国人保、中国太保、新华保险主表/附注主要依赖正文 fallback 或财务子目录兜底
- 中国人寿的 TOC 只有“08 财务报告”，三大主表依赖正文 fallback
