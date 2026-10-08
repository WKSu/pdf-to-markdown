"""Word and PowerPoint behind the same interface as the PDF Converter.

bridge.py and cli.py drive a conversion page by page: open, ask how many
pages, convert page n, reassemble. OfficeConverter answers those questions for
a .docx (a "page" is a chapter, split at the top-level headings) and a .pptx
(a "page" is a slide), so the worker, the review screen, the export and the
CLI need no format-specific paths beyond choosing which class to construct.

Like convert.py, nothing here knows a browser exists.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from convert import Options, PageResult
from docx_convert import DocxConverter
from pptx_convert import PptxConverter

# What a reviewable unit is called, per format. Used for the separator comment
# in the joined Markdown and, on the JavaScript side, for labels.
UNITS = {"pdf": "page", "docx": "section", "pptx": "slide"}

# Office's own alt text and captions are kept, but an image line is any
# Markdown image pointing into figures/.
IMAGE_MD = re.compile(r"^!\[(?:\\.|[^\]\\])*\]\(figures/[^)]+\)\s*$", re.MULTILINE)
TABLE_RULE = re.compile(r"^\|(?:-{3,}\|)+\s*$", re.MULTILINE)

CORE_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}

# Compound File Binary: legacy .doc/.ppt/.xls, and also any password-protected
# .docx/.pptx, which Office wraps in the same container.
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_format(data: bytes, filename: str = "") -> str:
    """'pdf', 'docx' or 'pptx', from the bytes rather than the file name."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:8] == OLE_MAGIC:
        raise ValueError(
            "Oud Office-formaat (.doc/.ppt) of een bestand met wachtwoord. "
            "Open het in Word of PowerPoint en sla het op als .docx of .pptx "
            "zonder wachtwoord."
        )
    if data[:2] == b"PK":
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            names = set()
        if "word/document.xml" in names:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
    # Some PDF writers put junk before the header; MuPDF copes with that.
    if filename.lower().endswith(".pdf"):
        return "pdf"
    raise ValueError(f"{filename or 'Dit bestand'} is geen PDF, .docx of .pptx.")


def _esc(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


class OfficeConverter:
    """A .docx or .pptx, converted one unit at a time on request."""

    def __init__(
        self, data: bytes, filename: str, opts: Options | None = None, kind: str = ""
    ) -> None:
        self.opts = opts or Options()
        self.filename = filename
        self.kind = kind or detect_format(data, filename)
        self.unit = UNITS[self.kind]
        if self.kind == "docx":
            self._conv = DocxConverter(data, filename)
            # Word needs one pass over the whole document anyway (list numbering
            # and footnote numbers run through it), and it is fast: plain XML.
            self._sections = self._conv.sections()
            self._count = len(self._sections)
        elif self.kind == "pptx":
            self._conv = PptxConverter(data, filename, notes=self.opts.speaker_notes)
            self._count = len(self._conv.slide_parts)
        else:
            raise ValueError(f"geen Office-document: {kind}")
        self.slug = self._conv.slug
        self._core = self._core_properties()

    # --- what bridge.py and cli.py ask of a converter ------------------------

    @property
    def page_count(self) -> int:
        return self._count

    @property
    def title(self) -> str:
        return self._core.get("title") or self.slug.replace("-", " ")

    @property
    def toc(self) -> list:
        """Top-level headings, the nearest thing to a PDF's bookmarks."""
        if self.kind == "docx":
            return [s.title for s in self._sections if s.title]
        return []

    # Emphasis detection exists for PDFs whose font name says "bold" for all
    # text. Word and PowerPoint mark emphasis explicitly, so it is meaningful.
    bold_coverage = 0.0
    strip_emphasis = False

    @property
    def _figures(self) -> dict[str, bytes]:
        if self.opts.ignore_images:
            return {}
        return self._conv.figures

    def front_matter(self) -> str:
        lines = [
            "---",
            f"title: {_esc(self.title)}",
            f"source_file: {_esc(self.filename)}",
            f"{self.unit}s: {self.page_count}",
        ]
        for key in ("author", "created", "modified"):
            if self._core.get(key):
                lines.append(f"{key}: {_esc(self._core[key])}")
        lines.append(f"extracted_with: {_esc(f'{self.kind}_convert (stdlib)')}")
        if self.kind == "pptx":
            lines.append(f"speaker_notes: {str(self.opts.speaker_notes).lower()}")
        lines.append("---")
        return "\n".join(lines)

    def page(self, number: int) -> PageResult:
        if self.kind == "docx":
            unit = self._sections[number - 1]
        else:
            unit = self._conv.slide(number)
        markdown, paths = unit.markdown, unit.figures
        if self.opts.ignore_images:
            markdown = re.sub(r"\n{3,}", "\n\n", IMAGE_MD.sub("", markdown)).strip()
            paths = []
        figures = self._conv.figures
        return PageResult(
            number=number,
            markdown=markdown,
            warnings=list(unit.warnings),
            figures=[{"path": p, "bytes": figures[p]} for p in paths if p in figures],
            # No preview: MuPDF renders neither format faithfully (see the
            # module docstrings), and a wrong picture of the original is worse
            # for a reviewer than none.
            preview=None,
            chars=len(markdown),
            tables=len(TABLE_RULE.findall(markdown)),
        )

    def close(self) -> None:
        self._conv.zip.close()

    # --- internals -----------------------------------------------------------

    def _core_properties(self) -> dict[str, str]:
        try:
            root = ET.fromstring(self._conv.zip.read("docProps/core.xml"))
        except (KeyError, ET.ParseError):
            return {}
        found = {}
        for key, path in (
            ("title", "dc:title"),
            ("author", "dc:creator"),
            ("created", "dcterms:created"),
            ("modified", "dcterms:modified"),
        ):
            el = root.find(path, CORE_NS)
            if el is not None and (el.text or "").strip():
                found[key] = el.text.strip()
        return found
