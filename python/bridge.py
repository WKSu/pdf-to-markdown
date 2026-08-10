"""Adapter between convert.py and the Web Worker.

convert.py stays free of any browser concern; everything that exists only
because the caller is JavaScript lives here. Values are returned as plain
dicts, and Pyodide maps Python bytes to Uint8Array on the way out.
"""

from __future__ import annotations

from typing import Any

import pymupdf
from convert import Converter, Options, assemble, suggest_margins

_open: dict[str, Converter] = {}


def _as_bytes(data: Any) -> bytes:
    """Coerce whatever JavaScript handed us into real bytes.

    A Uint8Array arrives as a pyodide JsProxy over the buffer, which PyMuPDF
    rejects outright. Converting here keeps convert.py taking plain bytes and
    unaware that a browser exists.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    to_py = getattr(data, "to_py", None)
    if to_py is not None:
        return bytes(to_py())
    return bytes(data)


def probe(pdf_bytes: bytes) -> dict[str, Any]:
    """Cheap look at a document before committing to a full conversion.

    Lets the UI show the page count and pre-fill the margin controls with
    detected values, so the user sees what will be cropped before it happens.
    """
    doc = pymupdf.open(stream=_as_bytes(pdf_bytes), filetype="pdf")
    try:
        top, bottom = suggest_margins(doc)
        return {
            "pages": doc.page_count,
            "toc_entries": len(doc.get_toc()),
            "title": (doc.metadata or {}).get("title") or "",
            "encrypted": doc.is_encrypted,
            "margin_top": top,
            "margin_bottom": bottom,
        }
    finally:
        doc.close()


def start(doc_id: str, pdf_bytes: bytes, opts: dict[str, Any]) -> dict[str, Any]:
    """Open a document and report what the conversion decided up front."""
    known = {f.name for f in Options.__dataclass_fields__.values()}
    options = Options(**{k: v for k, v in (opts or {}).items() if k in known})
    conv = Converter(
        _as_bytes(pdf_bytes), opts.get("filename", "document.pdf"), options
    )
    _open[doc_id] = conv
    return {
        "pages": conv.page_count,
        "slug": conv.slug,
        "title": conv.title,
        "toc_entries": len(conv.toc),
        "front_matter": conv.front_matter(),
        "bold_coverage": conv.bold_coverage,
        "emphasis_stripped": conv.strip_emphasis,
    }


def page(doc_id: str, number: int) -> dict[str, Any]:
    result = _open[doc_id].page(number)
    return {
        "number": result.number,
        "markdown": result.markdown,
        "warnings": list(result.warnings),
        "chars": result.chars,
        "tables": result.tables,
        "heading_repairs": result.heading_repairs,
        "dropped_tables": result.dropped_tables,
        "text_recovered": result.text_recovered,
        "preview": result.preview,
        "figures": [{"path": f["path"], "bytes": f["bytes"]} for f in result.figures],
    }


def finish(doc_id: str) -> None:
    conv = _open.pop(doc_id, None)
    if conv is not None:
        conv.close()


def join_pages(front_matter: str, pages: list[dict[str, Any]], separators: bool) -> str:
    """Reassemble edited pages into one document.

    Takes the Markdown back from the UI rather than from the converter, so a
    reviewer's corrections are what lands in the download.
    """
    from convert import PageResult

    results = [
        PageResult(number=p["number"], markdown=p["markdown"] or "") for p in pages
    ]
    return assemble(front_matter, results, separators)
