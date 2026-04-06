# finreport-slim 命令参数参考

脚本路径：`<skill_dir>/scripts/`

---

## convert（PDF 转换）

```bash
python <skill_dir>/scripts/convert.py "<pdf_path>" "<output_root>" [options]
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `pdf` | 必填 | 源PDF路径（位置参数） |
| `output_dir` | PDF所在目录 | 输出根目录（位置参数） |
| `--language` | `ch` | 文档语言（`ch`/`en`） |
| `--timeout` | `600` | 轮询超时秒数 |
| `--page-ranges` | 无 | 指定页范围，如 `"1-50"` |
| `--no-formula` | 关闭 | 禁用公式解析 |
| `--no-table` | 关闭 | 禁用表格解析 |
| `--ocr` | 关闭 | 启用OCR（扫描版PDF） |

API Key 从 `config.env` 自动加载。

---

## toc-scan（目录解析）

```bash
python <skill_dir>/scripts/finreport_scope.py toc-scan \
  --pages-json "<stem>.pages.json" \
  --output-dir "<dir>"
```

| 参数 | 说明 |
|------|------|
| `--pages-json` | pages.json 路径 |
| `--output-dir` | 输出目录 |

输出：`<stem>.toc.json`

---

## section-slim（章节提取）

```bash
python <skill_dir>/scripts/finreport_scope.py section-slim \
  --pages-json "<stem>.pages.json" \
  --toc-json "<stem>.toc.json" \
  --output-dir "<dir>" \
  --section <section-name> \
  [--ref-slim <bs.md> <is.md>] \
  [--max-pages N]
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--pages-json` | 必填 | pages.json 路径 |
| `--toc-json` | 无 | toc.json 路径（省略则现场解析） |
| `--output-dir` | 必填 | 输出目录 |
| `--section` | 必填 | 章节名：`management-discussion`, `balance-sheet`, `income-statement`, `financial-notes` |
| `--ref-slim` | 无 | 附注筛选用：资产负债表和利润表的slim文件路径 |
| `--max-pages` | 无 | 页数上限 |

输出：`<stem>.slim.<section_slug>.md`

---

## extract-theme（主题关键词提取）

```bash
python <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "<stem>.pages.json" \
  --output-dir "<dir>" \
  --theme <theme-name>
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--pages-json` | 必填 | pages.json 路径 |
| `--output-dir` | 必填 | 输出目录 |
| `--theme` | 必填 | 主题名（见 themes.py） |
| `--scope` | `all` | `all`=全文；`notes`=只扫附注区间 |
| `--min-score` | `10` | 命中阈值 |
| `--max-hits` | `30` | 最多保留页数 |

输出：`<stem>.<theme_slug>.json`

---

## slim（主题精简文档）

```bash
python <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "<stem>.pages.json" \
  --theme-json "<stem>.<theme_slug>.json" \
  --output-dir "<dir>"
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--pages-json` | 必填 | pages.json 路径 |
| `--theme-json` | 必填 | extract-theme 输出的 JSON |
| `--output-dir` | 必填 | 输出目录 |
| `--context` | `1` | 命中页前后各保留几页 |

输出：`<stem>.slim.<theme_slug>.md`

---

## 章节配置（sections.py）

在 `SECTION_TYPES` 中定义，字段：

| 字段 | 说明 |
|------|------|
| `toc_patterns` | TOC匹配正则，按优先级排列 |
| `body_patterns` | 正文扫描正则（fallback用） |
| `stop_body_patterns` | 本节结束标志 |
| `subtoc_names` | 内嵌财务子目录中的名称 |
| `merge_consecutive` | 是否合并连续子条目（management-discussion用） |

## 主题配置（themes.py）

在 `THEME_PRESETS` 中定义，格式参考 `themes.example.py`：

```python
"theme-name": {
    "description": "主题说明",
    "keywords": ["关键词1", "关键词2"],
    # 可选：
    "statement_patterns": [...],
    "note_start_patterns": [...],
    "note_stop_patterns": [...],
}
```
