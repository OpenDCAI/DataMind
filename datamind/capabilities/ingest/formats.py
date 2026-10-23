"""Format adapters used by workspace builds.

Adapters return provider-neutral text blocks so KB, Graph, and future export
layers can share one extraction result. Optional parsers fail with a clear
message; the original file remains available for lineage and raw fallback.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


DOCUMENT_EXTS = {
    ".txt", ".md", ".markdown", ".html", ".json", ".xml", ".py", ".java",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
}
TABLE_EXTS = {".csv", ".tsv", ".xls", ".xlsx"}


def worksheet_rows(sheet):
    """Read actual XML extent, retaining columns beyond a short title row.

    A first streaming pass finds the real width instead of trusting an inflated
    or undersized dimension. The second pads every row to that width, matching
    the existing table import semantics without altering the original bytes.
    """
    sheet.reset_dimensions()
    width = max((len(row) for row in sheet.iter_rows()), default=0)
    return sheet.iter_rows(max_col=width, values_only=True) if width else iter(())


@dataclass(frozen=True)
class ExtractedDocument:
    source: str
    format: str
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _plain_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")




def _json_request(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
                  body: bytes | None = None, timeout: float = 30.0) -> dict[str, Any]:
    from urllib.request import Request, urlopen
    headers = {"Accept": "application/json"}
    data = body
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"MinerU API request failed: {exc}") from exc


def _download_bytes(url: str, *, timeout: float = 30.0) -> bytes:
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request(url, headers={"Accept": "text/markdown,application/zip,*/*"}), timeout=timeout) as response:
            return response.read()
    except OSError as exc:
        raise RuntimeError(f"MinerU result download failed: {exc}") from exc


def _upload_bytes(url: str, body: bytes, *, timeout: float = 120.0) -> None:
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request(url, data=body, method="PUT"), timeout=timeout) as response:
            response.read()
    except OSError as exc:
        raise RuntimeError(f"MinerU upload failed: {exc}") from exc


def _find_result_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and key.lower() in {
                "markdown_url", "md_url", "result_url", "download_url", "full_zip_url",
            } and item.startswith(("http://", "https://")):
                return item
            found = _find_result_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_result_url(item)
            if found:
                return found
    return None


def _mineru_api_text(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Upload a local file to MinerU's hosted Agent API and poll Markdown output."""
    import time
    base = os.getenv("DATAMIND_MINERU_API_URL", "https://mineru.net/api/v1/agent").rstrip("/")
    timeout = float(os.getenv("DATAMIND_MINERU_API_TIMEOUT_S", "180"))
    options: dict[str, Any] = {"file_name": path.name, "language": os.getenv("DATAMIND_MINERU_LANGUAGE", "ch"),
                               "enable_table": True, "enable_formula": True,
                               "is_ocr": os.getenv("DATAMIND_MINERU_OCR", "false").lower() == "true"}
    created = _json_request(f"{base}/parse/file", method="POST", payload=options, timeout=30)
    if created.get("code", 0) != 0:
        raise RuntimeError(str(created.get("msg", "MinerU API submission failed")))
    data = created.get("data") or {}
    task_id, upload_url = data.get("task_id"), data.get("file_url")
    if not task_id or not upload_url:
        raise RuntimeError("MinerU API did not return task_id/file_url")
    _upload_bytes(upload_url, path.read_bytes(), timeout=120)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _json_request(f"{base}/parse/{task_id}", timeout=30)
        status_data = status.get("data") or {}
        state = status_data.get("state")
        if state == "done":
            result_url = _find_result_url(status_data)
            if not result_url:
                raise RuntimeError("MinerU completed without a Markdown result URL")
            payload = _download_bytes(result_url, timeout=60)
            if payload[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    names = sorted(name for name in archive.namelist() if name.lower().endswith(".md"))
                    text = "\n\n".join(archive.read(name).decode("utf-8", errors="replace") for name in names).strip()
            else:
                text = payload.decode("utf-8", errors="replace").strip()
            if not text:
                raise RuntimeError("MinerU API returned empty Markdown")
            return text, [{"type": "markdown", "source": "mineru-api", "task_id": task_id, "text": text}]
        if state == "failed":
            raise RuntimeError(str(status_data.get("err_msg") or status.get("msg") or "MinerU task failed"))
        time.sleep(2)
    raise RuntimeError(f"MinerU API timed out after {timeout:g}s")


def _mineru_text(path: Path) -> tuple[str, list[dict[str, Any]]]:
    # Kept as a small compatibility wrapper for tests and downstream adapters.
    return _mineru_api_text(path)

def _pdf_text(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires the 'pypdf' package") from exc
    reader = PdfReader(str(path))
    blocks: list[dict[str, Any]] = []
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            blocks.append({"type": "page", "page": number, "text": text})
    return "\n\n".join(f"<!-- page {b['page']} -->\n{b['text']}" for b in blocks), blocks


def _docx_text(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        import docx
    except ImportError as exc:
        raise RuntimeError("DOCX extraction requires the 'python-docx' package") from exc
    document = docx.Document(str(path))
    blocks: list[dict[str, Any]] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            blocks.append({"type": "paragraph", "text": text})
    for table_index, table in enumerate(document.tables, 1):
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        text = "\n".join(" | ".join(row) for row in rows if any(row)).strip()
        if text:
            blocks.append({"type": "table", "table": table_index, "text": text})
    return "\n\n".join(block["text"] for block in blocks), blocks


def _pptx_text(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError("PPTX extraction requires the 'python-pptx' package") from exc
    presentation = Presentation(str(path))
    blocks: list[dict[str, Any]] = []
    for number, slide in enumerate(presentation.slides, 1):
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
        text = "\n".join(texts).strip()
        if text:
            blocks.append({"type": "slide", "slide": number, "text": text})
    return "\n\n".join(f"<!-- slide {b['slide']} -->\n{b['text']}" for b in blocks), blocks


def _legacy_office_to_pdf(path: Path) -> Path:
    output_dir = Path(tempfile.mkdtemp(prefix="datamind-convert-"))
    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(path)],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"legacy Office extraction requires LibreOffice: {path.suffix}"
        ) from exc
    converted = output_dir / f"{path.stem}.pdf"
    if not converted.is_file():
        raise RuntimeError(f"LibreOffice did not produce a PDF for {path.name}")
    return converted


def extract_document(path: str | Path) -> ExtractedDocument:
    """Extract a document while retaining source and block provenance."""
    source = Path(path).expanduser().resolve()
    suffix = source.suffix.lower()
    warnings: list[str] = []
    if suffix in {".txt", ".md", ".markdown", ".py", ".java", ".json", ".xml"}:
        text = _plain_text(source)
        if suffix == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                warnings.append("invalid JSON; preserved as plain text")
        return ExtractedDocument(str(source), suffix.lstrip("."), text, [{"type": "text", "text": text}], warnings)
    if suffix == ".html":
        parser = _HTMLTextParser()
        parser.feed(_plain_text(source))
        text = "\n".join(parser.parts)
        return ExtractedDocument(str(source), "html", text, [{"type": "text", "text": text}], warnings)
    if suffix == ".pdf":
        # MinerU preserves layout, tables, formulas, and OCR when available.
        # It is accessed only through the hosted API; local installs are not required.
        mineru_mode = os.getenv("DATAMIND_MINERU", "auto").lower()
        if mineru_mode not in {"off", "pypdf"}:
            try:
                text, blocks = _mineru_text(source)
                warnings.append("parsed with MinerU API")
                return ExtractedDocument(str(source), "pdf", text, blocks, warnings)
            except RuntimeError as exc:
                warnings.append(f"MinerU API unavailable; fell back to pypdf: {exc}")
        text, blocks = _pdf_text(source)
        warnings.append("parsed with pypdf")
        return ExtractedDocument(str(source), "pdf", text, blocks, warnings)
    if suffix == ".docx":
        text, blocks = _docx_text(source)
        return ExtractedDocument(str(source), "docx", text, blocks, warnings)
    if suffix == ".pptx":
        text, blocks = _pptx_text(source)
        return ExtractedDocument(str(source), "pptx", text, blocks, warnings)
    if suffix in {".doc", ".ppt"}:
        converted = _legacy_office_to_pdf(source)
        text, blocks = _pdf_text(converted)
        warnings.append("legacy Office file converted to PDF via LibreOffice")
        return ExtractedDocument(str(source), suffix.lstrip("."), text, blocks, warnings)
    raise RuntimeError(f"no document extractor for '{suffix or '<none>'}'")


def extract_tabular(path: str | Path) -> list[tuple[str, list[str], list[dict[str, Any]]]]:
    """Return ``(table_name, columns, rows)`` for CSV/TSV/XLSX/XLS."""
    source = Path(path).expanduser().resolve()
    suffix = source.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with source.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            values = list(csv.reader(handle, delimiter=delimiter))
        if not values:
            return []
        columns = [cell.strip() or f"col_{index}" for index, cell in enumerate(values[0], 1)]
        rows = [dict(zip(columns, row + [""] * (len(columns) - len(row)))) for row in values[1:]]
        return [(source.stem, columns, rows)]
    if suffix in {".xlsx", ".xls"}:
        try:
            import openpyxl
        except ImportError as exc:
            raise RuntimeError("Excel import requires the 'openpyxl' package") from exc
        if suffix == ".xls":
            raise RuntimeError("legacy .xls import requires conversion to .xlsx")
        workbook = openpyxl.load_workbook(str(source), read_only=True, data_only=True)
        output: list[tuple[str, list[str], list[dict[str, Any]]]] = []
        try:
            for sheet in workbook.worksheets:
                values = list(worksheet_rows(sheet))
                if not values:
                    continue
                columns = [str(cell).strip() if cell is not None and str(cell).strip() else f"col_{index}"
                           for index, cell in enumerate(values[0], 1)]
                rows = [dict(zip(columns, list(row) + [None] * (len(columns) - len(row)))) for row in values[1:]]
                output.append((sheet.title, columns, rows))
        finally:
            workbook.close()
        return output
    raise RuntimeError(f"no table extractor for '{suffix or '<none>'}'")


__all__ = ["DOCUMENT_EXTS", "TABLE_EXTS", "ExtractedDocument", "extract_document", "extract_tabular"]
