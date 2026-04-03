# finreport-slim 使用说明

从年报 PDF 提取主题相关页，生成 30-70k token 的精简文档供 LLM 分析财务数据。

---

## 为什么这样做

年报 300+ 页，全文喂给 LLM 成本高（~350k token）且效果差。曾考虑过两条路：

- **正则精确提取**：规则脆弱，换公司或换主题就要重写，放弃。
- **粗删法**（按数字密度过滤页）：即使过滤后仍剩 250k+ token，且与主题无关，放弃。

**最终方案的核心洞察**：你今天关心什么，决定了你需要哪些页。与其试图用一份固定文档通吃所有问题，不如每次按主题动态选页。

```
PDF → pages.json（一次性）
          ↓
    extract-theme（关键词计分，纯本地，~0.1秒）
          ↓
       slim（命中页 + 上下文）→ ~30-70k token
          ↓
         LLM（带具体提问）
```

换主题只换关键词，pages.json 永久复用，不重新调用 API。

---

## 关键词计分机制

完全本地 Python，不调用 LLM，速度极快（370 页约 0.1 秒）。

逻辑三句话：

1. 每个主题预设一组关键词
2. 逐页统计关键词出现次数之和，达到 `--min-score` 阈值的页标记为"命中"
3. 命中页前后各取 `--context` 页一起输出，带入表格前的标题和表格后的口径说明

```
page 306  ← 上下文（context=1）
page 307  ← 上下文（context=1）
page 308  ← 命中页 ★  "46. 投资收益"
page 309  ← 上下文（context=1）
page 310  ← 上下文（context=1）
```

**为什么要上下文**：财务数据的可用性依赖口径——净投资收益率 vs 总投资收益率 vs 综合投资收益率，定义不同，混淆会得出错误结论。附注里的会计政策说明（"以公允价值计量且其变动计入其他综合收益"等）通常紧邻数据表格，`--context 1` 自然带入，无需额外处理。

---

## 前置条件

```bash
pip install requests
```

API Key 已写入 `<skill_dir>/config.env`（每 3 个月更新一次，直接替换文件中的值）。

---

## 三步走

### 第一步：PDF 转换（每个 PDF 只做一次）

```bash
python3 <skill_dir>/scripts/convert.py "/path/to/report.pdf" "/path/to/output"
```

产出：`/path/to/output/<stem>/<stem>.pages.json`

> 已有 `pages.json` 则跳过此步。

---

### 第二步：提取主题命中页

```bash
python3 <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "/path/to/output/<stem>/<stem>.pages.json" \
  --output-dir "/path/to/output/<stem>" \
  --theme investment-income
```

可用主题见 `scripts/themes.py`：

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

HTML 表格在输出时自动转换为标准 Markdown 表格，LLM 读取准确率更高，token 消耗也更低。

---

## 阈值调优（--min-score）

`--min-score` 是"一页里主题关键词出现次数之和"的最低门槛，直接决定命中页多少。

以平安 2025 年报（370 页）+ `investment-income` 主题为例：

| 阈值 | 命中页 | 含上下文 | 估算 token | 特点 |
|------|--------|---------|-----------|------|
| 20 | 20页 | 53页 | ~37k | 只抓高密度数据页，口径说明覆盖不全 |
| **10（默认）** | **33页** | **79页** | **~56k** | **数据页 + 会计政策口径，推荐** |
| 5 | 33页 | 79页 | ~56k | 与 10 完全相同（该年报 5-9 分区间为空档） |

从 20 → 10 新增的关键内容：page 201-209（金融工具分类和计量会计政策）、page 227-234（收入确认原则），即 FVTPL/FVOCI/摊余成本的定义——这是新准则下口径判断的核心依据。

**调整建议**：

| 情况 | 操作 |
|------|------|
| 漏掉重要附注页 | `--min-score 5` |
| 命中页噪声太多 | `--min-score 15` 或 `20` |
| 口径说明离表格较远 | `--context 2`（第三步加，默认 1） |
| 只要数据页，token 最少 | `--context 0` |

---

## 添加新主题

编辑 `scripts/themes.py`，在 `THEME_PRESETS` 中加条目：

```python
"insurance-liability": {
    "description": "保险合同负债、准备金",
    "keywords": ["保险合同负债", "合同服务边际", "寿险责任准备金"],
},
```

**选关键词的原则**：选在目标数据页高频出现、在其他页低频出现的词，4-8 个为宜。太少容易漏页，太多容易引入噪声。

格式参考 `scripts/themes.example.py`。

---

## 产出文件一览

```
output/<stem>/
├── <stem>.pages.json                  ← 第一步产出，永久复用
├── <stem>.md                          ← 全文 Markdown（备查）
├── <stem>.investment_income.json      ← 第二步产出（命中页列表）
└── <stem>.slim.investment_income.md   ← ★ 喂给 LLM
```

输出文件格式示意（`slim.md` 内部）：

```markdown
<!-- page: 307 -->
财务报表附注 八、合并财务报表主要项目注释（续）
...

---

<!-- page: 308 [hit] -->
46. 投资收益
| 项目 | 2025年度 | 2024年度 |
|------|---------|---------|
| 净投资收益 | 152,863 | 83,613 |
...

---

<!-- page: 309 -->
...
```

`[hit]` 标记命中页，方便人工核查哪些页是被关键词选中的。

---

## 局限与注意事项

- **主题关键词人工维护**：新主题需手动加关键词，LLM 能自行过滤少量噪声页
- **口径说明依赖上下文窗口**：`--context 1` 覆盖大多数情况，会计政策集中在报告前部时调大到 `2`
- **不同公司格式差异**：关键词通常稳定，极少数情况需微调
