"""PDF to Markdown conversion core.

Deliberately free of browser APIs: the exact same file runs under Pyodide in a
Web Worker and under `uv run` on the desktop (see cli.py). That is what makes
the conversion logic testable without a browser in the loop.

Pinned to pymupdf 1.27.2.2 / pymupdf4llm 0.3.4 because that is the PyMuPDF
build Pyodide 314 ships for ABI 2026_0. Keep the desktop pins in cli.py in
sync, or the browser and the CLI will silently diverge.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pymupdf
import pymupdf4llm
from pymupdf4llm.helpers.pymupdf_rag import IdentifyHeaders, TocHeaders

# A figure smaller than this fraction of the page is decoration (rules, logos,
# bullet glyphs), not content worth extracting.
IMAGE_SIZE_LIMIT = 0.05

# Text spans inside a figure shorter than this are noise (axis ticks, stray
# glyphs from a logo) rather than usable labels.
MIN_LABEL_CHARS = 3

# A map legend is useful; a full page of transcribed labels is not.
MAX_FIGURE_LABELS = 40

# A page with fewer characters than this, but with ink on it, is probably a
# scan or an all-graphics spread.
SPARSE_TEXT_CHARS = 60

# When a page yields less than this share of the text PyMuPDF can see on it, the
# vector-graphics handling has swallowed content and the page is retried. Only
# applied to pages with at least RETRY_MIN_CHARS of text, so that genuinely
# near-empty pages are not retried pointlessly.
# Set generously rather than tightly: the retry result is only kept when it
# actually yields more text, so a needless retry costs one extra pass and
# nothing else. At 0.5 pages sitting at 52-55% were missed while recovering to
# well over 100%.
RETRY_RETENTION = 0.75
RETRY_MIN_CHARS = 200

IMAGE_LINE = re.compile(r"^!\[\]\((?P<path>[^)]+)\)\s*$", re.MULTILINE)

HEADING_LINE = re.compile(r"^(#{1,6})[ \t]*(.+?)[ \t]*$", re.MULTILINE)

# C0 controls except tab and newline, plus the Unicode replacement character.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f�]")

# Emphasis that covers most of a document is not emphasis. Above this share of
# characters, the markers are dropped as noise.
EMPHASIS_NOISE_RATIO = 0.6

# Pages sampled when measuring emphasis coverage and guessing margins.
SAMPLE_PAGES = 12


@dataclass(frozen=True)
class Options:
    """Conversion settings. Mirrored by the option controls in the UI."""

    # Figure rendering. JPEG at 110 dpi is roughly a tenth the size of PNG and
    # plenty for feeding a model; the review pane is where you go to read a map
    # in detail. Note PNG size is NOT monotonic in dpi here -- on these InDesign
    # exports 150 dpi apparently matches the embedded image resolution, so PNG
    # at 150 is smaller than at 110 or 130, where resampling noise defeats the
    # compressor. If you switch to PNG, use 150, not something "lower".
    dpi: int = 110
    image_format: str = "jpg"
    preview_dpi: int = 96
    margin_top: float = 0.0
    margin_bottom: float = 0.0
    write_images: bool = True
    ignore_images: bool = False
    ignore_graphics: bool = False
    force_text: bool = True
    table_strategy: str = "lines_strict"
    figure_legends: bool = True
    page_separators: bool = True
    heading_mode: str = "hybrid"  # hybrid | toc | fontsize
    emphasis: str = "auto"  # auto | keep | strip
    previews: bool = True
    # PowerPoint only (see office.py); here so one Options travels everywhere.
    speaker_notes: bool = True


@dataclass
class PageResult:
    number: int  # 1-based
    markdown: str
    warnings: list[str] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    preview: bytes | None = None
    chars: int = 0
    tables: int = 0
    heading_repairs: int = 0
    dropped_tables: int = 0
    text_recovered: bool = False


class HybridHeaders:
    """Heading detector: trust the bookmark tree, fall back to font size.

    pymupdf4llm ships two detectors and neither is enough on its own for
    designed policy documents. IdentifyHeaders keys off font size, so a 24pt
    pull quote becomes an <h1>. TocHeaders uses the real bookmark hierarchy --
    accurate where it applies, but it returns "" for every span that is not a
    TOC entry, which drops sub-headings the font pass would have caught.

    So: ask the TOC first, and only fall back to font size. Where a document
    has no TOC at all this degrades to plain IdentifyHeaders.

    The fallback is also capped at FALLBACK_MAX_LEVEL. IdentifyHeaders assigns
    its deepest levels to the font sizes just above body text, which in a
    designed document means captions, callouts and lead paragraphs -- not
    headings. Letting those through produces a hierarchy where the same word is
    an <h1> on one page and an <h5> on another. Dropping them costs the odd real
    sub-heading and removes far more false ones; the review UI is there for the
    remainder.
    """

    FALLBACK_MAX_LEVEL = 3

    def __init__(self, doc: pymupdf.Document, mode: str = "hybrid") -> None:
        self.mode = mode
        self.toc = (
            TocHeaders(doc) if mode in ("hybrid", "toc") and doc.get_toc() else None
        )
        self.font = IdentifyHeaders(doc) if mode in ("hybrid", "fontsize") else None

    def get_header_id(self, span: dict, page: pymupdf.Page | None = None) -> str:
        if self.toc is not None:
            hit = self.toc.get_header_id(span, page=page)
            if hit:
                return hit
            if self.mode == "toc":
                return ""
        if self.font is None:
            return ""
        hit = self.font.get_header_id(span, page=page)
        # Only cap when the TOC was available to be preferred; in pure fontsize
        # mode the caller asked for the raw heuristic.
        if self.toc is not None and hit.count("#") > self.FALLBACK_MAX_LEVEL:
            return ""
        return hit


def slugify(name: str) -> str:
    """Filesystem- and URL-safe stem for output filenames."""
    stem = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^\w\s-]", "", stem).strip().lower()
    stem = re.sub(r"[\s_-]+", "-", stem)
    return stem or "document"


def emphasis_coverage(doc: pymupdf.Document, sample: int = SAMPLE_PAGES) -> float:
    """Share of characters whose font is flagged bold.

    PyMuPDF infers "bold" from the font name, which misfires badly on corporate
    typefaces. Rotterdam's own documents are set in a family called "Bolder",
    so 99.9% of the text comes out bold and every ** in the Markdown is noise.
    Measuring the share lets us tell real emphasis from a naming coincidence.
    """
    count = doc.page_count
    step = max(1, count // sample)
    indexes = list(range(0, count, step))[:sample]

    total = bold = 0
    for index in indexes:
        blocks = doc[index].get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
        for block in blocks:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    chars = len(span["text"].strip())
                    if not chars:
                        continue
                    total += chars
                    if span["flags"] & 2**4:  # bold bit
                        bold += chars
    return bold / total if total else 0.0


def clean_markup(markdown: str, strip_emphasis: bool) -> str:
    """Tidy pymupdf4llm's inline markup.

    Three fixes, all of which show up on every page of a designed document:
    adjacent bold runs get merged (a heading split across two spans arrives as
    `**1. Waarom een** **omgevingsvisie?**`), headings lose their redundant
    inner emphasis, and -- when emphasis turned out to be meaningless -- the
    bold markers go entirely.
    """
    # `**a** **b**` -> `**a b**`, repeatedly for runs of three or more spans
    for _ in range(3):
        merged = re.sub(r"\*\*(\s+)\*\*", r"\1", markdown)
        if merged == markdown:
            break
        markdown = merged

    if strip_emphasis:
        markdown = markdown.replace("**", "")

    # Broken glyph mappings occasionally yield C0 control characters -- a
    # typographic quote coming out as BEL, for instance. They are invisible in
    # an editor and corrupt the text for anything reading it downstream.
    markdown = CONTROL_CHARS.sub("", markdown)

    # A heading is already emphatic; inner markers only add noise.
    def tidy_heading(match: re.Match[str]) -> str:
        text = match.group(2).replace("**", "").strip()
        text = re.sub(r"\s+", " ", text)
        return f"{match.group(1)} {text}" if text else ""

    return HEADING_LINE.sub(tidy_heading, markdown)


TABLE_BLOCK = re.compile(
    r"(?:^\|.*\|[ \t]*\n)(?:^\|[-: |]+\|[ \t]*\n)(?:^\|.*\|[ \t]*\n?)*", re.MULTILINE
)


def _is_degenerate_table(block: str) -> bool:
    """Is this "table" actually an infographic that tripped table detection?

    Circular diagrams and callout clusters in designed documents are built from
    ruled vector shapes, which is exactly what `lines_strict` looks for. The
    result is a grid of placeholder headers and the same words repeated in every
    cell -- strictly worse than no table at all, because a model reads it as
    structured data. Three tells, any of which condemns the block:
    autogenerated ColN headers, mostly-empty cells, or near-identical rows.
    """
    rows = [r for r in block.strip().split("\n") if r.strip().startswith("|")]
    if len(rows) < 2:
        return False
    body = [r for r in rows if not re.fullmatch(r"\|[-: |]+\|", r.strip())]
    if not body:
        return False

    if re.search(r"\|Col\d+\|", rows[0]):
        return True

    cells = [c.strip() for row in body for c in row.strip("|").split("|")]
    if not cells:
        return False
    filled = [c for c in cells if c]
    if len(filled) / len(cells) < 0.35:
        return True

    # The same long value repeated across cells means the detector sliced one
    # block of text into a grid rather than finding real columns.
    long_filled = [c for c in filled if len(c) > 20]
    return len(long_filled) >= 3 and len(set(long_filled)) <= len(long_filled) / 2


def drop_degenerate_tables(markdown: str) -> tuple[str, int]:
    """Replace infographic-mistaken-for-table blocks with their plain text."""
    dropped = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal dropped
        block = match.group(0)
        if not _is_degenerate_table(block):
            return block
        dropped += 1
        # Keep the words, lose the false structure.
        seen: set[str] = set()
        kept: list[str] = []
        for row in block.strip().split("\n"):
            if re.fullmatch(r"\|[-: |]+\|", row.strip()):
                continue
            for cell in row.strip("|").split("|"):
                for part in cell.replace("<br>", "\n").split("\n"):
                    part = part.strip()
                    if (
                        not part
                        or part.lower() in seen
                        or re.fullmatch(r"Col\d+", part)
                    ):
                        continue
                    seen.add(part.lower())
                    kept.append(part)
        return (" ".join(kept) + "\n") if kept else ""

    return TABLE_BLOCK.sub(replace, markdown), dropped


def _key(text: str) -> str:
    """Alphanumeric-only lowercase form, for forgiving text comparisons."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def repair_split_headings(
    markdown: str, toc_entries: list[tuple[int, str]]
) -> tuple[str, int]:
    """Rejoin display headings that the layout broke across lines.

    A spread title set on two lines arrives as two separate blocks, so
    "Rotterdam als wereldhavenstad." becomes the heading "Rotterdam als"
    followed by the orphan paragraph "wereldhavenstad.". The bookmark tree has
    the full title, so where a heading is a strict prefix of a TOC entry the
    following lines are absorbed until the entry is accounted for.

    Returns the repaired Markdown and the number of headings repaired.
    """
    if not toc_entries:
        return markdown, 0

    lines = markdown.split("\n")
    repairs = 0

    for level, title in toc_entries:
        title_key = _key(title)
        if not title_key:
            continue
        for i, line in enumerate(lines):
            match = re.match(r"^(#{1,6})[ \t]+(.+)$", line)
            if not match:
                continue
            acc = _key(match.group(2))
            if not acc or acc == title_key or not title_key.startswith(acc):
                continue

            absorbed: list[int] = []
            j = i + 1
            while j < len(lines) and acc != title_key:
                candidate = lines[j].strip()
                if not candidate:
                    absorbed.append(j)
                    j += 1
                    continue
                if candidate.startswith(("#", "![", "|", ">")):
                    break
                merged = acc + _key(candidate)
                if not title_key.startswith(merged):
                    break
                acc = merged
                absorbed.append(j)
                j += 1

            if acc != title_key:
                continue
            lines[i] = f"{'#' * level} {title}"
            for index in absorbed:
                lines[index] = None  # type: ignore[call-overload]
            repairs += 1
            break

    if not repairs:
        return markdown, 0
    return "\n".join(line for line in lines if line is not None), repairs


def _figure_rects(chunk: dict) -> list[pymupdf.Rect]:
    """Bounding boxes of the figures pymupdf4llm decided were content.

    Images carry a bbox; vector graphics clusters carry a plain 4-tuple.
    """
    rects: list[pymupdf.Rect] = []
    for img in chunk.get("images", []):
        bbox = img.get("bbox") if isinstance(img, dict) else img
        if bbox is not None:
            rects.append(pymupdf.Rect(bbox))
    for gfx in chunk.get("graphics", []):
        bbox = gfx.get("bbox") if isinstance(gfx, dict) else gfx
        if bbox is not None:
            rects.append(pymupdf.Rect(bbox))
    return rects


def _labels_inside(page: pymupdf.Page, rect: pymupdf.Rect) -> list[str]:
    """Vector text drawn inside a figure, in reading order.

    Maps and infographics in these documents carry their place names, legends
    and year ranges as real text. Crop the figure to a PNG and that text is
    gone -- an AI model sees an opaque image link with no idea what it depicts.

    force_text=True means these words also appear in the page's running text,
    so this list is partly redundant. That is deliberate: what the page text
    cannot express is *association*, and a model reading "Rozenburg; Hoek van
    Holland; legenda: ..." directly under a figure knows they describe that
    map. Mild duplication is a cheaper problem than lost association.
    """
    seen: set[str] = set()
    labels: list[str] = []
    blocks = page.get_text("dict", clip=rect, flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            text = " ".join(s["text"] for s in line.get("spans", [])).strip()
            text = re.sub(r"\s+", " ", text)
            key = text.lower()
            if len(text) < MIN_LABEL_CHARS or key in seen:
                continue
            if not re.search(r"[A-Za-z]{2}|\d", text):
                continue  # punctuation or single stray letters
            seen.add(key)
            labels.append(text)
            if len(labels) >= MAX_FIGURE_LABELS:
                return labels
    return labels


def _annotate_figures(markdown: str, page: pymupdf.Page, chunk: dict) -> str:
    """Append a label list under each figure reference in the page Markdown."""
    rects = _figure_rects(chunk)
    if not rects:
        return markdown

    matches = list(IMAGE_LINE.finditer(markdown))
    if not matches:
        return markdown

    out: list[str] = []
    cursor = 0
    for i, match in enumerate(matches):
        out.append(markdown[cursor : match.end()])
        cursor = match.end()
        if i < len(rects):
            labels = _labels_inside(page, rects[i])
            if labels:
                out.append("\n\n_Labels in figuur:_ " + "; ".join(labels))
    out.append(markdown[cursor:])
    return "".join(out)


class Converter:
    """Holds an open document so pages can be converted and streamed one by one.

    Page-at-a-time rather than whole-document so the UI can show page 1 while
    page 200 is still running. The heading detector is still built once over
    the whole document -- it needs global font statistics to be meaningful.
    """

    def __init__(
        self, pdf_bytes: bytes, filename: str, opts: Options | None = None
    ) -> None:
        self.opts = opts or Options()
        self.filename = filename
        self.slug = slugify(filename)
        self.doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        self.toc = self.doc.get_toc()
        self.headers = HybridHeaders(self.doc, self.opts.heading_mode)
        self._figures: dict[str, bytes] = {}
        # pymupdf4llm writes figure files relative to the working directory, so a
        # relative image_path would litter it. Callers get the bytes handed to
        # them and choose where things live; this is only a staging area.
        self._image_dir = tempfile.mkdtemp(prefix="pdfmd-")

        self.bold_coverage = emphasis_coverage(self.doc)
        if self.opts.emphasis == "auto":
            self.strip_emphasis = self.bold_coverage > EMPHASIS_NOISE_RATIO
        else:
            self.strip_emphasis = self.opts.emphasis == "strip"

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def title(self) -> str:
        meta = (self.doc.metadata or {}).get("title") or ""
        return meta.strip() or self.slug.replace("-", " ")

    def front_matter(self) -> str:
        """YAML front matter, so downstream tooling knows what it is reading."""
        meta = self.doc.metadata or {}

        def esc(value: str) -> str:
            return '"' + str(value).replace('"', '\\"') + '"'

        lines = [
            "---",
            f"title: {esc(self.title)}",
            f"source_file: {esc(self.filename)}",
            f"pages: {self.page_count}",
        ]
        for key in ("author", "creationDate", "creator", "producer"):
            if meta.get(key):
                lines.append(f"{key.lower()}: {esc(meta[key])}")
        lines += [
            f"toc_entries: {len(self.toc)}",
            f"extracted_with: {esc(f'pymupdf {pymupdf.version[0]} / pymupdf4llm {pymupdf4llm.__version__}')}",
            f"heading_mode: {esc(self.opts.heading_mode)}",
            f"bold_coverage: {self.bold_coverage:.3f}",
            f"emphasis_stripped: {str(self.strip_emphasis).lower()}",
            "---",
        ]
        return "\n".join(lines)

    def _margins(self) -> tuple[float, float, float, float]:
        # pymupdf4llm 0.3.4 has no header/footer switches, so running headers
        # and page numbers have to be cropped away geometrically.
        return (0.0, self.opts.margin_top, 0.0, self.opts.margin_bottom)

    def _to_markdown(self, index: int, ignore_graphics: bool) -> dict:
        chunks = pymupdf4llm.to_markdown(
            self.doc,
            pages=[index],
            hdr_info=self.headers,
            page_chunks=True,
            write_images=self.opts.write_images and not self.opts.ignore_images,
            ignore_images=self.opts.ignore_images,
            ignore_graphics=ignore_graphics,
            image_path=self._image_dir,
            image_format=self.opts.image_format,
            image_size_limit=IMAGE_SIZE_LIMIT,
            filename=self.slug,
            dpi=self.opts.dpi,
            force_text=self.opts.force_text,
            table_strategy=self.opts.table_strategy,
            margins=self._margins(),
            show_progress=False,
        )
        if not chunks:
            return {"text": "", "tables": [], "images": [], "graphics": []}
        return chunks[0]

    def page(self, number: int) -> PageResult:
        """Convert one 1-based page number."""
        index = number - 1
        page = self.doc[index]
        opts = self.opts

        chunk = self._to_markdown(index, opts.ignore_graphics)

        # Vector-graphics handling silently eats text: an infographic page whose
        # five callout boxes are drawn as vector shapes comes back with the boxes
        # as pictures and their contents -- real policy text -- gone. Across this
        # document that costs about 8,000 characters on six pages, which matters
        # far more than the eight tables the same setting finds.
        #
        # So rather than choosing, measure. If a page returns much less text than
        # PyMuPDF can see on it, redo just that page with vector graphics ignored.
        # Costs one extra pass on roughly 3% of pages and loses only table
        # *formatting* there, since the cell text still comes through.
        retried = False
        visible = len(page.get_text().strip())
        if (
            not opts.ignore_graphics
            and visible >= RETRY_MIN_CHARS
            and len(chunk.get("text", "").strip()) < visible * RETRY_RETENTION
        ):
            recovered = self._to_markdown(index, ignore_graphics=True)
            if len(recovered.get("text", "").strip()) > len(
                chunk.get("text", "").strip()
            ):
                chunk = recovered
                retried = True

        markdown = chunk.get("text", "")
        markdown = clean_markup(markdown, self.strip_emphasis)
        markdown, repaired = repair_split_headings(
            markdown,
            [(lvl, t) for lvl, t, pg in self.toc if pg == number and t.strip()],
        )
        markdown, bad_tables = drop_degenerate_tables(markdown)
        # Figures are read off disk under the names pymupdf4llm chose, then both
        # the files and the links are renamed to 1-based pages, so a reviewer
        # looking at "page 12" finds p012 rather than having to subtract one.
        figures = self._collect_figures(markdown, number)
        markdown = self._rename_figure_links(markdown, number)

        # After renaming, so the legend text is not itself rewritten.
        if opts.figure_legends and not opts.ignore_images:
            markdown = _annotate_figures(markdown, page, chunk)

        result = PageResult(
            number=number,
            markdown=markdown.strip(),
            chars=len(markdown.strip()),
            tables=max(0, len(chunk.get("tables", [])) - bad_tables),
            heading_repairs=repaired,
            dropped_tables=bad_tables,
            text_recovered=retried,
        )
        result.figures = figures
        result.warnings = self._warnings(page, chunk, result)
        if opts.previews:
            result.preview = page.get_pixmap(dpi=opts.preview_dpi).tobytes("png")
        return result

    def _figure_name(self, path: str, number: int) -> str:
        """Turn a staged file path into `figures/<slug>-p<page>-<n>.<ext>`.

        Also converts pymupdf4llm's 0-based page index to the 1-based number a
        reviewer sees, so "page 12" maps to p012 without arithmetic.
        """
        stem = re.split(r"[/\\]", path)[-1]
        prefix = f"{self.slug}-{number - 1}-"
        if not stem.startswith(prefix):
            return path
        return f"figures/{self.slug}-p{number:03d}-{stem[len(prefix) :]}"

    def _rename_figure_links(self, markdown: str, number: int) -> str:
        return IMAGE_LINE.sub(
            lambda m: f"![]({self._figure_name(m.group('path'), number)})", markdown
        )

    def _collect_figures(self, markdown: str, number: int) -> list[dict[str, Any]]:
        """Read back the PNGs pymupdf4llm just wrote, under their final names.

        write_images writes to the (in-memory, under Pyodide) filesystem and
        references the files from the Markdown. Handing the bytes straight to
        the caller keeps the ZIP layout identical to the Markdown links.
        """
        figures: list[dict[str, Any]] = []
        for match in IMAGE_LINE.finditer(markdown):
            source = match.group("path")
            path = self._figure_name(source, number)
            if path in self._figures:
                continue
            try:
                with open(source, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            self._figures[path] = data
            figures.append({"path": path, "bytes": data})
        return figures

    def _warnings(
        self, page: pymupdf.Page, chunk: dict, result: PageResult
    ) -> list[str]:
        warnings: list[str] = []
        raw = page.get_text().strip()
        has_ink = bool(page.get_images() or page.get_drawings())
        # Dutch, because these strings are shown verbatim in the review pane and
        # the readers are Rotterdam colleagues. The CLI prints the same text.
        if not raw and has_ink:
            # PyMuPDF's own OCR needs a Tesseract binary that does not exist in
            # the WASM build, so flagging the page is all we can do here.
            warnings.append("geen tekstlaag - waarschijnlijk een scan, niets opgehaald")
        elif len(raw) < SPARSE_TEXT_CHARS and has_ink:
            warnings.append(
                f"heel weinig tekst ({len(raw)} tekens) op een pagina met beeld"
            )
        if result.tables:
            warnings.append(
                f"{result.tables} tabel(len) gevonden - controleer de kolommen"
            )
        if result.dropped_tables:
            warnings.append(
                f"{result.dropped_tables} valse tabel(len) uit vectorvormen verwijderd "
                "- kijk naar het figuur zelf"
            )
        if result.text_recovered:
            warnings.append(
                "tekst werd opgeslokt door vectorvormen; pagina opnieuw gedaan "
                "zonder die vormen - een tabel hier staat als platte tekst"
            )
        return warnings

    def close(self) -> None:
        self.doc.close()
        shutil.rmtree(self._image_dir, ignore_errors=True)


def assemble(
    front_matter: str,
    pages: list[PageResult],
    separators: bool = True,
    unit: str = "page",
) -> str:
    """Join per-page Markdown into the final document.

    `unit` names what a page is -- "slide" for a deck, "section" for a Word
    document -- so a separator never claims a page number that does not exist.
    """
    parts = [front_matter]
    for page in pages:
        if separators:
            parts.append(f"<!-- {unit} {page.number} -->")
        if page.markdown:
            parts.append(page.markdown)
    return "\n\n".join(parts).rstrip() + "\n"


def suggest_margins(
    doc: pymupdf.Document, sample: int = SAMPLE_PAGES
) -> tuple[float, float]:
    """Guess top/bottom margins by finding lines repeated across pages.

    Municipal documents put the same running header and page footer on every
    page; repeated across a sample of pages, they are easy to spot, and their
    y-extent is the margin to crop.
    """
    count = doc.page_count
    if count < 3:
        return (0.0, 0.0)
    step = max(1, count // sample)
    indexes = list(range(0, count, step))[:sample]

    seen: Counter[str] = Counter()
    where: dict[str, list[tuple[float, float, float]]] = {}
    for index in indexes:
        page = doc[index]
        height = page.rect.height
        for block in page.get_text("blocks"):
            y0, y1, text = block[1], block[3], block[4]
            text = re.sub(r"\s+", " ", text).strip()
            # digits vary page to page; normalise so "| 12" and "| 13" match
            key = re.sub(r"\d+", "#", text)
            if not key or len(key) > 120:
                continue
            if y0 > height * 0.15 and y1 < height * 0.85:
                continue  # only interested in the top and bottom strips
            seen[key] += 1
            where.setdefault(key, []).append((y0, y1, height))

    threshold = max(2, int(len(indexes) * 0.6))
    top = bottom = 0.0
    for key, hits in seen.items():
        if hits < threshold:
            continue
        for y0, y1, height in where[key]:
            if y1 < height * 0.15:
                top = max(top, y1 + 2)
            elif y0 > height * 0.85:
                bottom = max(bottom, height - y0 + 2)
    return (round(top, 1), round(bottom, 1))


__all__ = [
    "Converter",
    "HybridHeaders",
    "Options",
    "PageResult",
    "assemble",
    "clean_markup",
    "drop_degenerate_tables",
    "emphasis_coverage",
    "repair_split_headings",
    "slugify",
    "suggest_margins",
]
