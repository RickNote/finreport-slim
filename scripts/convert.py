#!/usr/bin/env python3
"""
pdf-to-md: Convert a PDF to Markdown via the MinerU cloud API.

Usage:
    python convert.py <pdf_path> [output_dir] [options]

    If output_dir is omitted, creates a subfolder next to the PDF.

Environment / Config:
    MINERU_API_KEY  — Bearer token, read from env var first, then from
                      ../config.env in the skill folder (auto-loaded).

Outputs (in <output_dir>/<pdf_stem>/):
    <stem>.md             Full document markdown with page markers
    <stem>.pages.json     Page-level text, markdown, and matched tags
    <stem>.candidates.json  Statement / notes page candidates
"""

import argparse
import json
import os
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PageRecord:
    page_idx: int
    text: str
    markdown: str
    matched_tags: list[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_config_env() -> None:
    """Load key=value pairs from ../config.env (relative to this script) into os.environ."""
    config_path = Path(__file__).parent.parent / "config.env"
    if not config_path.exists():
        return
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _require_env(name: str) -> str:
    _load_config_env()
    value = os.getenv(name)
    if not value:
        sys.exit(
            f"ERROR: '{name}' not set.\n"
            f"  Set the environment variable, or edit the skill's config.env file."
        )
    return value


def _mineru_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _extract_text_from_item(item: dict[str, Any]) -> str:
    for key in ("text", "table_body", "code_body"):
        value = item.get(key)
        if value:
            return str(value).strip()
    list_items = item.get("list_items") or []
    if list_items:
        return "\n".join(str(x) for x in list_items).strip()
    return ""


def _render_markdown_line(item: dict[str, Any], text: str) -> str:
    item_type = str(item.get("type") or item.get("category") or "").lower()
    if "title" in item_type or "heading" in item_type:
        return f"## {text}"
    return text


def _group_content_by_page(content_list: list[dict[str, Any]]) -> list[PageRecord]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in content_list:
        grouped[int(item.get("page_idx", 0))].append(item)

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

        pages.append(PageRecord(
            page_idx=page_idx,
            text="\n".join(texts).strip(),
            markdown="\n\n".join(markdown_lines).strip(),
            matched_tags=[],
        ))
    return pages


def _poll_mineru_batch_result(
    *,
    api_key: str,
    base_url: str,
    batch_id: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    url = f"{base_url}/api/v4/extract-results/batch/{batch_id}"
    print(f"  Polling batch {batch_id} ...", flush=True)

    while time.time() < deadline:
        resp = requests.get(url, headers=_mineru_headers(api_key), timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            sys.exit(f"ERROR: MinerU polling failed: code={payload.get('code')} msg={payload.get('msg')}")

        result_list = payload.get("data", {}).get("extract_result", [])
        if result_list:
            result = result_list[0]
            state = result.get("state")
            if state == "done":
                print("  Done.", flush=True)
                return result
            if state == "failed":
                sys.exit(f"ERROR: MinerU parsing failed: {result.get('err_msg') or 'unknown error'}")
        time.sleep(interval_seconds)

    sys.exit(f"ERROR: Timed out waiting for MinerU batch result: {batch_id}")


def _download_and_extract_zip_to_temp(zip_url: str) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="mineru_zip_"))
    zip_path = temp_dir / "mineru_result.zip"
    print("  Downloading result ZIP ...", flush=True)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; finreport-slim/1.0)"}
    with requests.get(zip_url, headers=headers, timeout=300, stream=True) as resp:
        resp.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    extract_dir = temp_dir / "unzipped"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def _find_single_file(root: Path, pattern: str) -> Path | None:
    matches = sorted(root.rglob(pattern))
    return matches[0] if matches else None


def _save_artifacts(output_dir: Path, pdf_path: Path, pages: list[PageRecord]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    pages_json_path = output_dir / f"{stem}.pages.json"
    markdown_path = output_dir / f"{stem}.md"
    candidates_path = output_dir / f"{stem}.candidates.json"

    pages_json_path.write_text(
        json.dumps(
            {"source_pdf": str(pdf_path), "num_pages": len(pages), "pages": [asdict(p) for p in pages]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    markdown_chunks = [f"<!-- page: {p.page_idx} -->\n{p.markdown}\n" for p in pages]
    markdown_path.write_text("\n\n".join(markdown_chunks), encoding="utf-8")

    candidates_path.write_text(
        json.dumps({"statement_pages": [], "notes_pages": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "pages_json": str(pages_json_path),
        "markdown": str(markdown_path),
        "candidates_json": str(candidates_path),
    }


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(
    pdf_path: Path,
    output_root: Path,
    api_key: str,
    base_url: str = "https://mineru.net",
    model_version: str = "vlm",
    language: str = "ch",
    enable_formula: bool = True,
    enable_table: bool = True,
    is_ocr: bool = False,
    page_ranges: str | None = None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> None:
    output_dir = output_root / pdf_path.stem

    # Step 1: Apply for upload URL
    print(f"[1/4] Requesting upload URL for {pdf_path.name} ...", flush=True)
    apply_url = f"{base_url.rstrip('/')}/api/v4/file-urls/batch"
    apply_payload: dict[str, Any] = {
        "files": [{"name": pdf_path.name, "data_id": pdf_path.stem}],
        "model_version": model_version,
        "language": language,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
        "is_ocr": is_ocr,
    }
    if page_ranges:
        apply_payload["page_ranges"] = page_ranges

    apply_resp = requests.post(apply_url, headers=_mineru_headers(api_key), json=apply_payload, timeout=60)
    apply_resp.raise_for_status()
    apply_result = apply_resp.json()
    if apply_result.get("code") != 0:
        sys.exit(f"ERROR: MinerU apply upload failed: code={apply_result.get('code')} msg={apply_result.get('msg')}")

    batch_id = apply_result["data"]["batch_id"]
    file_urls = apply_result["data"]["file_urls"]
    if not file_urls:
        sys.exit("ERROR: MinerU did not return any upload URL.")

    # Step 2: Upload PDF
    print(f"[2/4] Uploading PDF ...", flush=True)
    with pdf_path.open("rb") as f:
        upload_resp = requests.put(file_urls[0], data=f, timeout=300)
    if upload_resp.status_code not in (200, 201):
        sys.exit(f"ERROR: MinerU upload failed: HTTP {upload_resp.status_code}")

    # Step 3: Poll for result
    print(f"[3/4] Waiting for MinerU to process ...", flush=True)
    result = _poll_mineru_batch_result(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        batch_id=batch_id,
        timeout_seconds=timeout,
        interval_seconds=poll_interval,
    )
    zip_url = result.get("full_zip_url")
    if not zip_url:
        sys.exit("ERROR: MinerU result did not include full_zip_url.")

    # Step 4: Download, parse, save
    print(f"[4/4] Extracting and saving files to {output_dir} ...", flush=True)
    extract_dir = _download_and_extract_zip_to_temp(zip_url=zip_url)
    try:
        content_list_path = _find_single_file(extract_dir, "*content_list.json")
        if content_list_path is None:
            sys.exit(f"ERROR: Could not find content_list.json in MinerU ZIP. Format may have changed.")
        content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    finally:
        import shutil
        shutil.rmtree(extract_dir.parent, ignore_errors=True)

    pages = _group_content_by_page(content_list)
    written = _save_artifacts(output_dir=output_dir, pdf_path=pdf_path, pages=pages)

    print("\nDone!")
    print(f"  Output directory : {output_dir}")
    print(f"  Markdown         : {written['markdown']}")
    print(f"  Pages JSON       : {written['pages_json']}")
    print(f"  Candidates JSON  : {written['candidates_json']}")
    print(f"  Total pages      : {len(pages)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown via the MinerU cloud API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("pdf", nargs="?", help="Path to the source PDF file")
    parser.add_argument("output_dir", nargs="?", help="Output root directory (default: same folder as PDF)")
    parser.add_argument("--mineru-env", default="MINERU_API_KEY", help="Env var name for the MinerU API key")
    parser.add_argument("--base-url", default="https://mineru.net", help="MinerU API base URL")
    parser.add_argument("--model", default="vlm", help="MinerU model version")
    parser.add_argument("--language", default="ch", help="Document language code (ch / en / ...)")
    parser.add_argument("--timeout", type=int, default=600, help="Polling timeout in seconds")
    parser.add_argument("--poll-interval", type=int, default=5, help="Polling interval in seconds")
    parser.add_argument("--page-ranges", default=None, help='Page ranges, e.g. "1-10,20-30"')
    parser.add_argument("--no-formula", action="store_true", help="Disable formula parsing")
    parser.add_argument("--no-table", action="store_true", help="Disable table parsing")
    parser.add_argument("--ocr", action="store_true", help="Enable OCR processing")
    args = parser.parse_args()

    # Interactive prompts if not provided as CLI args
    pdf_str = args.pdf
    if not pdf_str:
        pdf_str = input("PDF file path: ").strip().strip("'\"")
    if not pdf_str:
        sys.exit("ERROR: No PDF path provided.")

    pdf_path = Path(pdf_str).expanduser().resolve()
    if not pdf_path.exists():
        sys.exit(f"ERROR: File not found: {pdf_path}")
    if not pdf_path.suffix.lower() == ".pdf":
        sys.exit(f"ERROR: Not a PDF file: {pdf_path}")

    output_root_str = args.output_dir
    if not output_root_str:
        output_root_str = input(f"Output directory (press Enter to use '{pdf_path.parent}'): ").strip().strip("'\"")
    output_root = Path(output_root_str).expanduser().resolve() if output_root_str else pdf_path.parent

    api_key = _require_env(args.mineru_env)

    convert(
        pdf_path=pdf_path,
        output_root=output_root,
        api_key=api_key,
        base_url=args.base_url,
        model_version=args.model,
        language=args.language,
        enable_formula=not args.no_formula,
        enable_table=not args.no_table,
        is_ocr=args.ocr,
        page_ranges=args.page_ranges,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    main()
