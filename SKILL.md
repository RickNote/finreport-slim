---
name: finreport-slim
description: 从上市保险公司年报（PDF）中提取主题相关页面并生成精简 Markdown 文档，供 LLM 分析财务数据使用。适用场景：(1) 将年报 PDF 转换为结构化 pages.json；(2) 按主题（投资收益、偿付能力、资产负债表、资产减值等）筛选相关页；(3) 生成 30-70k token 的精简文档供 LLM 直接提问。当用户需要分析年报财务数据、提取投资收益率、偿付能力指标、资产结构等信息时触发此 skill。
---

# finreport-slim

从年报 PDF 到 LLM-ready 精简文档的完整流程：PDF 转换 → 主题页筛选 → 精简文档生成。

核心脚本：`finreport_scope.py`（项目路径：`/Users/rick/Library/CloudStorage/OneDrive-个人/ProgramLibrary/finreport_scope/`）

---

## 流程概览

```
PDF → pages.json（一次性）→ extract-theme（按主题打分）→ slim（精简文档）→ LLM
```

---

## 第一步：PDF 转换

**检查**：`<output_dir>/<pdf_stem>/<pdf_stem>.pages.json` 是否已存在。若存在则跳过此步骤。

若不存在，运行转换脚本：

```bash
python3 scripts/convert.py "<pdf_path>" "<output_root>"
```

- 脚本位置：`/Users/rick/Library/CloudStorage/OneDrive-个人/skills/finreport-slim/scripts/convert.py`
- API Key 自动从 `config.env` 加载，无需手动设置
- 输出目录：`<output_root>/<pdf_stem>/`，包含 `<stem>.pages.json`、`<stem>.md`、`<stem>.candidates.json`

高级选项参见 references/commands.md 的 `convert` 参数部分。

---

## 第二步：选择主题

读取 `themes.py` 并展示预设主题：

**文件路径**：`/Users/rick/Library/CloudStorage/OneDrive-个人/ProgramLibrary/finreport_scope/themes.py`

读取 `themes.py` 中的 `THEME_PRESETS` 字典，列出所有可用主题名称和 description，让用户选择。

询问用户选择哪个主题。

**如果没有合适的预设主题**：让用户提供几个关键词（如"保险合同负债、合同服务边际、未到期责任准备金"），用 `--custom-keywords` 临时测试。提醒用户若效果理想，可将关键词添加到 `themes.py` 的 `THEME_PRESETS` 字典中永久保存。

---

## 第三步：提取 + 生成精简文档

### 3a. 提取主题命中页

```bash
cd /Users/rick/Library/CloudStorage/OneDrive-个人/ProgramLibrary/finreport_scope

python3 finreport_scope.py extract-theme \
  --pages-json "<output_dir>/<stem>/<stem>.pages.json" \
  --output-dir "<output_dir>/<stem>" \
  --theme <theme-name> \
  --scope all
```

输出：`<stem>.<theme_slug>.json`（主题命中结果）

### 3b. 生成精简文档

```bash
python3 finreport_scope.py slim \
  --pages-json "<output_dir>/<stem>/<stem>.pages.json" \
  --theme-json "<output_dir>/<stem>/<stem>.<theme_slug>.json" \
  --output-dir "<output_dir>/<stem>" \
  --context 1
```

输出：`<stem>.slim.<theme_slug>.md`（★ 喂给 LLM 的文件）

文件命名示例：`平安2025年报.slim.investment_income.md`

---

## 参数速查

详细参数说明见 [references/commands.md](references/commands.md)。

常用调整：
- 漏掉重要附注页 → `--min-score 5`
- 命中页噪声太多 → `--min-score 15` 或 `20`
- 口径说明离数据页较远 → `--context 2`
- 只要数据页，token 最少 → `--context 0`

---

## 第四步：把输出文件提供给 LLM

将 `<stem>.slim.<theme_slug>.md` 内容提供给 LLM，配合具体问题：

```
以下是[公司名][年份]年报中与[主题]相关的页面内容：

[粘贴 slim.md 内容]

请提取：...
```
