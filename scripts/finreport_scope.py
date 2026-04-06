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
        return ""

    grid: list[list[str]] = []
    spans: dict[int, tuple[int, str]] = {}
    max_cols = 0

    for row_cells in parser.rows:
        row: list[str] = []
        col_idx = 0

        def flush_spans() -> None:
            nonlocal col_idx
            while col_idx in spans:
                remaining, value = spans[col_idx]
                row.append(value)
                if remaining <= 1:
                    del spans[col_idx]
                else:
                    spans[col_idx] = (remaining - 1, value)
                col_idx += 1

        flush_spans()
        for cell in row_cells:
            flush_spans()
            text = str(cell["text"]).replace("|", "\\|")
            colspan = int(cell["colspan"])
            rowspan = int(cell["rowspan"])
            for offset in range(colspan):
                value = text if offset == 0 else ""
                row.append(value)
                if rowspan > 1:
                    spans[col_idx] = (rowspan - 1, value)
                col_idx += 1
            flush_spans()
        flush_spans()
        max_cols = max(max_cols, len(row))
        grid.append(row)

    if max_cols == 0:
        return ""

    padded = [row + [""] * (max_cols - len(row)) for row in grid]

    def md_row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header = padded[0]
    separator = "|" + "|".join("---" for _ in range(max_cols)) + "|"
    body = padded[1:]

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
    return "\n\n".join(blocks)


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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
