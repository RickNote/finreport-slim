# finreport-slim 命令参数参考

脚本路径：`<skill_dir>/scripts/`（`<skill_dir>` 为本 skill 根目录）

---

## convert 命令（PDF 转换）

```bash
python3 <skill_dir>/scripts/convert.py "<pdf_path>" "<output_root>" [options]
```

| 参数 | 默认值 | 说明 | 何时修改 |
|------|--------|------|---------|
| `pdf` | 必填 | 源 PDF 文件路径（位置参数） | 每次必填 |
| `output_dir` | PDF 所在目录 | 输出根目录（位置参数） | 每次指定 |
| `--language` | `ch` | 文档语言（`ch` / `en`） | 处理英文年报时改为 `en` |
| `--model` | `vlm` | MinerU 模型版本 | 基本不改 |
| `--timeout` | `600` | 轮询超时秒数 | 网络慢或超大 PDF 时调大 |
| `--poll-interval` | `5` | 轮询间隔秒数 | 基本不改 |
| `--page-ranges` | 无 | 只处理指定页范围，如 `"1-50"` | 调试或节省配额时使用 |
| `--no-formula` | 关闭（即默认启用） | 禁用公式解析 | 不含公式的报告可启用此 flag |
| `--no-table` | 关闭（即默认启用） | 禁用表格解析 | 通常保持默认 |
| `--ocr` | 关闭 | 启用 OCR | 扫描版 PDF 才需要 |
| `--mineru-env` | `MINERU_API_KEY` | API Key 的环境变量名 | 有多个 Key 时可换变量名 |
| `--base-url` | `https://mineru.net` | MinerU API 地址 | 基本不改 |

**API Key**：自动从 `config.env` 加载，无需手动设置。每 3 个月更新一次，直接编辑 `config.env` 文件中的 `MINERU_API_KEY` 值。

**输出文件**（在 `<output_root>/<pdf_stem>/` 下）：

| 文件 | 说明 |
|------|------|
| `<stem>.pages.json` | PDF 全文，页级结构（永久复用） |
| `<stem>.md` | PDF 全文 Markdown（备查） |
| `<stem>.candidates.json` | 报表页/附注页候选（备查） |

---

## extract-theme 命令（主题命中页提取）

```bash
python3 <skill_dir>/scripts/finreport_scope.py extract-theme \
  --pages-json "<stem>.pages.json" \
  --output-dir "<output_dir>" \
  --theme <theme-name>
```

| 参数 | 默认值 | 说明 | 何时修改 |
|------|--------|------|---------|
| `--pages-json` | 必填 | pages.json 路径 | 每次必填 |
| `--output-dir` | 必填 | 输出目录 | 每次必填 |
| `--theme` | 必填 | 主题名称（见 themes.py） | 每次必填 |
| `--scope` | `all` | `all`=全文扫描；`notes`=只扫财报附注区间 | 关注附注时用 `notes` 可减少噪声 |
| `--min-score` | `10` | 命中阈值（关键词出现次数之和） | 漏页时调低（如 `5`）；噪声多时调高（如 `15`） |
| `--max-hits` | `30` | 最多保留的命中页数 | 基本不改 |

**`--min-score` 调优参考**（平安 2025 年报，investment-income 主题）：

| 阈值 | 命中页 | 含上下文 | 估算 token | 特点 |
|------|--------|---------|-----------|------|
| 20 | 20页 | 53页 | ~37k | 高密度数据页，口径说明覆盖不全 |
| **10（默认）** | **33页** | **79页** | **~56k** | **数据页 + 会计政策口径，推荐** |
| 5 | 33页 | 79页 | ~56k | 与 10 相同（该年报 5-9 分区间为空档） |

输出：`<stem>.<theme_slug>.json`

---

## slim 命令（生成 LLM 精简文档）

```bash
python3 <skill_dir>/scripts/finreport_scope.py slim \
  --pages-json "<stem>.pages.json" \
  --theme-json "<stem>.<theme_slug>.json" \
  --output-dir "<output_dir>"
```

| 参数 | 默认值 | 说明 | 何时修改 |
|------|--------|------|---------|
| `--pages-json` | 必填 | pages.json 路径 | 每次必填 |
| `--theme-json` | 必填 | extract-theme 生成的 .json 路径 | 每次必填 |
| `--output-dir` | 必填 | 输出目录 | 每次必填 |
| `--context` | `1` | 命中页前后各保留几页 | 口径说明离数据页较远时调大到 `2`；token 紧张时调为 `0` |

输出：`<stem>.slim.<theme_slug>.md`（★ 直接提供给 LLM）

---

## 主题管理（themes.py）

**文件路径**：`<skill_dir>/scripts/themes.py`，与 `finreport_scope.py` 同目录。

**格式参考**：见 `scripts/themes.example.py`

新增主题只需在 `THEME_PRESETS` 字典中添加一个条目：

```python
"your-theme": {
    "description": "主题说明",
    "keywords": ["关键词1", "关键词2", "关键词3"],
},
```

主题名在 `--theme` 参数中使用，输出文件名中连字符自动转为下划线（如 `your-theme` → `<stem>.your_theme.json`）。
