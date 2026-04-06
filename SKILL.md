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

`<pdf_dir>` = PDF 文件所在目录，`<stem>` = PDF 文件名去掉扩展名。
output_root 固定用 PDF 所在目录，输出文件夹 `<pdf_dir>/<stem>/` 与 PDF 同级，名字即报告名。

```bash
python3 <skill_dir>/scripts/convert.py "<pdf_path>" "<pdf_dir>"
```

### 第二步：选择主题

读取 `<skill_dir>/scripts/themes.py` 中的 `THEME_PRESETS`，以编号列表形式展示所有主题：

```
请选择要提取的主题（可输入编号，多选用逗号或空格隔开）：
1. investment-assets — 投资资产情况及资产配置结构
2. insurance-liability — 保险合同负债、承保财务损益
3. solvency — 偿付能力充足率指标
4. investment-income — 净投资/总投资/综合收益及其细项
```

等待用户回复后再执行，不得在用户未选择时自动跑全部主题。

若无合适主题，让用户提供关键词后提醒将其添加到 `scripts/themes.py`，格式参考 `scripts/themes.example.py`。

### 第三步：提取命中页 + 生成精简文档

```bash
python3 <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "<pdf_dir>/<stem>/<stem>.pages.json" \
  --output-dir "<pdf_dir>/<stem>" \
  --theme <theme-name>

python3 <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "<pdf_dir>/<stem>/<stem>.pages.json" \
  --theme-json "<pdf_dir>/<stem>/<stem>.<theme_slug>.json" \
  --output-dir "<pdf_dir>/<stem>"
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
