# finreport-slim 使用说明

这个 skill 的目标是把年报 PDF 先转成可复用的 `pages.json`，再按主题筛页，最后生成适合喂给 LLM 的精简 Markdown。

## 工作流

```text
PDF
  -> convert
  -> <stem>.pages.json
  -> extract-theme
  -> <stem>.<theme>.json
  -> slim
  -> <stem>.slim.<theme>.md
```

## 三步命令

### 1. convert

把 PDF 交给 MinerU 解析，并落地为页级文本。

```bash
python3 <skill_dir>/scripts/convert.py "/path/to/report.pdf" "/path/to/output"
```

输出目录示例：

```text
output/<stem>/
  <stem>.pages.json
  <stem>.md
  <stem>.candidates.json
```

说明：

- `<stem>.pages.json` 是后续步骤的核心输入。
- `<stem>.md` 是整份年报的页级 Markdown 备查版本。
- `<stem>.candidates.json` 是根据报表页 / 附注页规则做的初筛结果。
- 现在脚本会在转换阶段先做一轮特殊符号清洗，尽量把 MinerU 留下的 `$...$`、`^ +`、拆开的百分号等内容转回更可读的文本。

### 2. extract-theme

按主题关键词给页打分，找出命中页。

```bash
python3 <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "/path/to/output/<stem>/<stem>.pages.json" \
  --output-dir "/path/to/output/<stem>" \
  --theme investment-income
```

输出：

```text
<stem>.<theme_slug>.json
```

这个文件里会保存：

- `theme`: 主题名
- `theme_description`: 主题说明
- `scope`: 扫描范围，`all` 或 `notes`
- `note_window`: 如果只扫附注，这里会记录自动推断的附注页区间
- `hit_windows`: 连续命中页的窗口
- `hits`: 每个命中页的分数、命中关键词、摘要

### 3. slim

把命中页及上下文页拼成一个精简版 Markdown。

```bash
python3 <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "/path/to/output/<stem>/<stem>.pages.json" \
  --theme-json "/path/to/output/<stem>/<stem>.investment_income.json" \
  --output-dir "/path/to/output/<stem>"
```

输出：

```text
<stem>.slim.<theme_slug>.md
```

说明：

- `--context 1` 表示每个命中页前后各带 1 页上下文。
- `slim` 阶段也会再次做特殊符号清洗，所以即使你复用的是历史 `pages.json`，最终 `.slim.md` 也会比之前更干净。

## 主题配置怎么写

主题配置放在 `themes.py` 里的 `THEME_PRESETS`。

在这个 skill 仓库里，模板文件是：

```text
scripts/themes.example.py
```

实际使用时，部署目录里需要有真正的 `themes.py`，`finreport_scope.py` 会直接执行：

```python
from themes import THEME_PRESETS
```

也就是说：

- `themes.example.py` 是模板
- `themes.py` 是运行时实际读取的配置文件

### 最小可用格式

```python
"investment-income": {
    "description": "投资收益、公允价值变动及相关口径",
    "keywords": [
        "投资收益",
        "公允价值变动损益",
        "利息收入",
        "股息收入",
    ],
},
```

### 完整格式

```python
"balance-sheet": {
    "description": "合并资产负债表主表及附注映射",
    "statement_patterns": [
        r"合并资产负债表",
        r"Consolidated Balance Sheet",
    ],
    "note_start_patterns": [
        r"财务报表附注",
    ],
    "note_stop_patterns": [
        r"附录：财务报表补充资料",
    ],
    "keywords": [
        "资产负债表",
        "货币资金",
        "商誉",
        "递延所得税资产",
    ],
},
```

## `THEME_PRESETS` 字段解释

下面按“字段名 -> 作用 -> 在代码里怎么用”说明。

### `description`

作用：

- 给这个主题写一句人能看懂的说明。

在代码里的用途：

- `extract-theme` 输出 JSON 时会写入 `theme_description`。
- 主要用于让你或用户选择主题时快速判断。

建议：

- 写业务语言，不要写成代码注释式短语。
- 一句话说清“想抓什么内容”即可。

### `keywords`

作用：

- 这是主题筛页的核心字段。
- `extract-theme` 会逐页检查这些关键词是否出现，并据此打分。

在代码里的用途：

- `_score_page_for_keywords()` 用它做命中判断和分值计算。
- `_extract_excerpt()` 用命中的关键词截取摘要片段。
- `_detect_theme_hits()` 根据分数和 `--min-score` 判断是否收录该页。

怎么理解：

- 这是“这一页像不像我要的主题”的判断依据。
- 它不负责定位附注起止页，也不负责识别主表页。

建议：

- 优先选目标页高频出现、其他页低频出现的词。
- 一般 4 到 8 个比较合适。
- 太少容易漏页，太多容易引噪声。
- 尽量用年报原文里的标准表述，不要自己发明同义词。

### `statement_patterns`

作用：

- 用正则识别“主表页”。
- 主要给 `locate` 这条链路用，也就是“主表行 -> 附注段落”的映射逻辑。

在代码里的用途：

- `_auto_select_statement_pages()` 读取它。
- `_find_page_indices_by_patterns()` 用它在全文中找报表页。

什么时候需要：

- 你在做类似资产负债表、利润表、现金流量表这种“主表 + 附注”的映射任务时需要。
- 如果只是做 `extract-theme` 关键词筛页，通常不需要填。

建议：

- 用页面标题级别的稳定表述。
- 可以同时放中文和英文标题，增强兼容性。

### `note_start_patterns`

作用：

- 用正则识别附注区间从哪里开始。

在代码里的用途：

- `_infer_note_window()` 会先用它找附注起点。
- 当 `extract-theme --scope notes` 时，会先缩小到附注页范围再筛页。

什么时候需要：

- 主题主要分布在财务报表附注里，而且你希望只扫附注、排除正文噪声时。

建议：

- 选附注首页或附注大章节标题里稳定出现的短语。
- 优先用靠近页面顶部的标题，因为 `_infer_note_window()` 会做“页首附近匹配”。

### `note_stop_patterns`

作用：

- 用正则识别附注区间到哪里结束。

在代码里的用途：

- `_infer_note_window()` 找到起点后，会继续用它找终点。

什么时候需要：

- 年报附注后面常常跟着补充资料、附录、受托业务等章节，这时建议填。

建议：

- 选“附录”“补充资料”“受托业务”这类稳定标题。
- 宁可稍微保守，也不要写得太泛，否则可能提前截断附注。

## 字段之间的关系

可以这样理解：

- `keywords` 决定“哪一页值得保留”
- `statement_patterns` 决定“哪些页是主表页”
- `note_start_patterns` / `note_stop_patterns` 决定“附注页范围在哪里”
- `description` 只是解释这个主题是什么

也就是说，真正负责主题打分的是 `keywords`，其他几个字段都是辅助定位页区间或主表页。

## 增加新主题的建议

推荐流程：

1. 先在输出的 `<stem>.md` 或 `<stem>.pages.json` 里手工找几页你真正想要的页面。
2. 记录这些页里反复出现、但别的章节不常出现的词。
3. 先只写 `description + keywords`，跑一次 `extract-theme`。
4. 如果命中页太散，再考虑是否要加 `--scope notes`，以及是否补 `note_start_patterns` / `note_stop_patterns`。
5. 只有当你需要“主表行映射到附注”时，再补 `statement_patterns`。

## 调参建议

### `--min-score`

作用：

- 命中门槛。
- 越高越严格，页数越少；越低越宽松，覆盖越大。

经验：

- 漏重要附注时，先试 `5`
- 噪声页太多时，试 `15` 或 `20`

### `--context`

作用：

- 控制命中页前后附带多少页。

经验：

- 只要表格：`0`
- 表格加附近口径说明：`1`
- 会计政策说明和表格隔得较远：`2`

## 输出文件说明

```text
output/<stem>/
  <stem>.pages.json
  <stem>.md
  <stem>.candidates.json
  <stem>.<theme_slug>.json
  <stem>.slim.<theme_slug>.md
```

常用判断方式：

- 想看全文解析质量：查 `<stem>.md`
- 想看哪些页被主题命中：查 `<stem>.<theme_slug>.json`
- 想直接给 LLM：用 `<stem>.slim.<theme_slug>.md`

## 文本清洗规则

`slim` 阶段对每个页面做多层清洗，顺序如下：

### 1. HTML 表格转 Markdown

- 使用 HTMLParser 解析 `<table>` 标签，正确处理 `colspan` / `rowspan`
- 分组表（第一列有内容、其余列全空的行≥2行时）会被拆成 **分组标题 + 多个小表**
- 多个连续表格之间强制空行，避免粘连

### 2. 特殊符号清洗

针对 MinerU 常见残留：

- `$2 9 . 3 \%$` → `29.3%`
- `$90 \%$` → `90%`
- `$^ +$` / `$^{+}$` → `+`
- `$\cdot$` → `-`
- `$” 9 0 7 3 “$` → `”9073”`

如果后续遇到新的脏模式，优先去 `_clean_special_symbols()` 补规则。

### 3. 阅读体验优化

- 去掉 `%` 和数字后面接中文标点时的多余空格
- 收敛双空格列表前缀
- 清理括号内外多余空格
- 表格行不做处理，避免破坏列分隔符

如果后续发现”结构对但读着不顺”的情况，优先去 `_polish_readability()` 补规则。

### 4. 页头 / 页脚噪声过滤

自动检测并移除页面头尾的噪声行：

- **公司名**：从报告前 10 页自动提取（匹配 `XX股份有限公司` / `XX有限公司` 格式），同时生成全角/半角括号变体
- **报告年份**：通用正则匹配（`二零XX年年报`、`20XX年年度报告`）
- **页码**：1-4 位纯数字
- **栏目标题**：`财务报表`、`审计报告`、`关于我们`
- **OCR 短乱码**：紧邻已确认噪声行的短字符串（≤14字符、无标点、无长英文词）

过滤策略：先预检页面尾部 8 行内是否存在噪声，确认存在后才启用移除；页头同理。这样既能清除噪声，又不会误删正文。

如果后续新样本中出现未覆盖的固定页脚，优先去 `_is_footer_noise_line()` 补模式。如需添加新的通用页脚正则，修改 `_FOOTER_GENERIC_RE` 列表。

## 限制

- 主题词仍然需要人工维护。
- 特殊符号清洗是规则法，不保证覆盖所有公式残留。
- 不同公司年报标题口径会略有差异，新主题第一次通常都需要手调一轮关键词。
- 页脚公司名依赖前 10 页自动提取，如果年报前 10 页没有公司全称独立行，需手动在代码中补充。
