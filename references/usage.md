# finreport-slim 使用说明

从年报 PDF 提取主题相关页，生成 30-70k token 的精简文档供 LLM 分析。

---

## 前置条件

```bash
pip install requests
```

API Key 已写入 `<skill_dir>/config.env`（每 3 个月更新一次）。

---

## 三步走

### 第一步：PDF 转换（每个 PDF 只做一次）

```bash
python3 <skill_dir>/scripts/convert.py "/path/to/report.pdf" "/path/to/output"
```

产出：`/path/to/output/<stem>/<stem>.pages.json`

> 已有 `pages.json` 则跳过。

---

### 第二步：提取主题命中页

```bash
python3 <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "/path/to/output/<stem>/<stem>.pages.json" \
  --output-dir "/path/to/output/<stem>" \
  --theme investment-income
```

可用主题见 `scripts/themes.py`，当前预设：

| 主题名 | 内容 |
|--------|------|
| `investment-income` | 投资收益、公允价值变动 |
| `solvency` | 偿付能力充足率 |
| `balance-sheet` | 合并资产负债表及附注 |
| `asset-impairment` | 资产减值、信用减值 |

产出：`<stem>.investment_income.json`

---

### 第三步：生成 LLM 精简文档

```bash
python3 <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "/path/to/output/<stem>/<stem>.pages.json" \
  --theme-json "/path/to/output/<stem>/<stem>.investment_income.json" \
  --output-dir "/path/to/output/<stem>"
```

产出：`<stem>.slim.investment_income.md` ← 直接提供给 LLM

---

## 常用调参

| 问题 | 解决方法 |
|------|---------|
| 漏掉重要附注页 | `--min-score 5`（第二步加） |
| 命中页噪声太多 | `--min-score 15` 或 `20` |
| 口径说明距表格较远 | `--context 2`（第三步加，默认 1） |
| 想节省 token | `--context 0` |

---

## 添加新主题

编辑 `scripts/themes.py`，在 `THEME_PRESETS` 中加条目：

```python
"insurance-liability": {
    "description": "保险合同负债、准备金",
    "keywords": ["保险合同负债", "合同服务边际", "寿险责任准备金"],
},
```

格式参考 `scripts/themes.example.py`。

---

## 产出文件一览

```
output/<stem>/
├── <stem>.pages.json              ← 第一步产出，永久复用
├── <stem>.md                      ← 全文 Markdown（备查）
├── <stem>.investment_income.json  ← 第二步产出
└── <stem>.slim.investment_income.md  ← ★ 喂给 LLM
```
