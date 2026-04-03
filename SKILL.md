---
name: finreport-slim
description: 从上市保险公司年报（PDF）中提取主题相关页面并生成精简 Markdown 文档，供 LLM 分析财务数据使用。适用场景：(1) 将年报 PDF 转换为结构化 pages.json；(2) 按主题（投资收益、偿付能力、资产负债表、资产减值等）筛选相关页；(3) 生成 30-70k token 的精简文档供 LLM 直接提问。当用户需要分析年报财务数据、提取投资收益率、偿付能力指标、资产结构等信息时触发此 skill。
---

# finreport-slim

`<skill_dir>` = 本 SKILL.md 所在目录的绝对路径。所有脚本在 `<skill_dir>/scripts/`。

需要某个参数的完整说明时，加载 [references/commands.md](references/commands.md)。

---

## 流程

### 第一步：PDF 转换（检查 pages.json 是否已存在，存在则跳过）

```bash
python3 <skill_dir>/scripts/convert.py "<pdf_path>" "<output_root>"
```

### 第二步：选择主题

读取 `<skill_dir>/scripts/themes.py` 中的 `THEME_PRESETS`，列出所有主题名和 description 供用户选择。

若无合适主题，让用户提供关键词后提醒将其添加到 `scripts/themes.py`，格式参考 `scripts/themes.example.py`。

### 第三步：提取命中页 + 生成精简文档

```bash
python3 <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "<output_dir>/<stem>/<stem>.pages.json" \
  --output-dir "<output_dir>/<stem>" \
  --theme <theme-name>

python3 <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "<output_dir>/<stem>/<stem>.pages.json" \
  --theme-json "<output_dir>/<stem>/<stem>.<theme_slug>.json" \
  --output-dir "<output_dir>/<stem>"
```

输出：`<stem>.slim.<theme_slug>.md` ← 提供给 LLM

---

## 常用调参

| 问题 | 操作 |
|------|------|
| 漏掉重要附注页 | `--min-score 5`（第三步 extract-theme 加） |
| 命中页噪声太多 | `--min-score 15` 或 `20` |
| 口径说明离表格较远 | `--context 2`（第三步 slim 加，默认 1） |
| 只要数据页 | `--context 0` |
