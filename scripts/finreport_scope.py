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


DEFAULT_STATEMENT_PATTERNS = [
    r"合并资产负债表",
    r"Consolidated Balance Sheet",
    r"Balance Sheet",
]
DEFAULT_NOTES_PATTERNS = [
    r"财务报表附注",
    r"Notes to the Financial Statements",
]
STATEMENT_ROW_RE = re.compile(
    r"<tr><td>([^<]+)</td><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td></tr>"
)
NOTE_HEADING_RE = re.compile(r"(?m)^(?P<note_no>\d{1,2})\.\s*(?P<title>[^\n（(]+)")
TOP_LEVEL_HEADING_RE = re.compile(r"(?m)^[一二三四五六七八九十]+、[^\n]+")



@dataclass
class PageRecord:
    page_idx: int
    text: str
    markdown: str
    matched_tags: list[str]


@dataclass
class StatementRow:
    page_idx: int
    item_name: str
    note_reference: str | None
    current_period_amount: str | None
    prior_period_amount: str | None


@dataclass
class NoteSection:
    note_no: str
    title: str
    start_page_idx: int
    end_page_idx: int
    pages: list[int]
    text: str


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
        ".statement_rows.json",
        ".note_sections.json",
        ".statement_note_map.json",
        ".records.json",
        ".candidates.json",
        ".md",
        ".json",
    ]:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _parse_statement_rows(statement_pages: list[dict[str, Any]]) -> list[StatementRow]:
    rows: list[StatementRow] = []
    for page in statement_pages:
        for item_name, note_ref, current_amt, prior_amt in STATEMENT_ROW_RE.findall(
            page["text"]
        ):
            item_name = item_name.strip()
            note_ref = note_ref.strip() or None
            current_amt = current_amt.strip() or None
            prior_amt = prior_amt.strip() or None
            if not item_name:
                continue
            if item_name in {
                "资产",
                "负债",
                "股东权益",
                "负债及股东权益",
                "金融投资：",
            }:
                continue
            if not current_amt and not prior_amt:
                continue
            rows.append(
                StatementRow(
                    page_idx=int(page["page_idx"]),
                    item_name=item_name,
                    note_reference=note_ref,
                    current_period_amount=current_amt,
                    prior_period_amount=prior_amt,
                )
            )
    return rows


def _build_note_sections(notes_pages: list[dict[str, Any]]) -> list[NoteSection]:
    matches: list[dict[str, Any]] = []
    for page in notes_pages:
        page_text = page["text"]
        for match in NOTE_HEADING_RE.finditer(page_text):
            title = match.group("title").strip(" ：:")
            note_no = match.group("note_no")
            if matches:
                prev = matches[-1]
                if (
                    prev["note_no"] == note_no
                    and _normalize_item_name(prev["title"]) == _normalize_item_name(title)
                ):
                    continue
            matches.append(
                {
                    "note_no": note_no,
                    "title": title,
                    "page_idx": int(page["page_idx"]),
                    "start": match.start(),
                }
            )

    if not matches:
        return []

    page_map = {int(page["page_idx"]): page["text"] for page in notes_pages}
    ordered_pages = sorted(page_map)
    page_position = {page_idx: i for i, page_idx in enumerate(ordered_pages)}
    sections: list[NoteSection] = []

    for idx, current in enumerate(matches):
        next_match = matches[idx + 1] if idx + 1 < len(matches) else None
        start_page = current["page_idx"]
        start_offset = current["start"]
        if idx == 0 and ordered_pages[0] < start_page:
            start_page = ordered_pages[0]
            start_offset = 0
        end_page = next_match["page_idx"] if next_match else ordered_pages[-1]
        collected_pages: list[int] = []
        text_chunks: list[str] = []

        start_pos = page_position[start_page]
        end_pos = page_position[end_page]
        for pos in range(start_pos, end_pos + 1):
            page_idx = ordered_pages[pos]
            if pos > start_pos and ordered_pages[pos] != ordered_pages[pos - 1] + 1:
                break
            page_text = page_map[page_idx]
            if page_idx == start_page and page_idx == end_page and next_match:
                text_chunks.append(page_text[start_offset : next_match["start"]].strip())
            elif page_idx == start_page:
                text_chunks.append(page_text[start_offset:].strip())
            elif next_match and page_idx == end_page:
                text_chunks.append(page_text[: next_match["start"]].strip())
            else:
                text_chunks.append(page_text.strip())
            collected_pages.append(page_idx)

        section_text = "\n\n".join(chunk for chunk in text_chunks if chunk).strip()
        section_text = _clean_note_text(section_text)
        sections.append(
            NoteSection(
                note_no=current["note_no"],
                title=current["title"],
                start_page_idx=start_page,
                end_page_idx=collected_pages[-1],
                pages=collected_pages,
                text=section_text,
            )
        )
    return sections


def _normalize_item_name(text: str) -> str:
    normalized = text.strip()
    normalized = normalized.replace("其中：", "")
    normalized = normalized.replace("其中:", "")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.strip("：:")
    return normalized


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


def _auto_select_statement_pages(
    pages: list[dict[str, Any]],
    theme_config: dict[str, Any],
) -> list[int]:
    statement_patterns = list(theme_config.get("statement_patterns") or DEFAULT_STATEMENT_PATTERNS)
    return _find_page_indices_by_patterns(
        pages,
        statement_patterns,
        skip_toc_pages=True,
    )


def _extract_excerpt(text: str, keywords: list[str], max_chars: int = 500) -> str:
    normalized = _clean_note_text(text)
    for keyword in keywords:
        idx = normalized.find(keyword)
        if idx >= 0:
            start = max(0, idx - 120)
            end = min(len(normalized), idx + max_chars)
            return normalized[start:end].strip()
    return normalized[:max_chars].strip()


def _matches_near_top(text: str, patterns: list[str], max_offset: int = 220) -> bool:
    head = str(text or "")[:max_offset]
    return any(re.search(pattern, head, flags=re.IGNORECASE) for pattern in patterns)


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


def _match_note_section(
    row: StatementRow, note_sections: list[NoteSection]
) -> NoteSection | None:
    if row.note_reference:
        for section in note_sections:
            if section.note_no == row.note_reference:
                return section

    target = _normalize_item_name(row.item_name)
    if not target:
        return None

    for section in note_sections:
        title = _normalize_item_name(section.title)
        if title == target or target in title or title in target:
            return section
    return None


def locate_statement_notes(args: argparse.Namespace) -> None:
    pages_json_path = Path(args.pages_json).expanduser().resolve()
    pages_payload = _load_pages_payload(pages_json_path)
    pages = pages_payload["pages"]
    artifact_prefix = _derive_artifact_prefix(pages_json_path)
    theme_config = _get_theme_config(args.theme)

    statement_page_indices = args.statement_pages or _auto_select_statement_pages(
        pages, theme_config
    )
    statement_pages = _select_pages(
        pages=pages,
        page_indices=statement_page_indices,
        fallback_tags=("statement:",),
    )

    inferred_note_window = None
    note_page_indices = args.notes_pages
    if note_page_indices is None:
        inferred_note_window = _infer_note_window(pages, theme_config)
        if inferred_note_window is not None:
            note_page_indices = inferred_note_window.page_indices
    notes_pages = _select_pages(
        pages=pages,
        page_indices=note_page_indices,
        fallback_tags=("notes:",),
    )
    if not statement_pages:
        raise RuntimeError("No statement pages found for locator.")
    if not notes_pages:
        raise RuntimeError("No note pages found for locator.")

    statement_rows = _parse_statement_rows(statement_pages)
    note_sections = _build_note_sections(notes_pages)

    results: list[dict[str, Any]] = []
    for row in statement_rows:
        matched = _match_note_section(row, note_sections)
        if matched is None:
            matched = _build_fallback_note_section(row, notes_pages)
        results.append(
            {
                **asdict(row),
                "matched_note": (
                    {
                        "note_no": matched.note_no,
                        "title": matched.title,
                        "start_page_idx": matched.start_page_idx,
                        "end_page_idx": matched.end_page_idx,
                        "pages": matched.pages,
                        "text_excerpt": matched.text[: args.max_note_chars],
                    }
                    if matched
                    else None
                ),
            }
        )

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    statement_rows_path = output_dir / f"{artifact_prefix}.statement_rows.json"
    note_sections_path = output_dir / f"{artifact_prefix}.note_sections.json"
    statement_note_map_path = output_dir / f"{artifact_prefix}.statement_note_map.json"
    statement_rows_path.write_text(
        json.dumps([asdict(row) for row in statement_rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    note_sections_path.write_text(
        json.dumps([asdict(section) for section in note_sections], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    statement_note_map_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    matched_count = sum(1 for item in results if item["matched_note"] is not None)
    print(
        json.dumps(
            {
                "status": "ok",
                "output_dir": str(output_dir),
                "statement_rows_json": str(statement_rows_path),
                "note_sections_json": str(note_sections_path),
                "statement_note_map_json": str(statement_note_map_path),
                "theme": args.theme,
                "statement_pages": [int(page["page_idx"]) for page in statement_pages],
                "notes_page_range": {
                    "start_page_idx": int(notes_pages[0]["page_idx"]),
                    "end_page_idx": int(notes_pages[-1]["page_idx"]),
                    "num_pages": len(notes_pages),
                    "auto_detected": args.notes_pages is None,
                },
                "inferred_note_window": (
                    asdict(inferred_note_window) if inferred_note_window is not None else None
                ),
                "statement_rows": len(statement_rows),
                "note_sections": len(note_sections),
                "matched_rows": matched_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _clean_table_cell(text: str) -> str:
    value = re.sub(r"<[^>]+>", "", text)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_tables_from_text(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for table_html in re.findall(r"<table>(.*?)</table>", text, flags=re.DOTALL):
        rows: list[list[str]] = []
        for row_html in re.findall(r"<tr>(.*?)</tr>", table_html, flags=re.DOTALL):
            cells = [
                _clean_table_cell(cell_html)
                for cell_html in re.findall(
                    r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.DOTALL
                )
            ]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _extract_unit(text: str) -> str | None:
    match = re.search(r"金额单位为([^\n）)]+)", text)
    if match:
        return match.group(1).strip()
    return None


def _split_sentences(text: str) -> list[str]:
    normalized = _clean_note_text(text)
    normalized = re.sub(r"<table>.*?</table>", "", normalized, flags=re.DOTALL)
    normalized = normalized.replace("\n", "")
    parts = re.split(r"(?<=[。；])", normalized)
    return [part.strip() for part in parts if part.strip()]


def _collect_policy_sentences(item_name: str, note_text: str) -> list[str]:
    normalized_name = _normalize_item_name(item_name)
    keywords = [
        normalized_name,
        "不能用于日常经营",
        "按",
        "减值准备",
        "折算汇率",
        "受限制",
        "抵押",
        "冻结",
        "质押品",
        "分析如下",
        "可收回金额",
        "折现率",
        "增长率",
        "可抵扣亏损",
        "互抵金额",
    ]
    sentences: list[str] = []
    for sentence in _split_sentences(note_text):
        normalized_sentence = _normalize_item_name(sentence)
        if any(keyword and keyword in normalized_sentence for keyword in keywords):
            sentences.append(sentence)
    deduped: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        if sentence not in seen:
            seen.add(sentence)
            deduped.append(sentence)
    return deduped


def _find_matching_table_rows(
    item_name: str,
    current_amount: str | None,
    prior_amount: str | None,
    tables: list[list[list[str]]],
) -> list[dict[str, Any]]:
    normalized_name = _normalize_item_name(item_name)
    scored_matches: list[tuple[int, dict[str, Any]]] = []
    for table_idx, table in enumerate(tables):
        for row_idx, row in enumerate(table):
            if not any(cell.strip() for cell in row):
                continue
            normalized_cells = [_normalize_item_name(cell) for cell in row]
            score = 0
            if any(normalized_name and normalized_name == cell for cell in normalized_cells):
                score += 100
            elif any(
                normalized_name and (
                    normalized_name in cell or cell in normalized_name
                )
                for cell in normalized_cells
                if cell
            ):
                score += 60

            has_current = bool(current_amount and current_amount in row)
            has_prior = bool(prior_amount and prior_amount in row)
            if has_current and has_prior:
                score += 80
            elif has_current or has_prior:
                score += 20

            if score > 0:
                scored_matches.append(
                    (
                        score,
                        {
                            "table_index": table_idx,
                            "row_index": row_idx,
                            "cells": row,
                        },
                    )
                )

    scored_matches.sort(
        key=lambda item: (
            -item[0],
            item[1]["table_index"],
            item[1]["row_index"],
        )
    )
    matches: list[dict[str, Any]] = []
    seen_rows: set[tuple[int, int]] = set()
    for _, row_data in scored_matches:
        row_key = (row_data["table_index"], row_data["row_index"])
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)
        matches.append(
            {
                "table_index": row_data["table_index"],
                "row_index": row_data["row_index"],
                "cells": row_data["cells"],
            }
        )
    return matches


def _build_fallback_note_section(
    row: StatementRow,
    notes_pages: list[dict[str, Any]],
) -> NoteSection | None:
    normalized_name = _normalize_item_name(row.item_name)
    best_page: dict[str, Any] | None = None
    best_score = 0
    for page in notes_pages:
        text = str(page.get("text") or "")
        page_idx = int(page["page_idx"])
        score = 0
        normalized_text = _normalize_item_name(text)
        if normalized_name and normalized_name in normalized_text:
            score += 50
        if row.current_period_amount and row.current_period_amount in text:
            score += 40
        if row.prior_period_amount and row.prior_period_amount in text:
            score += 30

        table_matches = _find_matching_table_rows(
            item_name=row.item_name,
            current_amount=row.current_period_amount,
            prior_amount=row.prior_period_amount,
            tables=_extract_tables_from_text(text),
        )
        if table_matches:
            score += 60

        if score > best_score:
            best_score = score
            best_page = {"page_idx": page_idx, "text": text}

    if best_page is None or best_score < 90:
        return None

    return NoteSection(
        note_no=row.note_reference or "",
        title=row.item_name,
        start_page_idx=int(best_page["page_idx"]),
        end_page_idx=int(best_page["page_idx"]),
        pages=[int(best_page["page_idx"])],
        text=_clean_note_text(str(best_page["text"])),
    )


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


def _select_pages(
    pages: list[dict[str, Any]],
    page_indices: list[int] | None,
    fallback_tags: tuple[str, ...],
) -> list[dict[str, Any]]:
    if page_indices:
        wanted = set(page_indices)
        return [page for page in pages if int(page["page_idx"]) in wanted]
    return [
        page
        for page in pages
        if any(str(tag).startswith(fallback_tags) for tag in page.get("matched_tags", []))
    ]


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

    locate_parser = subparsers.add_parser(
        "locate",
        help="Locate note sections corresponding to balance-sheet rows.",
    )
    locate_parser.add_argument("--pages-json", required=True, help="Path to pages.json.")
    locate_parser.add_argument("--output-dir", required=True)
    locate_parser.add_argument(
        "--theme",
        choices=theme_choices,
        default="balance-sheet",
        help="Extraction theme preset. Current row-to-note mapping is implemented for balance-sheet best.",
    )
    locate_parser.add_argument("--statement-pages", type=int, nargs="*")
    locate_parser.add_argument("--notes-pages", type=int, nargs="*")
    locate_parser.add_argument("--max-note-chars", type=int, default=4000)
    locate_parser.set_defaults(func=locate_statement_notes)

    records_parser = subparsers.add_parser(
        "build-records",
        help="Build structured extraction records from statement-note mappings.",
    )
    records_parser.add_argument("--mapping-json", required=True)
    records_parser.add_argument("--output-dir", required=True)
    records_parser.add_argument("--max-note-chars", type=int, default=4000)
    records_parser.add_argument("--max-rows-per-item", type=int, default=12)
    records_parser.add_argument("--max-policy-sentences", type=int, default=8)
    records_parser.set_defaults(func=build_extraction_records)

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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
