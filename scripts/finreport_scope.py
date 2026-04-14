#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import tempfile
import zipfile
from collections import defaultdict
from html.parser import HTMLParser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from themes import THEME_PRESETS
from sections import SECTION_TYPES


DEFAULT_STATEMENT_PATTERNS = [
    r"合并资产负债表",
    r"Consolidated Balance Sheet",
    r"Balance Sheet",
]
DEFAULT_NOTES_PATTERNS = [
    r"财务报表附注",
    r"Notes to the Financial Statements",
]


@dataclass
class PageRecord:
    page_idx: int
    text: str
    markdown: str
    matched_tags: list[str]



@dataclass
class PageWindow:
    start_page_idx: int
    end_page_idx: int
    page_indices: list[int]


@dataclass
class ThemeHit:
    page_idx: int
    score: int
    matched_keywords: list[str]
    excerpt: str


@dataclass
class StatementNoteRef:
    order: int
    item_name: str
    note_key: str
    note_no: int
    note_subref: str | None
    current_amount: str | None
    prior_amount: str | None


@dataclass
class NoteBlock:
    block_id: int
    page_idx: int
    text: str


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _collapse_spaced_number(text: str) -> str:
    collapsed = re.sub(r"(?<=\d)\s+(?=[\d.%])", "", text)
    return re.sub(r"(?<=\.)\s+(?=\d)", "", collapsed)


def _collapse_spaced_ascii_token(text: str) -> str:
    return re.sub(
        r"(?<![A-Za-z])(?:[A-Za-z]\s+){1,5}[A-Za-z](?![A-Za-z])",
        lambda match: re.sub(r"\s+", "", match.group(0)),
        text,
    )


def _normalize_formula_fragment(fragment: str) -> str:
    normalized = fragment.strip()
    normalized = normalized.replace(r"\%", "%")
    normalized = normalized.replace(r"\cdot", "·")
    normalized = normalized.replace(r"\times", "x")
    normalized = re.sub(r"\\\^\s*\{\s*\+\s*\}", "+", normalized)
    normalized = re.sub(r"\\\^\s*\+", "+", normalized)
    normalized = re.sub(r"\^\s*\{\s*\+\s*\}", "+", normalized)
    normalized = re.sub(r"\^\s*\+", "+", normalized)
    normalized = re.sub(r"\\(?:mathsf|mathrm|text|operatorname)\s*", "", normalized)
    normalized = normalized.replace("{", " ").replace("}", " ")
    normalized = normalized.replace("\\", "")
    normalized = _collapse_spaced_number(normalized)
    normalized = _collapse_spaced_ascii_token(normalized)
    normalized = re.sub(r"\s+([%.,;:])", r"\1", normalized)
    normalized = re.sub(r"([(\"“])\s+", r"\1", normalized)
    normalized = re.sub(r"\s+([)\"”])", r"\1", normalized)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    return normalized.strip()


def _expand_compact_date(match: re.Match[str]) -> str:
    year = match.group(1)
    month = int(match.group(2))
    day = int(match.group(3))
    return f"{year}年{month}月{day}日"


def _clean_residual_ocr_noise(text: str) -> str:
    cleaned = str(text or "").replace("\uffa0", " ")

    normalized_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line

        for _ in range(3):
            prev_line = line

            # Duplicate year fragments are a common OCR residue, e.g.
            # "新华20252025年是".
            line = re.sub(r"(?<!\d)(20\d{2})\1年", r"\1年", line)

            # Normalize compact dates both in standalone form and when directly
            # followed by an amount or page-number residue.
            line = re.sub(
                r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?=\d)",
                lambda m: _expand_compact_date(m) + " ",
                line,
            )
            line = re.sub(
                r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
                _expand_compact_date,
                line,
            )

            # Split compact value sequences that MinerU occasionally glues
            # together.
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?=20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))",
                r"\1 / ",
                line,
            )
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?=人民币)",
                r"\1 / ",
                line,
            )
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+)(?=\d{1,3}(?:,\d{3})+)",
                r"\1 / ",
                line,
            )
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+)(?=\d{1,3}(?:,\d{3})+\.\d)",
                r"\1 / ",
                line,
            )
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+\.\d+)(?=\d+(?:\.\d+)?%)",
                r"\1 / ",
                line,
            )
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+)(?=(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)?%))",
                r"\1 / ",
                line,
            )
            line = re.sub(
                r"(?<!\d)(\d{1,2})\.(\d{3}\.\d{2})(?!\d)",
                r"\1,\2",
                line,
            )

            # Restore separators in glued date/value chains and rate ranges.
            line = re.sub(r"(?<=日)(?=\d)", " ", line)
            line = re.sub(
                r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?=20\d{2}年)",
                r"\1 / ",
                line,
            )
            line = re.sub(r"(?<=%)(?=\d+-\d+年)", " / ", line)

            if line == prev_line:
                break

        # If a large amount is immediately followed by a short page-number-like
        # suffix at line end, keep the amount and drop the dangling tail.
        line = re.sub(r"(?<!\d)(\d{1,3}(?:,\d{3})+)(\d{2,3})$", r"\1", line)

        line = re.sub(r"\s{2,}", " ", line)
        normalized_lines.append(line)

    return "\n".join(normalized_lines)


def _clean_special_symbols(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\$\s*\\cdot\s*\$", "- ", cleaned)
    cleaned = re.sub(
        r"\$(.+?)\$",
        lambda match: _normalize_formula_fragment(match.group(1)),
        cleaned,
        flags=re.DOTALL,
    )
    cleaned = _collapse_spaced_number(cleaned)
    cleaned = _clean_residual_ocr_noise(cleaned)
    cleaned = re.sub(r"\s+([%.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"\(\s+", "(", cleaned)
    cleaned = re.sub(r"\s+\)", ")", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _polish_readability(text: str) -> str:
    polished_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if line.startswith("|") and line.endswith("|"):
            polished_lines.append(line)
            continue
        line = re.sub(r"\+\s*/\s*-\s*", "+/- ", line)
        line = re.sub(r"(?<=[\u4e00-\u9fff])\s*/\s*(?=[\u4e00-\u9fff])", "/", line)
        line = re.sub(
            r"(\d+(?:\.\d+)?%)\s+(?=\d+(?:\.\d+)?%)",
            r"\1、",
            line,
        )
        line = re.sub(
            r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*/\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?%)",
            r"\1，\2，\3",
            line,
        )
        line = re.sub(
            r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*/\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)",
            r"\1，\2",
            line,
        )
        line = re.sub(
            r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?%)",
            r"\1，\2",
            line,
        )
        line = re.sub(
            r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*/\s*(20\d{2}年)",
            r"\1，\2",
            line,
        )
        line = re.sub(
            r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*/\s*人民币",
            r"\1，人民币",
            line,
        )
        line = re.sub(r"(?<=\d)%\s+([，。；：])", r"%\1", line)
        line = re.sub(r"(?<=\d)\s+([，。；：])", r"\1", line)
        line = re.sub(r"([（(])\s+", r"\1", line)
        line = re.sub(r"\s+([）)])", r"\1", line)
        line = re.sub(r"(?<=\S)\s{2,}", " ", line)
        if line.startswith("-  "):
            line = "- " + line[3:]
        polished_lines.append(line)
    polished = "\n".join(polished_lines)
    polished = re.sub(r"\n{3,}", "\n\n", polished)
    return polished.strip()


# ---------------------------------------------------------------------------
# Footer / header noise filtering
# ---------------------------------------------------------------------------

# Generic patterns that match across any annual report
_FOOTER_GENERIC_RE: list[re.Pattern[str]] = [
    re.compile(r"^\d{1,4}$"),                                  # page number
    re.compile(r"^二零[二三四五六七八九零〇]+年年报$"),          # 二零二五年年报
    re.compile(r"^20\d{2}年年度报告$"),                         # 2025年年度报告
    re.compile(r"^关于我们$"),
    re.compile(r"^财务报表$"),
    re.compile(r"^审计报告$"),
]


def _build_footer_noise_set(pages: list[dict[str, Any]]) -> set[str]:
    """Auto-detect report-specific footer noise strings from the first pages.

    Scans the first ~10 pages for standalone company-name lines
    (ending with 股份有限公司 or 有限公司) and adds both bracket variants.
    """
    noise: set[str] = set()
    for page in pages[:10]:
        for line in str(page.get("text", "")).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if 4 <= len(stripped) <= 40 and re.fullmatch(
                r".+(?:股份有限公司|有限公司)", stripped
            ):
                noise.add(stripped)
                noise.add(stripped.replace("（", "(").replace("）", ")"))
                noise.add(stripped.replace("(", "（").replace(")", "）"))
    return noise


def _is_footer_noise_line(
    line: str, extra_noise: set[str] | None = None
) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    # Generic patterns
    for pat in _FOOTER_GENERIC_RE:
        if pat.fullmatch(stripped):
            return True
    # Report-specific noise (auto-detected company name etc.)
    if extra_noise and stripped in extra_noise:
        return True
    return False


def _looks_like_short_footer_garble(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("|"):
        return False
    if len(stripped) > 14:
        return False
    if re.search(r"[，。；：？！,.%()（）]", stripped):
        return False
    if re.search(r"[A-Za-z]{3,}", stripped):
        return False
    return True


_FOOTER_SCAN_DEPTH = 8


def _strip_footer_noise(
    text: str, extra_noise: set[str] | None = None
) -> str:
    lines = str(text or "").splitlines()
    if not lines:
        return ""

    tail_start = max(0, len(lines) - _FOOTER_SCAN_DEPTH)

    # Pre-check: is there any real footer noise (non-blank) in the tail?
    has_footer = any(
        lines[j].strip()
        and _is_footer_noise_line(lines[j], extra_noise)
        for j in range(tail_start, len(lines))
    )
    if not has_footer:
        return text

    # Scan from bottom; since we know there IS footer noise in the tail,
    # treat both noise lines and short garble lines as removable.
    i = len(lines) - 1
    while i >= tail_start:
        line = lines[i].rstrip()
        if _is_footer_noise_line(line, extra_noise):
            i -= 1
            continue
        if _looks_like_short_footer_garble(line):
            i -= 1
            continue
        break

    return "\n".join(lines[: i + 1]).strip()


def _strip_header_noise(
    text: str, extra_noise: set[str] | None = None
) -> str:
    """Remove noise lines from the top of a page (mirrored footer logic)."""
    lines = str(text or "").splitlines()
    if not lines:
        return ""

    head_end = min(len(lines), _FOOTER_SCAN_DEPTH)

    has_noise = any(
        lines[j].strip()
        and _is_footer_noise_line(lines[j], extra_noise)
        for j in range(head_end)
    )
    if not has_noise:
        return text

    i = 0
    while i < head_end:
        line = lines[i].rstrip()
        if _is_footer_noise_line(line, extra_noise):
            i += 1
            continue
        if _looks_like_short_footer_garble(line):
            i += 1
            continue
        break

    return "\n".join(lines[i:]).strip()


def _extract_text_from_item(item: dict[str, Any]) -> str:
    for key in ("text", "table_body", "code_body"):
        value = item.get(key)
        if value:
            return _polish_readability(_clean_special_symbols(str(value).strip()))
    list_items = item.get("list_items") or []
    if list_items:
        return _polish_readability(
            _clean_special_symbols("\n".join(str(x) for x in list_items).strip())
        )
    return ""


def _render_markdown_line(item: dict[str, Any], text: str) -> str:
    item_type = str(item.get("type") or item.get("category") or "").lower()
    if "title" in item_type or "heading" in item_type:
      return f"## {text}"
    if "table" in item_type:
      return text
    if "list" in item_type:
      return text
    return text


def _compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def _match_tags(text: str, compiled: list[re.Pattern[str]], prefix: str) -> list[str]:
    tags: list[str] = []
    for pattern in compiled:
        if pattern.search(text):
            tags.append(f"{prefix}:{pattern.pattern}")
    return tags


def _group_content_by_page(
    content_list: list[dict[str, Any]],
    statement_patterns: list[str],
    notes_patterns: list[str],
) -> list[PageRecord]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in content_list:
        grouped[int(item.get("page_idx", 0))].append(item)

    statement_re = _compile_patterns(statement_patterns)
    notes_re = _compile_patterns(notes_patterns)

    pages: list[PageRecord] = []
    for page_idx in sorted(grouped):
        texts: list[str] = []
        markdown_lines: list[str] = []
        for item in grouped[page_idx]:
            text = _extract_text_from_item(item)
            if not text:
                continue
            texts.append(text)
            markdown_lines.append(_render_markdown_line(item, text))

        page_text = "\n".join(texts).strip()
        page_markdown = "\n\n".join(markdown_lines).strip()
        matched_tags = (
            _match_tags(page_text, statement_re, "statement")
            + _match_tags(page_text, notes_re, "notes")
        )
        pages.append(
            PageRecord(
                page_idx=page_idx,
                text=page_text,
                markdown=page_markdown,
                matched_tags=matched_tags,
            )
        )
    return pages


def _save_conversion_artifacts(
    output_dir: Path,
    pdf_path: Path,
    pages: list[PageRecord],
    artifact_prefix: str,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    pages_json = output_dir / f"{artifact_prefix}.pages.json"
    markdown_path = output_dir / f"{artifact_prefix}.md"
    candidates_path = output_dir / f"{artifact_prefix}.candidates.json"

    pages_payload = {
        "source_pdf": str(pdf_path),
        "num_pages": len(pages),
        "pages": [asdict(page) for page in pages],
    }
    pages_json.write_text(
        json.dumps(pages_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    markdown_chunks: list[str] = []
    candidates = {"statement_pages": [], "notes_pages": []}
    for page in pages:
        markdown_chunks.append(f"<!-- page: {page.page_idx} -->\n{page.markdown}\n")
        if any(tag.startswith("statement:") for tag in page.matched_tags):
            candidates["statement_pages"].append(page.page_idx)
        if any(tag.startswith("notes:") for tag in page.matched_tags):
            candidates["notes_pages"].append(page.page_idx)

    markdown_path.write_text("\n\n".join(markdown_chunks), encoding="utf-8")
    candidates_path.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "pages_json": str(pages_json),
        "markdown": str(markdown_path),
        "candidates_json": str(candidates_path),
    }


def _mineru_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _poll_mineru_batch_result(
    *,
    api_key: str,
    base_url: str,
    batch_id: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    url = f"{base_url.rstrip('/')}/api/v4/extract-results/batch/{batch_id}"

    while time.time() < deadline:
        response = requests.get(url, headers=_mineru_headers(api_key), timeout=60)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"MinerU batch polling failed: code={payload.get('code')} msg={payload.get('msg')}"
            )

        result_list = payload.get("data", {}).get("extract_result", [])
        if not result_list:
            time.sleep(interval_seconds)
            continue

        result = result_list[0]
        state = result.get("state")
        if state == "done":
            return result
        if state == "failed":
            raise RuntimeError(
                f"MinerU parsing failed: {result.get('err_msg') or payload.get('msg') or 'unknown error'}"
            )
        time.sleep(interval_seconds)

    raise TimeoutError(f"Timed out waiting for MinerU batch result: {batch_id}")


def _download_and_extract_zip_to_temp(zip_url: str) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="mineru_zip_"))
    zip_path = temp_dir / "mineru_result.zip"
    with requests.get(zip_url, timeout=300, stream=True) as response:
        response.raise_for_status()
        with zip_path.open("wb") as file_obj:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_obj.write(chunk)

    extract_dir = temp_dir / "unzipped"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def _find_single_file(root: Path, pattern: str) -> Path | None:
    matches = list(root.rglob(pattern))
    if not matches:
        return None
    matches.sort()
    return matches[0]


def _load_pages_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _derive_artifact_prefix(path: Path) -> str:
    name = path.name
    for suffix in [
        ".pages.json",
        ".candidates.json",
        ".md",
        ".json",
    ]:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem




def _clean_note_text(text: str) -> str:
    cleaned = _clean_special_symbols(text)
    cleaned = re.sub(
        r"\n?财务报表\s*\n?二零二五年年报\s*\n?中国平安保险（集团）股份有限公司\s*\n?",
        "\n",
        cleaned,
    )
    cleaned = re.sub(
        r"\n?二零二五年年报\s*\n?中国平安保险（集团）股份有限公司\s*\n?",
        "\n",
        cleaned,
    )
    cleaned = re.sub(r"(?m)^\d{1,3}\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _get_theme_config(theme: str) -> dict[str, Any]:
    if theme not in THEME_PRESETS:
        raise RuntimeError(
            f"Unsupported theme: {theme}. Available themes: {', '.join(sorted(THEME_PRESETS))}"
        )
    return THEME_PRESETS[theme]


def _find_page_indices_by_patterns(
    pages: list[dict[str, Any]],
    patterns: list[str],
    *,
    start_page_idx: int | None = None,
    end_page_idx: int | None = None,
    skip_toc_pages: bool = False,
) -> list[int]:
    if not patterns:
        return []
    compiled = _compile_patterns(patterns)
    matches: list[int] = []
    for page in pages:
        page_idx = int(page["page_idx"])
        if start_page_idx is not None and page_idx < start_page_idx:
            continue
        if end_page_idx is not None and page_idx > end_page_idx:
            continue
        text = str(page.get("text") or "")
        if skip_toc_pages and page_idx <= 20 and "目录" in text:
            continue
        if any(pattern.search(text) for pattern in compiled):
            matches.append(page_idx)
    return matches


def _find_first_page_index_by_patterns(
    pages: list[dict[str, Any]],
    patterns: list[str],
    *,
    start_page_idx: int | None = None,
    end_page_idx: int | None = None,
) -> int | None:
    matches = _find_page_indices_by_patterns(
        pages,
        patterns,
        start_page_idx=start_page_idx,
        end_page_idx=end_page_idx,
        skip_toc_pages=True,
    )
    return matches[0] if matches else None


def _parse_html_table_grid(table_html: str) -> list[list[dict[str, Any]]]:
    class TableParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.rows: list[list[dict[str, Any]]] = []
            self._in_row = False
            self._in_cell = False
            self._current_row: list[dict[str, Any]] = []
            self._current_cell: dict[str, Any] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attrs_map = dict(attrs)
            if tag == "tr":
                self._in_row = True
                self._current_row = []
                return
            if tag in {"td", "th"} and self._in_row:
                self._in_cell = True
                self._current_cell = {
                    "text": "",
                    "rowspan": max(1, int(attrs_map.get("rowspan") or "1")),
                    "colspan": max(1, int(attrs_map.get("colspan") or "1")),
                    "is_header": tag == "th",
                }
                return
            if self._in_cell and tag == "br" and self._current_cell is not None:
                self._current_cell["text"] += "\n"

        def handle_endtag(self, tag: str) -> None:
            if tag in {"td", "th"} and self._in_cell and self._current_cell is not None:
                self._current_cell["text"] = _clean_special_symbols(
                    html.unescape(self._current_cell["text"]).strip()
                )
                self._current_row.append(self._current_cell)
                self._current_cell = None
                self._in_cell = False
                return
            if tag == "tr" and self._in_row:
                if self._current_row:
                    self.rows.append(self._current_row)
                self._current_row = []
                self._in_row = False

        def handle_data(self, data: str) -> None:
            if self._in_cell and self._current_cell is not None:
                self._current_cell["text"] += data

    parser = TableParser()
    parser.feed(table_html)
    if not parser.rows:
        return []

    grid: list[list[dict[str, Any]]] = []
    spans: dict[int, tuple[dict[str, Any], int]] = {}
    max_cols = 0

    for row_cells in parser.rows:
        row: list[dict[str, Any]] = []
        col_idx = 0

        def flush_spans() -> None:
            nonlocal col_idx
            while col_idx in spans:
                anchor, remaining = spans[col_idx]
                row.append({
                    "text": "",
                    "rowspan": anchor["rowspan"],
                    "colspan": anchor["colspan"],
                    "is_header": anchor["is_header"],
                    "is_anchor": False,
                })
                if remaining <= 1:
                    del spans[col_idx]
                else:
                    spans[col_idx] = (anchor, remaining - 1)
                col_idx += 1

        flush_spans()
        for cell in row_cells:
            flush_spans()
            text = str(cell["text"])
            colspan = int(cell["colspan"])
            rowspan = int(cell["rowspan"])
            anchor = {
                "text": text,
                "rowspan": rowspan,
                "colspan": colspan,
                "is_header": bool(cell["is_header"]),
                "is_anchor": True,
            }
            row.append(anchor)
            if rowspan > 1:
                spans[col_idx] = (anchor, rowspan - 1)
            for offset in range(1, colspan):
                row.append({
                    "text": "",
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "is_header": bool(cell["is_header"]),
                    "is_anchor": False,
                })
                if rowspan > 1:
                    spans[col_idx + offset] = (anchor, rowspan - 1)
            col_idx += colspan
            flush_spans()
        flush_spans()
        max_cols = max(max_cols, len(row))
        grid.append(row)

    if max_cols == 0:
        return []

    padded: list[list[dict[str, Any]]] = []
    for row in grid:
        if len(row) < max_cols:
            row = row + [
                {
                    "text": "",
                    "rowspan": 1,
                    "colspan": 1,
                    "is_header": False,
                    "is_anchor": True,
                }
                for _ in range(max_cols - len(row))
            ]
        padded.append(row)
    return padded


def _extract_tables_from_text(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for match in re.finditer(r"<table.*?</table>", str(text or ""), flags=re.DOTALL | re.IGNORECASE):
        grid = _parse_html_table_grid(match.group(0))
        if not grid:
            continue
        tables.append([[str(cell["text"]).strip() for cell in row] for row in grid])
    return tables


def _find_toc_entries(pages: list[dict[str, Any]], max_page_idx: int = 20) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for page in pages:
        page_idx = int(page["page_idx"])
        if page_idx > max_page_idx:
            continue
        text = str(page.get("text") or "")
        for table in _extract_tables_from_text(text):
            for row in table:
                if len(row) < 2:
                    continue
                title = row[0].strip()
                page_no = row[-1].strip()
                if not title or not re.fullmatch(r"\d{1,4}", page_no):
                    continue
                entries.append(
                    {
                        "toc_page_idx": page_idx,
                        "title": title,
                        "reported_page_no": int(page_no),
                    }
                )
    return entries


# ---------------------------------------------------------------------------
# TOC-driven extraction (toc-scan + section-slim)
# ---------------------------------------------------------------------------

_TOC_LINE_RE = re.compile(r"^(.{2,60}?)\s{1,}(\d{1,4})\s*$")

# Anchors used to compute offset between TOC page numbers and actual page_idx.
# Each tuple: (substring to find in TOC title, pattern to match near top of body page)
_TOC_OFFSET_ANCHORS: list[tuple[str, str]] = [
    ("合并资产负债表", r"合并资产负债表"),
    ("合并利润表", r"合并利润表"),
    ("财务报表附注", r"财务报表附注"),
    ("管理层讨论与分析", r"管理层讨论与分析"),
    ("业绩综述", r"业绩综述"),
]


_TOC_LINE_REVERSED_RE = re.compile(r"^(\d{1,4})\s+(.{2,60}?)\s*$")


def _scan_toc_entries(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan the first 20 pages for TOC entries.

    Handles three formats:
    - HTML table (平安): <table><tr><td>标题</td><td>页码</td></tr>...
    - Plain text forward (新华): 第四节 管理层讨论与分析 27
    - Plain text reversed (太保): 17 经营业绩回顾与分析
    """
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for page in pages:
        page_idx = int(page["page_idx"])
        if page_idx > 20:
            break
        text = str(page.get("text") or "")

        # Only process pages that look like a TOC
        if "目录" not in text and "contents" not in text.lower():
            continue

        # 1. HTML table format (平安 / H 股格式)
        for table in _extract_tables_from_text(text):
            for row in table:
                if len(row) < 2:
                    continue
                title = row[0].strip()
                page_no_str = row[-1].strip()
                if not title or not re.fullmatch(r"\d{1,4}", page_no_str):
                    continue
                page_no = int(page_no_str)
                if 1 <= page_no <= 600 and 2 <= len(title) <= 60:
                    key = (title, page_no)
                    if key not in seen:
                        seen.add(key)
                        entries.append({"title": title, "reported_page_no": page_no, "toc_page_idx": page_idx})

        # 2. Plain text (新华: "标题 页码"；太保: "页码 标题" 两种格式)
        raw = re.sub(r"<[^>]+>", "", text)
        plain_lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

        forward_lines = [ln for ln in plain_lines if _TOC_LINE_RE.match(ln)]
        reversed_lines = [ln for ln in plain_lines if _TOC_LINE_REVERSED_RE.match(ln)
                          and not _TOC_LINE_RE.match(ln)]  # don't double-count

        # forward format (新华 style)
        if len(forward_lines) >= 3:
            for line in forward_lines:
                m = _TOC_LINE_RE.match(line)
                if m:
                    title = m.group(1).strip()
                    page_no = int(m.group(2))
                    if 1 <= page_no <= 600 and 2 <= len(title) <= 60:
                        key = (title, page_no)
                        if key not in seen:
                            seen.add(key)
                            entries.append({"title": title, "reported_page_no": page_no, "toc_page_idx": page_idx})

        # reversed format (太保 style: "17 经营业绩回顾与分析")
        if len(reversed_lines) >= 3:
            for line in reversed_lines:
                m = _TOC_LINE_REVERSED_RE.match(line)
                if m:
                    page_no = int(m.group(1))
                    title = m.group(2).strip()
                    if 1 <= page_no <= 600 and 2 <= len(title) <= 60:
                        key = (title, page_no)
                        if key not in seen:
                            seen.add(key)
                            entries.append({"title": title, "reported_page_no": page_no, "toc_page_idx": page_idx})

    entries.sort(key=lambda e: e["reported_page_no"])
    return entries


# ---------------------------------------------------------------------------
# Sub-TOC detection: some reports embed a "财务报告 页次" index page inside the
# financial section that maps statement names to absolute page_idx values.
# (e.g. 人保 page 119: "合并及公司资产负债表 126  合并及公司利润表 129  ...")
# ---------------------------------------------------------------------------

_FINANCIAL_STMT_SUBTOC_NAMES: list[str] = [
    "合并及公司资产负债表",
    "合并资产负债表",
    "合并及公司利润表",
    "合并利润表",
    "财务报表附注",
    "合并现金流量表",
    "合并股东权益变动表",
]


def _extract_financial_page_map(pages: list[dict[str, Any]]) -> dict[str, int]:
    """Scan all pages for a sub-TOC listing financial-statement names + page numbers.

    Returns a mapping like: {"合并资产负债表": 126, "合并利润表": 129, ...}
    Page numbers are always returned as absolute page_idx values.

    Handles two formats:
      - Absolute: "合并资产负债表 126"  (人保)
      - Relative with P prefix: "合并资产负债表 P5"  (太保) — offset from sub-TOC page
    """
    # Pattern captures optional "P" prefix and the digit part
    _SUBTOC_NUM_RE_SUFFIX = r"[^\dP\n]{0,15}(P?)(\d{1,4})"

    for page in pages:
        text = str(page.get("text") or "")
        page_idx = int(page["page_idx"])
        found: dict[str, int] = {}
        has_p_prefix = False
        for name in _FINANCIAL_STMT_SUBTOC_NAMES:
            m = re.search(re.escape(name) + _SUBTOC_NUM_RE_SUFFIX, text)
            if m:
                if m.group(1) == "P":
                    has_p_prefix = True
                found[name] = int(m.group(2))

        if len(found) >= 2:
            # If "P" prefix detected, numbers are relative to this page
            if has_p_prefix:
                found = {k: page_idx + v for k, v in found.items()}
            return found

    return {}


def _compute_toc_offset(
    pages: list[dict[str, Any]],
    toc_entries: list[dict[str, Any]],
) -> int:
    """Compute offset so that: actual page_idx = reported_page_no + offset.

    Strategy: find a reliable anchor in both the TOC and the document body, then
    compute the difference.
    """
    toc_map: dict[str, int] = {e["title"]: e["reported_page_no"] for e in toc_entries}

    for anchor_title, body_pattern in _TOC_OFFSET_ANCHORS:
        toc_page_no: int | None = None
        for title, page_no in toc_map.items():
            if anchor_title in title:
                toc_page_no = page_no
                break
        if toc_page_no is None:
            continue

        compiled = re.compile(body_pattern)
        for page in pages:
            page_idx = int(page["page_idx"])
            if page_idx <= 20:
                continue
            text = str(page.get("text") or "")
            if compiled.search(text[:300]):
                return page_idx - toc_page_no

    return 0


def _build_toc_with_ranges(
    pages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return (entries_with_ranges, offset).

    Each entry gains start_page_idx and end_page_idx fields.
    """
    raw_entries = _scan_toc_entries(pages)
    if not raw_entries:
        return [], 0

    offset = _compute_toc_offset(pages, raw_entries)
    last_page_idx = int(pages[-1]["page_idx"])

    result: list[dict[str, Any]] = []
    for i, entry in enumerate(raw_entries):
        start_idx = entry["reported_page_no"] + offset
        end_idx = (
            raw_entries[i + 1]["reported_page_no"] + offset - 1
            if i + 1 < len(raw_entries)
            else last_page_idx
        )
        result.append(
            {
                "title": entry["title"],
                "reported_page_no": entry["reported_page_no"],
                "start_page_idx": start_idx,
                "end_page_idx": end_idx,
            }
        )
    return result, offset


def toc_scan(args: argparse.Namespace) -> None:
    """Parse the TOC and output a toc.json with page-range information for every section."""
    pages_json_path = Path(args.pages_json).expanduser().resolve()
    pages_payload = _load_pages_payload(pages_json_path)
    pages = pages_payload["pages"]
    artifact_prefix = _derive_artifact_prefix(pages_json_path)

    entries, offset = _build_toc_with_ranges(pages)
    if not entries:
        raise RuntimeError("No TOC entries found in the first 20 pages.")

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    toc_path = output_dir / f"{artifact_prefix}.toc.json"
    payload: dict[str, Any] = {
        "source_pdf": pages_payload.get("source_pdf"),
        "toc_page_offset": offset,
        "num_entries": len(entries),
        "entries": entries,
    }
    toc_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "ok",
                "toc_json": str(toc_path),
                "offset": offset,
                "num_entries": len(entries),
                "entries_preview": entries[:12],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# Note-level filtering: extract only sub-notes referenced in BS/IS
# ---------------------------------------------------------------------------

_CN_NUM_MAP = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
               "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17,
               "十八": 18, "十九": 19, "二十": 20}


def _parse_note_refs_from_slim(slim_paths: list[str]) -> tuple[str | None, set[int], int | None]:
    """Parse BS/IS slim markdown to extract (main_section_cn, {sub_note_numbers}).

    E.g. for 太保 BS: main_section = "六", sub_notes = {1, 2, 3, 5, 6, ...}
    """
    main_section: str | None = None
    sub_notes: set[int] = set()
    first_bs_note: int | None = None  # first note from first BS line item

    for slim_path in slim_paths:
        text = Path(slim_path).expanduser().resolve().read_text(encoding="utf-8")
        is_bs = "balance_sheet" in slim_path or "资产负债表" in text[:200]

        # Find main section: "附注六", "附注七", "附注八", etc.
        if main_section is None:
            m = re.search(r"附注([一二三四五六七八九十]+)", text)
            if m:
                main_section = m.group(1)

        # Extract sub-note numbers from table cells
        # Handles: "| 1 |", "| 8/56(1) |", "| 10/56(2) |"
        for m in re.finditer(r"\|\s*(\d{1,2})(?:[/\s（(]|\s*\|)", text):
            val = int(m.group(1))
            if 1 <= val <= 99:
                sub_notes.add(val)
                if is_bs and first_bs_note is None:
                    first_bs_note = val

    # When no main section prefix (e.g. 新华 uses "附注" without section number),
    # note numbers < first BS reference are likely false positives from general
    # accounting policy sections (1-7 etc.).  Filter them out.
    if main_section is None and first_bs_note is not None and first_bs_note > 3:
        sub_notes = {n for n in sub_notes if n >= first_bs_note}

    return main_section, sub_notes, first_bs_note


def _filter_notes_by_refs(
    pages: list[dict[str, Any]],
    pages_map: dict[int, dict[str, Any]],
    start_idx: int,
    end_idx: int,
    ref_slim_paths: list[str],
    footer_noise: set[str],
) -> set[int]:
    """Return set of page_idx values that contain referenced sub-notes.

    Scans the financial notes section to find:
    1. The main section header (e.g. "六、合并财务报表...")
    2. Sub-note headings (e.g. "1. 货币资金", "2. 衍生金融工具")
    3. Continuation pages of referenced sub-notes
    """
    main_section_cn, sub_notes, first_bs_note = _parse_note_refs_from_slim(ref_slim_paths)
    if not sub_notes:
        # No refs found — return all pages as fallback
        return set(range(start_idx, end_idx + 1))

    # Convert main section Chinese numeral to find the section header
    main_num = _CN_NUM_MAP.get(main_section_cn, 0) if main_section_cn else 0
    # Build Chinese section prefix: "六、" or "七、"
    section_prefix = f"{main_section_cn}、" if main_section_cn else None

    # Scan pages in the notes range
    # Track which note/sub-note is "active" on each page
    included: set[int] = set()
    in_main_section = False if section_prefix else True  # No prefix = start immediately
    current_subnote: int | None = None
    # Pattern: "N. " or "N、" at start of a line (sub-note heading)
    _SUBNOTE_RE = re.compile(r"(?:^|\n)\s*(\d{1,2})[、\.\s]")
    # Chinese numeral section headings: "八、货币资金" → note 8
    _CN_SECTION_RE = re.compile(
        r"(?:^|\n)(" + "|".join(re.escape(k) for k in sorted(_CN_NUM_MAP, key=len, reverse=True)) + r")[、\.]"
    )

    for idx in range(start_idx, end_idx + 1):
        page = pages_map.get(idx)
        if page is None:
            continue
        text = str(page.get("text") or "")

        # Detect main section start (when section_prefix is known)
        if section_prefix and not in_main_section:
            if section_prefix in text[:60]:
                in_main_section = True
                current_subnote = min(sub_notes) if sub_notes else 1

        if not in_main_section:
            continue

        # Detect next main section → stop (only when we're inside a known section)
        # Check first 3 lines since some pages start with a running header
        if section_prefix:
            next_cn = {v: k for k, v in _CN_NUM_MAP.items()}.get(main_num + 1)
            if next_cn:
                head_text = "\n".join(text.split("\n")[:3])
                if f"{next_cn}、" in head_text:
                    break

        # --- Determine current note number on this page ---
        # Method 1: Arabic numeral headings (sub-notes under a main section)
        subnote_matches = list(_SUBNOTE_RE.finditer(text[:500]))
        if subnote_matches:
            current_subnote = int(subnote_matches[0].group(1))

        # Method 2: Continuation "(续)" with sub-note number
        cont_match = re.search(r"[（\(]续[）\)].*?(\d{1,2})[、\.\s]", text[:200])
        if cont_match:
            current_subnote = int(cont_match.group(1))

        # Method 3: Chinese numeral section headings (新华 style: "八、货币资金")
        # Used when no main section prefix (notes are top-level numbered)
        if not section_prefix:
            cn_match = _CN_SECTION_RE.search(text[:60])
            if cn_match:
                cn_num = _CN_NUM_MAP.get(cn_match.group(1))
                if cn_num is not None:
                    current_subnote = cn_num

        # Method 4 (no-prefix fallback): when the previous section was below
        # the BS threshold and this page has no heading but starts with data
        # content (table), it likely belongs to the first referenced note.
        # E.g. 新华: after section 7 ends, pages 188-189 have note 8/9 data
        # but no heading.
        if (not section_prefix
                and first_bs_note is not None
                and (current_subnote is None or current_subnote < first_bs_note)
                and text.lstrip().startswith("<table>")):
            current_subnote = first_bs_note

        if current_subnote is not None and current_subnote in sub_notes:
            included.add(idx)

    return included


def section_slim(args: argparse.Namespace) -> None:
    """Extract a named section's pages and output a slim LLM-ready markdown.

    Section is looked up via toc.json (or computed on-the-fly from pages.json).
    No keyword scoring needed — the TOC gives us exact page ranges.
    """
    pages_json_path = Path(args.pages_json).expanduser().resolve()
    pages_payload = _load_pages_payload(pages_json_path)
    pages = pages_payload["pages"]
    artifact_prefix = _derive_artifact_prefix(pages_json_path)
    pages_map = {int(p["page_idx"]): p for p in pages}
    footer_noise = _build_footer_noise_set(pages)

    # Load or compute TOC entries with ranges
    if args.toc_json:
        toc_data = json.loads(Path(args.toc_json).expanduser().resolve().read_text(encoding="utf-8"))
        toc_entries = toc_data["entries"]
    else:
        toc_entries, _ = _build_toc_with_ranges(pages)
        if not toc_entries:
            raise RuntimeError(
                "No TOC entries found. Run 'toc-scan' first or pass --toc-json."
            )

    # Resolve section patterns
    section_key = args.section
    if section_key in SECTION_TYPES:
        section_config = SECTION_TYPES[section_key]
        toc_patterns = [re.compile(p, re.IGNORECASE) for p in section_config["toc_patterns"]]
        merge_consecutive: bool = bool(section_config.get("merge_consecutive"))
    else:
        # Treat section_key as a literal TOC title substring
        toc_patterns = [re.compile(re.escape(section_key), re.IGNORECASE)]
        merge_consecutive = False

    matched = [
        e for e in toc_entries
        if any(pat.search(e["title"]) for pat in toc_patterns)
    ]

    # --- TOC hit with verification ---
    # If a TOC entry matches, verify that the mapped page actually contains
    # relevant content (body_patterns).  H+A dual-listed reports (人保, 人寿)
    # have different page offsets for front vs. financial sections, so a TOC hit
    # can map to the wrong page.  If verification fails, fall through to body scan.
    toc_verified = False
    if matched:
        if merge_consecutive:
            first_idx = toc_entries.index(matched[0])
            last_idx = toc_entries.index(matched[-1])
            start_page_idx = toc_entries[first_idx]["start_page_idx"]
            end_page_idx = toc_entries[last_idx]["end_page_idx"]
        else:
            start_page_idx = matched[0]["start_page_idx"]
            end_page_idx = matched[0]["end_page_idx"]

        # Fix invalid range
        if end_page_idx < start_page_idx:
            matched_pos = toc_entries.index(matched[-1])
            stop_pats = section_config.get("stop_body_patterns", []) if section_key in SECTION_TYPES else []
            stop_res = [re.compile(p, re.IGNORECASE) for p in stop_pats]
            found_end = False
            if merge_consecutive and stop_res:
                for subsequent in toc_entries[matched_pos + 1 :]:
                    if any(sr.search(subsequent["title"]) for sr in stop_res):
                        end_page_idx = subsequent["start_page_idx"] - 1
                        found_end = True
                        break
            if not found_end:
                for subsequent in toc_entries[matched_pos + 1 :]:
                    if subsequent["start_page_idx"] > start_page_idx:
                        end_page_idx = subsequent["start_page_idx"] - 1
                        break
                else:
                    end_page_idx = int(pages[-1]["page_idx"])

        # Verify: check that at least one body_pattern appears as a heading
        # in the first few pages of the mapped range.  If not, the offset is
        # likely wrong (common with H+A dual-listed reports).
        body_patterns_list: list[str] = section_config.get("body_patterns", []) if section_key in SECTION_TYPES else []
        # Use strict heading check for non-financial sections (management-discussion)
        # to avoid matching TOC pages or reference text.
        _has_subtoc = bool(section_config.get("subtoc_names")) if section_key in SECTION_TYPES else False
        _verify_fn = _matches_near_top if _has_subtoc else _matches_as_heading
        if body_patterns_list:
            verify_end = min(start_page_idx + 3, end_page_idx + 1)
            for check_idx in range(start_page_idx, verify_end):
                page_data = pages_map.get(check_idx)
                if page_data and _verify_fn(str(page_data.get("text") or ""), body_patterns_list):
                    toc_verified = True
                    break
        else:
            toc_verified = True  # no body patterns to verify against

    if matched and toc_verified:
        fallback_used = False
    else:
        # Fallback: find the section by scanning the document body.
        body_patterns_list: list[str] = section_config.get("body_patterns", []) if section_key in SECTION_TYPES else []
        if not body_patterns_list:
            available = [e["title"] for e in toc_entries]
            raise RuntimeError(
                f"No TOC entry matched section '{section_key}' and no body_patterns defined.\n"
                f"Available TOC entries ({len(available)}): {available}"
            )

        last_page_idx = int(pages[-1]["page_idx"])
        stop_body_patterns: list[str] = section_config.get("stop_body_patterns", []) if section_key in SECTION_TYPES else []
        subtoc_names: list[str] = section_config.get("subtoc_names", []) if section_key in SECTION_TYPES else []
        _has_subtoc = bool(subtoc_names)
        # Non-financial sections use strict heading match to skip TOC pages
        _body_match_fn = _matches_near_top if _has_subtoc else _matches_as_heading

        # Step 1: try sub-TOC lookup (e.g. 人保 page 119 "合并及公司资产负债表 126")
        start_page_idx: int | None = None
        if subtoc_names:
            fin_map = _extract_financial_page_map(pages)
            for name in subtoc_names:
                if name in fin_map:
                    start_page_idx = fin_map[name]
                    break

        # Step 2: body pattern scan across the FULL document if sub-TOC didn't work
        _AUDIT_RE = re.compile(r"审计报告|Auditor")
        if start_page_idx is None:
            for page in pages:
                idx = int(page["page_idx"])
                text = str(page.get("text") or "")
                if _AUDIT_RE.search(text[:150]):
                    continue
                if _body_match_fn(text, body_patterns_list):
                    start_page_idx = idx
                    break

        if start_page_idx is None:
            available = [e["title"] for e in toc_entries]
            raise RuntimeError(
                f"Could not locate section '{section_key}' in the document body.\n"
                f"Available TOC entries: {available}"
            )

        # Step 3: find end using stop patterns (search full doc after start)
        # Use heading-level match for non-financial sections to avoid stopping
        # on table cells that mention the stop keyword (e.g. "集团内含价值" in a table).
        _stop_match_fn = _matches_near_top if _has_subtoc else _matches_as_heading
        end_page_idx = last_page_idx
        if stop_body_patterns:
            for page in pages:
                idx = int(page["page_idx"])
                if idx <= start_page_idx or idx > last_page_idx:
                    continue
                text = str(page.get("text") or "")
                if _stop_match_fn(text, stop_body_patterns):
                    end_page_idx = idx - 1
                    break
        fallback_used = True

    if args.max_pages and (end_page_idx - start_page_idx + 1) > args.max_pages:
        end_page_idx = start_page_idx + args.max_pages - 1

    # --- Note-level filtering for financial-notes ---
    # If --ref-slim files are provided and this is a financial-notes section,
    # only include pages whose sub-note numbers are referenced in the BS/IS.
    ref_slim_paths: list[str] = getattr(args, "ref_slim", None) or []
    note_page_filter: set[int] | None = None  # None = include all pages
    if ref_slim_paths and section_key == "financial-notes":
        note_page_filter = _filter_notes_by_refs(
            pages, pages_map, start_page_idx, end_page_idx, ref_slim_paths, footer_noise,
        )

    # Build slim output
    chunks: list[str] = []
    for idx in range(start_page_idx, end_page_idx + 1):
        if note_page_filter is not None and idx not in note_page_filter:
            continue
        page = pages_map.get(idx)
        if page is None:
            continue
        clean = _clean_page_for_llm(page, footer_noise)
        if clean:
            chunks.append(f"<!-- page: {idx} -->\n{clean}")

    output_text = "\n\n---\n\n".join(chunks)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    section_slug = section_key.replace("-", "_")
    output_path = output_dir / f"{artifact_prefix}.slim.{section_slug}.md"
    output_path.write_text(output_text, encoding="utf-8")

    est_tokens = int(len(output_text) / 1.5)
    print(
        json.dumps(
            {
                "status": "ok",
                "section": section_key,
                "matched_toc_entries": [e["title"] for e in matched] if matched else [],
                "fallback_body_scan": fallback_used,
                "page_range": {"start": start_page_idx, "end": end_page_idx},
                "pages_included": len(chunks),
                "output": str(output_path),
                "chars": len(output_text),
                "est_tokens": est_tokens,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


_MD_PAGE_RE = re.compile(r"(?m)^<!-- page: (\d+) -->\s*$")
_MD_NOTE_HEADING_RE = re.compile(r"^\s*(\d{1,2})(?:[\.、]|\s+)([^\n]{1,80})$")
_MD_SUBSECTION_RE = re.compile(r"^\s*[（(](\d{1,2})[）)]\s*(.+)?$")
_MD_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")


def _derive_statement_artifact_prefix(path: Path) -> str:
    name = path.name
    for suffix in [
        ".slim.balance_sheet.md",
        ".slim.income_statement.md",
        ".slim.financial_notes.md",
    ]:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return _derive_artifact_prefix(path)


def _split_markdown_pages(text: str) -> list[tuple[int, str]]:
    matches = list(_MD_PAGE_RE.finditer(str(text or "")))
    if not matches:
        return []

    pages: list[tuple[int, str]] = []
    for idx, match in enumerate(matches):
        page_idx = int(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        page_text = text[start:end].strip()
        if page_text:
            pages.append((page_idx, page_text))
    return pages


def _split_markdown_blocks(page_text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n{2,}", str(page_text or "")) if block.strip()]


def _parse_markdown_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(_MD_TABLE_SEPARATOR_RE.fullmatch(cell or "") for cell in cells)


def _clean_statement_item_name(name: str) -> str:
    cleaned = str(name or "").strip().strip("*")
    cleaned = re.sub(r"^(?:其中|加|减)\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _normalize_lookup_text(text: str) -> str:
    normalized = _clean_note_text(str(text or ""))
    normalized = normalized.replace("（", "(").replace("）", ")")
    normalized = re.sub(r"[：:\-·•,，。；;、\s]+", "", normalized)
    return normalized.lower()


def _build_item_search_terms(item_name: str) -> list[str]:
    base = _clean_statement_item_name(item_name)
    candidates = [base]
    no_paren = re.sub(r"[（(][^()（）]{0,40}[)）]", "", base).strip()
    if no_paren and no_paren != base:
        candidates.append(no_paren)
    normalized: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip("：: ")
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _parse_note_reference(cell_text: str) -> tuple[str, int, str | None] | None:
    raw = str(cell_text or "").strip()
    if not raw:
        return None
    consolidated = raw.split("/")[0].strip()
    match = re.match(r"(\d{1,2})(?:\s*[（(](\d{1,2})[）)])?", consolidated)
    if not match:
        return None
    note_no = int(match.group(1))
    note_subref = match.group(2)
    note_key = f"{note_no}({note_subref})" if note_subref else str(note_no)
    return note_key, note_no, note_subref


def _extract_statement_note_refs(statement_text: str) -> list[StatementNoteRef]:
    refs: list[StatementNoteRef] = []
    order = 0
    lines = str(statement_text or "").splitlines()
    idx = 0
    while idx < len(lines):
        if not lines[idx].strip().startswith("|"):
            idx += 1
            continue

        start = idx
        while idx < len(lines) and lines[idx].strip().startswith("|"):
            idx += 1
        table_lines = lines[start:idx]
        rows = [_parse_markdown_table_cells(line) for line in table_lines]
        separator_idx = next((row_idx for row_idx, row in enumerate(rows) if _is_markdown_separator_row(row)), None)
        if separator_idx is None:
            continue

        note_col_idx: int | None = None
        for row in rows[: min(len(rows), separator_idx + 3)]:
            for col_idx, cell in enumerate(row):
                if "附注" in cell:
                    note_col_idx = col_idx
                    break
            if note_col_idx is not None:
                break
        if note_col_idx is None:
            continue

        for row in rows[separator_idx + 1 :]:
            if not row or note_col_idx >= len(row):
                continue
            item_name = _clean_statement_item_name(row[0] if row else "")
            if not item_name:
                continue

            parsed_ref = _parse_note_reference(row[note_col_idx])
            if not parsed_ref:
                continue

            current_amount = row[note_col_idx + 1].strip() if note_col_idx + 1 < len(row) else None
            prior_amount = row[note_col_idx + 2].strip() if note_col_idx + 2 < len(row) else None
            refs.append(
                StatementNoteRef(
                    order=order,
                    item_name=item_name,
                    note_key=parsed_ref[0],
                    note_no=parsed_ref[1],
                    note_subref=parsed_ref[2],
                    current_amount=current_amount or None,
                    prior_amount=prior_amount or None,
                )
            )
            order += 1
    return refs


def _group_statement_refs(refs: list[StatementNoteRef]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for ref in refs:
        group = grouped.get(ref.note_key)
        if group is None:
            group = {
                "note_key": ref.note_key,
                "note_no": ref.note_no,
                "note_subref": ref.note_subref,
                "order": ref.order,
                "items": [],
                "terms": [],
                "amounts": [],
            }
            grouped[ref.note_key] = group

        if ref.item_name not in group["items"]:
            group["items"].append(ref.item_name)
        for term in _build_item_search_terms(ref.item_name):
            if term not in group["terms"]:
                group["terms"].append(term)
        for amount in (ref.current_amount, ref.prior_amount):
            if amount and amount not in {"-", "–", "—"} and amount not in group["amounts"]:
                group["amounts"].append(amount)

    return sorted(grouped.values(), key=lambda item: item["order"])


def _find_note_heading_in_block(block: str, max_lines: int = 8) -> tuple[int, str, int] | None:
    lines = [line.strip() for line in str(block or "").splitlines()[:max_lines]]
    for idx, line in enumerate(lines):
        if not line or line.startswith("|") or line.startswith("**"):
            continue
        match = _MD_NOTE_HEADING_RE.match(line)
        if not match:
            continue
        title = re.sub(r"\s*[（(]续[）)]\s*$", "", match.group(2)).strip()
        if not title or len(title) > 80:
            continue
        return int(match.group(1)), title, idx
    return None


def _detect_note_heading(block: str) -> tuple[int, str] | None:
    found = _find_note_heading_in_block(block)
    if found is None:
        return None
    return found[0], found[1]


def _parse_notes_blocks(
    notes_text: str,
    initial_note_no: int | None,
) -> tuple[dict[int, dict[str, Any]], list[NoteBlock]]:
    note_map: dict[int, dict[str, Any]] = {}
    unassigned: list[NoteBlock] = []
    pages = _split_markdown_pages(notes_text)
    if not pages:
        return note_map, unassigned

    first_page_idx = pages[0][0]
    seen_explicit = False
    current_note_no: int | None = None
    block_id = 0

    def push_note_block(note_no: int, page_idx: int, block_text: str, title: str | None = None) -> None:
        nonlocal block_id
        note_entry = note_map.setdefault(note_no, {"title": title, "blocks": []})
        if title and not note_entry.get("title"):
            note_entry["title"] = title
        note_entry["blocks"].append(
            NoteBlock(block_id=block_id, page_idx=page_idx, text=block_text.strip())
        )
        block_id += 1

    def push_unassigned(page_idx: int, block_text: str) -> None:
        nonlocal block_id
        unassigned.append(NoteBlock(block_id=block_id, page_idx=page_idx, text=block_text.strip()))
        block_id += 1

    for page_idx, page_text in pages:
        blocks = _split_markdown_blocks(page_text)
        explicit_positions: list[tuple[int, int, str]] = []
        for pos, block in enumerate(blocks):
            heading = _detect_note_heading(block)
            if heading is not None:
                explicit_positions.append((pos, heading[0], heading[1]))

        if explicit_positions:
            first_explicit_pos = explicit_positions[0][0]
            if first_explicit_pos > 0:
                target_note = None
                if seen_explicit and current_note_no is not None:
                    target_note = current_note_no
                elif not seen_explicit and page_idx == first_page_idx and initial_note_no is not None:
                    target_note = initial_note_no
                for block in blocks[:first_explicit_pos]:
                    if target_note is not None:
                        push_note_block(target_note, page_idx, block)
                    else:
                        push_unassigned(page_idx, block)

            for pos_idx, (start_pos, note_no, title) in enumerate(explicit_positions):
                end_pos = (
                    explicit_positions[pos_idx + 1][0]
                    if pos_idx + 1 < len(explicit_positions)
                    else len(blocks)
                )
                for block in blocks[start_pos:end_pos]:
                    push_note_block(note_no, page_idx, block, title=title)
            current_note_no = explicit_positions[-1][1]
            seen_explicit = True
            continue

        target_note = None
        if not seen_explicit and page_idx == first_page_idx and initial_note_no is not None:
            target_note = initial_note_no

        for block in blocks:
            if target_note is not None:
                push_note_block(target_note, page_idx, block)
            else:
                push_unassigned(page_idx, block)

    return note_map, unassigned


def _score_note_block(block: NoteBlock, terms: list[str], amounts: list[str]) -> int:
    score = 0
    normalized_block = _normalize_lookup_text(block.text)
    for term in terms:
        normalized_term = _normalize_lookup_text(term)
        if normalized_term and normalized_term in normalized_block:
            score = max(score, 12 + min(len(normalized_term), 12))
    for amount in amounts:
        if amount and amount in block.text:
            score += 4
    return score


def _slice_note_subsection_blocks(
    blocks: list[NoteBlock],
    note_subref: str | None,
) -> tuple[list[NoteBlock], str | None]:
    if not blocks or not note_subref:
        return blocks, None

    subsection_blocks: list[NoteBlock] = []
    subsection_title: str | None = None
    current_subref: str | None = None
    seen_subsection = False

    for block in blocks:
        lines = [line.strip() for line in block.text.splitlines()[:8] if line.strip()]
        for line in lines:
            match = _MD_SUBSECTION_RE.match(line)
            if not match:
                continue
            current_subref = match.group(1)
            if current_subref == note_subref:
                subsection_title = (match.group(2) or "").strip() or None
                seen_subsection = True
            break
        if current_subref == note_subref:
            subsection_blocks.append(block)

    if not seen_subsection or not subsection_blocks:
        return blocks, None

    result: list[NoteBlock] = []
    main_heading = _detect_note_heading(blocks[0].text)
    if main_heading is not None:
        result.append(blocks[0])
    for block in subsection_blocks:
        if all(existing.block_id != block.block_id for existing in result):
            result.append(block)
    return result, subsection_title


def _merge_note_blocks(blocks: list[NoteBlock]) -> list[NoteBlock]:
    merged: list[NoteBlock] = []
    seen_ids: set[int] = set()
    for block in sorted(blocks, key=lambda item: item.block_id):
        if block.block_id in seen_ids:
            continue
        seen_ids.add(block.block_id)
        merged.append(block)
    return merged


def _render_note_blocks(blocks: list[NoteBlock]) -> str:
    rendered: list[str] = []
    current_page_idx: int | None = None
    for block in blocks:
        if block.page_idx != current_page_idx:
            rendered.append(f"<!-- page: {block.page_idx} -->")
            current_page_idx = block.page_idx
        rendered.append(block.text)
    return "\n\n".join(rendered).strip()


def _build_statement_with_notes_output(
    *,
    statement_title: str,
    statement_text: str,
    refs: list[StatementNoteRef],
    note_map: dict[int, dict[str, Any]],
    unassigned_blocks: list[NoteBlock],
) -> tuple[str, dict[str, int]]:
    note_groups = _group_statement_refs(refs)
    lines = [
        f"# {statement_title}及对应财务报表附注",
        "",
        "## 原始主表",
        "",
        statement_text.strip(),
        "",
        "## 对应附注",
    ]

    matched_groups = 0
    unmatched_groups = 0

    for group in note_groups:
        note_entry = note_map.get(group["note_no"]) or {}
        note_blocks = list(note_entry.get("blocks") or [])
        note_title = note_entry.get("title") or (group["items"][0] if group["items"] else "")
        scoped_blocks, sub_title = _slice_note_subsection_blocks(note_blocks, group["note_subref"])
        if sub_title:
            note_title = sub_title

        extra_blocks = [
            block
            for block in unassigned_blocks
            if _score_note_block(block, group["terms"], group["amounts"]) >= 8
        ]
        merged_blocks = _merge_note_blocks(scoped_blocks + extra_blocks)

        lines.extend(
            [
                "",
                f"### 附注 {group['note_key']}" + (f" {note_title}" if note_title else ""),
                "",
                f"对应主表科目：{'；'.join(group['items'])}",
            ]
        )

        if merged_blocks:
            matched_groups += 1
            lines.extend(["", _render_note_blocks(merged_blocks)])
        else:
            unmatched_groups += 1
            lines.extend(["", "未在财务报表附注中定位到明确内容，建议人工复核。"])

    output_text = "\n".join(lines).strip() + "\n"
    return output_text, {
        "statement_refs": len(note_groups),
        "matched_refs": matched_groups,
        "unmatched_refs": unmatched_groups,
    }


def statement_notes(args: argparse.Namespace) -> None:
    balance_sheet_path = Path(args.balance_sheet_slim).expanduser().resolve()
    income_statement_path = Path(args.income_statement_slim).expanduser().resolve()
    financial_notes_path = Path(args.financial_notes_slim).expanduser().resolve()

    balance_sheet_text = balance_sheet_path.read_text(encoding="utf-8")
    income_statement_text = income_statement_path.read_text(encoding="utf-8")
    financial_notes_text = financial_notes_path.read_text(encoding="utf-8")

    balance_sheet_refs = _extract_statement_note_refs(balance_sheet_text)
    income_statement_refs = _extract_statement_note_refs(income_statement_text)
    all_refs = balance_sheet_refs + income_statement_refs
    initial_note_no = min((ref.note_no for ref in all_refs), default=None)

    note_map, unassigned_blocks = _parse_notes_blocks(financial_notes_text, initial_note_no)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_prefix = _derive_statement_artifact_prefix(balance_sheet_path)

    balance_sheet_output, balance_stats = _build_statement_with_notes_output(
        statement_title="合并资产负债表",
        statement_text=balance_sheet_text,
        refs=balance_sheet_refs,
        note_map=note_map,
        unassigned_blocks=unassigned_blocks,
    )
    balance_output_path = output_dir / f"{artifact_prefix}.slim.balance_sheet_with_notes.md"
    balance_output_path.write_text(balance_sheet_output, encoding="utf-8")

    income_statement_output, income_stats = _build_statement_with_notes_output(
        statement_title="合并利润表",
        statement_text=income_statement_text,
        refs=income_statement_refs,
        note_map=note_map,
        unassigned_blocks=unassigned_blocks,
    )
    income_output_path = output_dir / f"{artifact_prefix}.slim.income_statement_with_notes.md"
    income_output_path.write_text(income_statement_output, encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "ok",
                "balance_sheet_with_notes": str(balance_output_path),
                "income_statement_with_notes": str(income_output_path),
                "balance_sheet_stats": balance_stats,
                "income_statement_stats": income_stats,
                "note_blocks": sum(len(item.get("blocks") or []) for item in note_map.values()),
                "unassigned_blocks": len(unassigned_blocks),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _infer_note_window(
    pages: list[dict[str, Any]],
    theme_config: dict[str, Any],
) -> PageWindow | None:
    note_start_patterns = list(theme_config.get("note_start_patterns") or DEFAULT_NOTES_PATTERNS)
    note_stop_patterns = list(theme_config.get("note_stop_patterns") or [])
    start_page_idx = None
    for pattern in note_start_patterns:
        for page in pages:
            page_idx = int(page["page_idx"])
            text = str(page.get("text") or "")
            if page_idx <= 20 and "目录" in text:
                continue
            if _matches_near_top(text, [pattern]):
                start_page_idx = page_idx
                break
        if start_page_idx is not None:
            break
    toc_entries = _find_toc_entries(pages)
    if start_page_idx is None:
        for entry in toc_entries:
            if "财务报表附注" in entry["title"]:
                start_page_idx = max(0, entry["reported_page_no"] - 5)
                break
    if start_page_idx is None:
        return None

    end_page_idx = int(pages[-1]["page_idx"])
    if note_stop_patterns:
        stop_page_idx = _find_first_page_index_by_patterns(
            pages,
            note_stop_patterns,
            start_page_idx=start_page_idx + 1,
        )
        if stop_page_idx is not None:
            end_page_idx = stop_page_idx - 1
    if end_page_idx == int(pages[-1]["page_idx"]):
        for entry in toc_entries:
            if "附录" in entry["title"] and "财务报表补充资料" in entry["title"]:
                end_page_idx = min(end_page_idx, entry["reported_page_no"] + 5)
                break

    page_indices = [
        int(page["page_idx"])
        for page in pages
        if start_page_idx <= int(page["page_idx"]) <= end_page_idx
    ]
    if not page_indices:
        return None
    return PageWindow(
        start_page_idx=page_indices[0],
        end_page_idx=page_indices[-1],
        page_indices=page_indices,
    )


def _build_page_lookup(pages: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(page["page_idx"]): page for page in pages}


def _select_page_range(
    pages: list[dict[str, Any]],
    start_page_idx: int,
    end_page_idx: int,
) -> list[dict[str, Any]]:
    return [
        page
        for page in pages
        if start_page_idx <= int(page["page_idx"]) <= end_page_idx
    ]


def _extract_excerpt(text: str, keywords: list[str], max_chars: int = 500) -> str:
    normalized = _clean_note_text(text)
    for keyword in keywords:
        idx = normalized.find(keyword)
        if idx >= 0:
            start = max(0, idx - 120)
            end = min(len(normalized), idx + max_chars)
            return normalized[start:end].strip()
    return normalized[:max_chars].strip()


_TOC_PAGE_RE = re.compile(r"目\s*录")


def _matches_near_top(text: str, patterns: list[str], max_offset: int = 220) -> bool:
    head = str(text or "")[:max_offset]
    return any(re.search(pattern, head, flags=re.IGNORECASE) for pattern in patterns)


def _matches_as_heading(text: str, patterns: list[str]) -> bool:
    """Check if any pattern appears as a heading in the first ~80 chars of the page.

    Stricter than _matches_near_top: avoids matching TOC pages or pages that
    merely *reference* the section name in running text.
    """
    text = str(text or "")
    # Skip main TOC pages
    if _TOC_PAGE_RE.search(text[:30]):
        return False
    # Skip mini-TOC / section-divider pages: if 2+ of the first 5 lines
    # look like "title  number" entries, it's a TOC page, not real content.
    first_lines = [ln.strip() for ln in text.split("\n")[:6] if ln.strip()]
    toc_line_count = sum(1 for ln in first_lines if _TOC_LINE_RE.match(ln) or _TOC_LINE_REVERSED_RE.match(ln))
    if toc_line_count >= 2:
        return False
    # Check only the first line (section headings are always the first line)
    first_line = text.split("\n")[0].strip() if text else ""
    return any(re.search(pattern, first_line, flags=re.IGNORECASE) for pattern in patterns)


def _score_page_for_keywords(text: str, keywords: list[str]) -> tuple[int, list[str]]:
    matched: list[str] = []
    normalized = _clean_note_text(text)
    for keyword in keywords:
        if keyword and keyword in normalized:
            matched.append(keyword)
    unique_matched = list(dict.fromkeys(matched))
    score = 0
    for keyword in unique_matched:
        score += max(10, min(len(keyword) * 3, 30))
    if "<table>" in text:
        score += 15
    return score, unique_matched


def _group_consecutive_page_indices(page_indices: list[int]) -> list[PageWindow]:
    if not page_indices:
        return []
    ordered = sorted(set(page_indices))
    windows: list[PageWindow] = []
    current = [ordered[0]]
    for page_idx in ordered[1:]:
        if page_idx == current[-1] + 1:
            current.append(page_idx)
            continue
        windows.append(
            PageWindow(
                start_page_idx=current[0],
                end_page_idx=current[-1],
                page_indices=list(current),
            )
        )
        current = [page_idx]
    windows.append(
        PageWindow(
            start_page_idx=current[0],
            end_page_idx=current[-1],
            page_indices=list(current),
        )
    )
    return windows


def _detect_theme_hits(
    pages: list[dict[str, Any]],
    theme_config: dict[str, Any],
    *,
    min_score: int,
) -> list[ThemeHit]:
    hits: list[ThemeHit] = []
    keywords = list(theme_config.get("keywords") or [])
    for page in pages:
        page_idx = int(page["page_idx"])
        text = str(page.get("text") or "")
        score, matched_keywords = _score_page_for_keywords(text, keywords)
        if score < min_score or not matched_keywords:
            continue
        hits.append(
            ThemeHit(
                page_idx=page_idx,
                score=score,
                matched_keywords=matched_keywords,
                excerpt=_extract_excerpt(text, matched_keywords),
            )
        )
    hits.sort(key=lambda item: (-item.score, item.page_idx))
    return hits




def build_extraction_records(args: argparse.Namespace) -> None:
    mapping_json_path = Path(args.mapping_json).expanduser().resolve()
    mapping = json.loads(mapping_json_path.read_text(encoding="utf-8"))
    artifact_prefix = _derive_artifact_prefix(mapping_json_path)
    records: list[dict[str, Any]] = []
    for item in mapping:
        matched_note = item.get("matched_note")
        if not matched_note:
            continue
        note_text = _clean_note_text(matched_note.get("text_excerpt", ""))
        tables = _extract_tables_from_text(note_text)
        record = {
            "statement_item": item["item_name"],
            "statement_page_idx": item["page_idx"],
            "note_reference": item.get("note_reference"),
            "current_period_amount": item.get("current_period_amount"),
            "prior_period_amount": item.get("prior_period_amount"),
            "unit": _extract_unit(note_text),
            "note_title": matched_note["title"],
            "note_pages": matched_note["pages"],
            "matched_table_rows": _find_matching_table_rows(
                item_name=item["item_name"],
                current_amount=item.get("current_period_amount"),
                prior_amount=item.get("prior_period_amount"),
                tables=tables,
            )[: args.max_rows_per_item],
            "policy_sentences": _collect_policy_sentences(
                item_name=item["item_name"],
                note_text=note_text,
            )[: args.max_policy_sentences],
            "note_text_excerpt": note_text[: args.max_note_chars],
        }
        records.append(record)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / f"{artifact_prefix}.records.json"
    records_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "output_dir": str(output_dir),
                "records_json": str(records_path),
                "records": len(records),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def extract_theme_hits(args: argparse.Namespace) -> None:
    pages_json_path = Path(args.pages_json).expanduser().resolve()
    pages_payload = _load_pages_payload(pages_json_path)
    pages = pages_payload["pages"]
    artifact_prefix = _derive_artifact_prefix(pages_json_path)
    theme_config = _get_theme_config(args.theme)

    scoped_pages = pages
    note_window = None
    if args.scope == "notes":
        note_window_theme = (
            theme_config
            if theme_config.get("note_start_patterns")
            else THEME_PRESETS["balance-sheet"]
        )
        note_window = _infer_note_window(pages, note_window_theme)
        if note_window is None:
            raise RuntimeError("Could not infer notes page window for theme extraction.")
        scoped_pages = _select_page_range(
            pages,
            note_window.start_page_idx,
            note_window.end_page_idx,
        )

    hits = _detect_theme_hits(
        scoped_pages,
        theme_config,
        min_score=args.min_score,
    )
    hit_windows = _group_consecutive_page_indices([hit.page_idx for hit in hits])

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    theme_slug = args.theme.replace("-", "_")
    output_path = output_dir / f"{artifact_prefix}.{theme_slug}.json"
    payload = {
        "theme": args.theme,
        "theme_description": theme_config.get("description"),
        "scope": args.scope,
        "source_pdf": pages_payload.get("source_pdf"),
        "note_window": asdict(note_window) if note_window is not None else None,
        "hit_windows": [asdict(window) for window in hit_windows],
        "hits": [asdict(hit) for hit in hits[: args.max_hits]],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "theme": args.theme,
                "scope": args.scope,
                "theme_json": str(output_path),
                "hits": len(payload["hits"]),
                "hit_windows": len(hit_windows),
                "note_window": payload["note_window"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _html_table_to_markdown(table_html: str) -> str:
    """Convert an HTML table to standard Markdown table format."""

    def normalize_pipe_cell(text: str) -> str:
        normalized = str(text or "").replace("|", "\\|")
        normalized = re.sub(r"\s*\n\s*", " / ", normalized)
        normalized = re.sub(r"\s{2,}", " ", normalized)
        return normalized.strip()

    def is_value_like(text: str) -> bool:
        candidate = text.strip().replace(",", "")
        if not candidate:
            return False
        if candidate in {"-", "—"}:
            return True
        return bool(re.fullmatch(r"\(?-?\d+(?:\.\d+)?%?\)?", candidate))

    def infer_header_row_count(rows: list[list[dict[str, Any]]], has_merged_cells: bool) -> int:
        if not rows:
            return 0
        if not has_merged_cells or len(rows) == 1:
            return 1

        width = len(rows[0])
        numeric_threshold = max(2, min(width - 1, max(1, width // 2)))
        for row_idx, row in enumerate(rows):
            numeric_count = sum(1 for cell in row[1:] if is_value_like(str(cell["text"])))
            if numeric_count >= numeric_threshold:
                return max(1, row_idx)

        # Most merged-header financial tables use 2 header rows: a super header
        # (year / category) followed by the real column labels.
        return max(1, min(2, len(rows) - 1))

    def effective_header_text(row: list[dict[str, Any]], column_idx: int) -> str:
        cell = row[column_idx]
        text = str(cell["text"]).strip()
        if text:
            return text
        for left_idx in range(column_idx - 1, -1, -1):
            candidate = row[left_idx]
            candidate_text = str(candidate["text"]).strip()
            if (
                candidate.get("is_anchor")
                and candidate_text
                and left_idx + int(candidate["colspan"]) > column_idx
            ):
                return candidate_text
        return ""

    padded = _parse_html_table_grid(table_html)
    if not padded:
        return ""

    max_cols = max(len(row) for row in padded)
    has_merged_cells = any(
        int(cell["rowspan"]) > 1 or int(cell["colspan"]) > 1
        for row in padded
        for cell in row
        if cell.get("is_anchor")
    )

    def md_row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header_row_count = infer_header_row_count(padded, has_merged_cells)
    if header_row_count >= len(padded):
        header_row_count = 1

    super_header: str | None = None
    if has_merged_cells:
        header_parts_by_column: list[list[str]] = []
        for column_idx in range(max_cols):
            parts: list[str] = []
            for row_idx in range(header_row_count):
                text = normalize_pipe_cell(effective_header_text(padded[row_idx], column_idx))
                if text and (not parts or parts[-1] != text):
                    parts.append(text)
            header_parts_by_column.append(parts)

        header = [" / ".join(parts) for parts in header_parts_by_column]
        body = [
            [normalize_pipe_cell(str(cell["text"])) for cell in row]
            for row in padded[header_row_count:]
        ]
    else:
        header = [normalize_pipe_cell(str(cell["text"])) for cell in padded[0]]
        body = [
            [normalize_pipe_cell(str(cell["text"])) for cell in row]
            for row in padded[1:]
        ]

    if header and not header[0].strip():
        non_empty_first_col = sum(1 for row in body if row and row[0].strip())
        if non_empty_first_col >= max(2, len(body) // 3):
            header[0] = "项目"

    cleaned_body: list[list[str]] = []
    seen_value_signatures: set[tuple[str, ...]] = set()
    for row in body:
        if not any(cell.strip() for cell in row):
            continue
        signature = tuple(cell.strip() for cell in row[1:])
        if not row[0].strip():
            if not any(signature):
                continue
            if signature in seen_value_signatures:
                continue
            row = row[:]
            row[0] = "合计"
        cleaned_body.append(row)
        if any(signature):
            seen_value_signatures.add(signature)
    body = cleaned_body

    separator = "|" + "|".join("---" for _ in range(max_cols)) + "|"

    def is_section_row(cells: list[str]) -> bool:
        if not cells:
            return False
        first = cells[0].strip()
        rest = [cell.strip() for cell in cells[1:]]
        return bool(first) and not any(rest)

    section_row_count = sum(1 for row in body if is_section_row(row))
    if section_row_count < 2:
        # Need at least 2 section rows to justify splitting; a single one
        # is more likely a data row that happens to have empty trailing cols.
        lines = [md_row(header), separator]
        for row in body:
            lines.append(md_row(row))
        return "\n".join(lines)

    blocks: list[str] = []
    current_section: str | None = None
    current_rows: list[list[str]] = []

    def flush_current_rows() -> None:
        nonlocal current_rows
        if not current_rows:
            return
        lines = []
        if current_section:
            lines.append(f"**{current_section}**")
        if super_header:
            lines.append(f"**{super_header}**")
        lines.append(md_row(header))
        lines.append(separator)
        for row in current_rows:
            lines.append(md_row(row))
        blocks.append("\n".join(lines))
        current_rows = []

    for row in body:
        if is_section_row(row):
            flush_current_rows()
            current_section = row[0].strip()
            continue
        current_rows.append(row)

    flush_current_rows()
    if blocks:
        return "\n\n".join(blocks)

    lines = []
    if super_header:
        lines.append(f"**{super_header}**")
    lines.append(md_row(header))
    lines.append(separator)
    for row in body:
        lines.append(md_row(row))
    return "\n".join(lines)


def _clean_page_for_llm(
    page: dict[str, Any], footer_noise: set[str] | None = None
) -> str:
    """Return LLM-ready text for a page: HTML tables become standard Markdown tables."""
    text = page["text"]
    text = re.sub(
        r"<table.*?</table>",
        lambda m: f"\n\n{_html_table_to_markdown(m.group(0))}\n\n",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = _polish_readability(_clean_special_symbols(text))
    text = _strip_header_noise(text, footer_noise)
    text = _strip_footer_noise(text, footer_noise)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def slim_for_llm(args: argparse.Namespace) -> None:
    pages_json_path = Path(args.pages_json).expanduser().resolve()
    pages_payload = _load_pages_payload(pages_json_path)
    pages = pages_payload["pages"]
    pages_map = {int(p["page_idx"]): p for p in pages}
    artifact_prefix = _derive_artifact_prefix(pages_json_path)

    # Auto-detect report-specific footer noise (company name etc.)
    footer_noise = _build_footer_noise_set(pages)

    theme_json_path = Path(args.theme_json).expanduser().resolve()
    theme_data = json.loads(theme_json_path.read_text(encoding="utf-8"))
    hits = theme_data.get("hits", [])
    hit_indices = sorted(set(h["page_idx"] for h in hits))
    theme_name = theme_data.get("theme", "unknown")

    # Expand each hit with ±context pages
    selected: set[int] = set()
    for idx in hit_indices:
        for offset in range(-args.context, args.context + 1):
            neighbor = idx + offset
            if neighbor in pages_map:
                selected.add(neighbor)

    selected_sorted = sorted(selected)

    chunks: list[str] = []
    for idx in selected_sorted:
        page = pages_map[idx]
        flag = " [hit]" if idx in hit_indices else ""
        clean = _clean_page_for_llm(page, footer_noise)
        if clean:
            chunks.append(f"<!-- page: {idx}{flag} -->\n{clean}")

    output_text = "\n\n---\n\n".join(chunks)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    theme_slug = theme_name.replace("-", "_")
    output_path = output_dir / f"{artifact_prefix}.slim.{theme_slug}.md"
    output_path.write_text(output_text, encoding="utf-8")

    est_tokens = int(len(output_text) / 1.5)
    print(
        json.dumps(
            {
                "status": "ok",
                "theme": theme_name,
                "hit_pages": len(hit_indices),
                "total_pages": len(selected_sorted),
                "output": str(output_path),
                "chars": len(output_text),
                "est_tokens": est_tokens,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def convert_pdf(args: argparse.Namespace) -> None:
    api_key = _require_env(args.mineru_env)
    pdf_path = Path(args.pdf).expanduser().resolve()
    output_root = Path(args.output_dir).expanduser().resolve()
    output_dir = output_root / pdf_path.stem
    base_url = args.mineru_base_url.rstrip("/")

    apply_url = f"{base_url}/api/v4/file-urls/batch"
    apply_payload = {
        "files": [{"name": pdf_path.name, "data_id": pdf_path.stem}],
        "model_version": args.mineru_model,
        "language": args.language,
        "enable_formula": args.enable_formula,
        "enable_table": args.enable_table,
        "is_ocr": args.is_ocr,
    }
    if args.page_ranges:
        apply_payload["page_ranges"] = args.page_ranges

    apply_response = requests.post(
        apply_url,
        headers=_mineru_headers(api_key),
        json=apply_payload,
        timeout=60,
    )
    apply_response.raise_for_status()
    apply_result = apply_response.json()
    if apply_result.get("code") != 0:
        raise RuntimeError(
            f"MinerU apply upload URL failed: code={apply_result.get('code')} msg={apply_result.get('msg')}"
        )

    batch_id = apply_result["data"]["batch_id"]
    file_urls = apply_result["data"]["file_urls"]
    if not file_urls:
        raise RuntimeError("MinerU did not return any upload URL.")

    with pdf_path.open("rb") as file_obj:
        upload_response = requests.put(file_urls[0], data=file_obj, timeout=300)
    if upload_response.status_code not in (200, 201):
        raise RuntimeError(
            f"MinerU upload failed: HTTP {upload_response.status_code}"
        )

    result = _poll_mineru_batch_result(
        api_key=api_key,
        base_url=base_url,
        batch_id=batch_id,
        timeout_seconds=args.timeout,
        interval_seconds=args.poll_interval,
    )
    zip_url = result.get("full_zip_url")
    if not zip_url:
        raise RuntimeError("MinerU result did not include full_zip_url.")

    extract_dir = _download_and_extract_zip_to_temp(zip_url=zip_url)
    try:
        content_list_path = _find_single_file(extract_dir, "*content_list.json")
        if content_list_path is None:
            raise RuntimeError(
                f"Could not find content_list JSON under {extract_dir}. MinerU zip format may have changed."
            )

        content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    finally:
        import shutil

        shutil.rmtree(extract_dir.parent, ignore_errors=True)

    pages = _group_content_by_page(
        content_list=content_list,
        statement_patterns=args.statement_pattern,
        notes_patterns=args.notes_pattern,
    )
    written_files = _save_conversion_artifacts(
        output_dir=output_dir,
        pdf_path=pdf_path,
        pages=pages,
        artifact_prefix=pdf_path.stem,
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "source_pdf": str(pdf_path),
                "output_dir": str(output_dir),
                "pages_json": written_files["pages_json"],
                "markdown": written_files["markdown"],
                "candidates_json": written_files["candidates_json"],
                "batch_id": batch_id,
                "num_pages": len(pages),
                "statement_candidates": [
                    page.page_idx
                    for page in pages
                    if any(tag.startswith("statement:") for tag in page.matched_tags)
                ],
                "notes_candidates": [
                    page.page_idx
                    for page in pages
                    if any(tag.startswith("notes:") for tag in page.matched_tags)
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    theme_choices = sorted(THEME_PRESETS)
    parser = argparse.ArgumentParser(
        description="FinReport Scope pipeline for financial-report extraction with MinerU cloud API."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert_parser = subparsers.add_parser(
        "convert", help="Convert PDF to page-level text/markdown with MinerU."
    )
    convert_parser.add_argument("--pdf", required=True, help="Path to the source PDF.")
    convert_parser.add_argument(
        "--output-dir", required=True, help="Directory for pages.json and document.md."
    )
    convert_parser.add_argument(
        "--mineru-env",
        default="MINERU_API_KEY",
        help="Env var that holds the MinerU API key.",
    )
    convert_parser.add_argument(
        "--mineru-base-url",
        default="https://mineru.net",
        help="MinerU official API base URL.",
    )
    convert_parser.add_argument("--mineru-model", default="vlm")
    convert_parser.add_argument("--language", default="ch")
    convert_parser.add_argument("--timeout", type=int, default=600)
    convert_parser.add_argument("--poll-interval", type=int, default=5)
    convert_parser.add_argument("--page-ranges")
    convert_parser.add_argument("--enable-formula", action="store_true", default=True)
    convert_parser.add_argument("--disable-formula", dest="enable_formula", action="store_false")
    convert_parser.add_argument("--enable-table", action="store_true", default=True)
    convert_parser.add_argument("--disable-table", dest="enable_table", action="store_false")
    convert_parser.add_argument("--is-ocr", action="store_true", default=False)
    convert_parser.add_argument(
        "--statement-pattern",
        action="append",
        default=list(DEFAULT_STATEMENT_PATTERNS),
        help="Regex for locating statement pages. Can be repeated.",
    )
    convert_parser.add_argument(
        "--notes-pattern",
        action="append",
        default=list(DEFAULT_NOTES_PATTERNS),
        help="Regex for locating notes pages. Can be repeated.",
    )
    convert_parser.set_defaults(func=convert_pdf)

    theme_parser = subparsers.add_parser(
        "extract-theme",
        help="Find high-signal pages and evidence windows for a reusable financial theme.",
    )
    theme_parser.add_argument("--pages-json", required=True)
    theme_parser.add_argument("--output-dir", required=True)
    theme_parser.add_argument(
        "--theme",
        choices=theme_choices,
        required=True,
    )
    theme_parser.add_argument(
        "--scope",
        choices=("all", "notes"),
        default="all",
        help="Whether to scan the full report or only the inferred financial-notes window.",
    )
    theme_parser.add_argument("--min-score", type=int, default=10)
    theme_parser.add_argument("--max-hits", type=int, default=30)
    theme_parser.set_defaults(func=extract_theme_hits)

    slim_parser = subparsers.add_parser(
        "slim",
        help="Build a lean LLM-ready markdown from a theme's hit pages + context.",
    )
    slim_parser.add_argument("--pages-json", required=True, help="Path to pages.json.")
    slim_parser.add_argument(
        "--theme-json", required=True, help="Path to <stem>.<theme>.json produced by extract-theme."
    )
    slim_parser.add_argument("--output-dir", required=True)
    slim_parser.add_argument(
        "--context",
        type=int,
        default=1,
        help="Number of pages of context to include on each side of a hit (default: 1).",
    )
    slim_parser.set_defaults(func=slim_for_llm)

    # ------------------------------------------------------------------
    # toc-scan: parse TOC and output toc.json with page-range info
    # ------------------------------------------------------------------
    toc_parser = subparsers.add_parser(
        "toc-scan",
        help="Parse the annual report TOC and output a toc.json with page-range for every section.",
    )
    toc_parser.add_argument("--pages-json", required=True, help="Path to pages.json.")
    toc_parser.add_argument("--output-dir", required=True, help="Directory for toc.json.")
    toc_parser.set_defaults(func=toc_scan)

    # ------------------------------------------------------------------
    # section-slim: TOC-driven extraction of a named section → slim.md
    # No keyword scoring; page range comes directly from toc.json.
    # ------------------------------------------------------------------
    section_choices = sorted(SECTION_TYPES)
    section_parser = subparsers.add_parser(
        "section-slim",
        help="Extract a named section via TOC lookup and output a slim LLM-ready markdown.",
    )
    section_parser.add_argument("--pages-json", required=True, help="Path to pages.json.")
    section_parser.add_argument("--output-dir", required=True)
    section_parser.add_argument(
        "--section",
        required=True,
        help=(
            f"Section to extract. Built-in choices: {section_choices}. "
            "Or pass any substring of a TOC entry title for a direct match."
        ),
    )
    section_parser.add_argument(
        "--toc-json",
        default=None,
        help="Path to toc.json from toc-scan. If omitted, TOC is parsed on the fly.",
    )
    section_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Cap the section at this many pages (useful for very long financial-notes).",
    )
    section_parser.add_argument(
        "--ref-slim",
        nargs="+",
        default=None,
        help=(
            "For financial-notes: path(s) to balance-sheet / income-statement slim .md files. "
            "When provided, only sub-notes referenced in those statements are extracted."
        ),
    )
    section_parser.set_defaults(func=section_slim)

    statement_notes_parser = subparsers.add_parser(
        "statement-notes",
        help="Compose balance-sheet / income-statement docs with their matched financial notes.",
    )
    statement_notes_parser.add_argument("--balance-sheet-slim", required=True)
    statement_notes_parser.add_argument("--income-statement-slim", required=True)
    statement_notes_parser.add_argument("--financial-notes-slim", required=True)
    statement_notes_parser.add_argument("--output-dir", required=True)
    statement_notes_parser.set_defaults(func=statement_notes)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
