"""Word (.docx) to Markdown.

A .docx is a zip of XML that already says what everything is: a heading is a
paragraph with a heading style, a list item carries a numbering reference, a
table is a <w:tbl>. So unlike the PDF path there is no layout to reverse
engineer -- no reading order, no fake tables, no running headers to crop
(headers and footers live in separate parts and are simply never read).

Standard library only, on purpose: it runs unchanged under Pyodide, adds
nothing to the 32 MB download, and has no version pins to keep in sync.

PyMuPDF 1.27 can open .docx too, but tested on a simple document it drops
list markers, table structure, bold/italic and images, so it is not used here.

A Word document has no fixed pages, so for page-by-page review it is split
into sections at its top-level headings (see sections()). office.py adapts
that to the interface the UI and the CLI use for PDF pages.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


def q(tag: str) -> str:
    """'w:p' -> '{namespace}p', for comparing against element tags."""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


W_VAL = q("w:val")
R_ID = q("r:id")
R_EMBED = q("r:embed")

# Word's built-in style names are English in the XML regardless of UI
# language; Dutch Word shows "Kop 1" but stores name="heading 1".
HEADING_NAME = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)

LIST_STYLE_DEPTH = re.compile(
    r"^list (?:bullet|number|paragraph)? ?(\d)$", re.IGNORECASE
)

MD_SPECIAL = re.compile(r"([\\`*_\[\]])")

# Plain text that happens to start like Markdown block syntax ("2024. Een
# jaar", "- en verder", "#hashtag ...") would otherwise turn into a list or a
# heading.
BLOCK_START = re.compile(r"^(#|[-+>]\s|\d+[.)]\s)")

HEADING_MD = re.compile(r"^(#{1,6}) (.*)$")
FOOTNOTE_REF = re.compile(r"(?<!\\)\[\^(\d+)\](?!:)")
FIGURE_REF = re.compile(r"\]\((figures/[^)]+)\)")


@dataclass
class Section:
    """One reviewable unit: everything up to the next top-level heading."""

    markdown: str
    title: str = ""
    warnings: list[str] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)


def _on(el: ET.Element | None) -> bool:
    """A toggle property like <w:b/> is on unless w:val says otherwise."""
    if el is None:
        return False
    return el.get(W_VAL, "true").lower() not in ("0", "false", "off", "none")


class DocxConverter:
    def __init__(self, data: bytes, filename: str) -> None:
        import io

        self.filename = filename
        self.slug = (
            re.sub(r"[^a-z0-9]+", "-", PurePosixPath(filename).stem.lower()).strip("-")
            or "document"
        )
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.warnings: list[str] = []
        self.figures: dict[str, bytes] = {}
        self.rels = self._rels("word/_rels/document.xml.rels")
        self.styles = self._styles()
        self.numbering = self._numbering()
        self.footnotes = self._footnotes()
        self.used_footnotes: list[str] = []
        self._list_counters: dict[tuple[str, int], int] = {}

    # --- package parts -------------------------------------------------------

    def _xml(self, name: str) -> ET.Element | None:
        try:
            return ET.fromstring(self.zip.read(name))
        except KeyError:
            return None

    def _rels(self, name: str) -> dict[str, str]:
        root = self._xml(name)
        if root is None:
            return {}
        return {
            r.get("Id"): r.get("Target", "") for r in root.iter(q("rel:Relationship"))
        }

    def _styles(self) -> dict[str, dict]:
        """styleId -> {'heading': level or None, 'based_on': id}."""
        root = self._xml("word/styles.xml")
        styles: dict[str, dict] = {}
        if root is None:
            return styles
        for s in root.iter(q("w:style")):
            sid = s.get(q("w:styleId"))
            name_el = s.find("w:name", NS)
            name = name_el.get(W_VAL, "") if name_el is not None else ""
            level = None
            m = HEADING_NAME.match(name)
            if m:
                level = int(m.group(1))
            elif name.lower() == "title":
                level = 1
            outline = s.find("w:pPr/w:outlineLvl", NS)
            if level is None and outline is not None:
                level = int(outline.get(W_VAL, "9")) + 1
            based = s.find("w:basedOn", NS)
            styles[sid] = {
                "name": name,
                "num_pr": s.find("w:pPr/w:numPr", NS),
                "heading": level if level and level <= 6 else None,
                "based_on": based.get(W_VAL) if based is not None else None,
                "title": name.lower() == "title",
            }
        return styles

    def _heading_level(self, style_id: str | None) -> int | None:
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            style = self.styles.get(style_id)
            if not style:
                return None
            if style["heading"]:
                return style["heading"]
            style_id = style["based_on"]
        return None

    def _style_num_pr(self, style_id: str | None) -> ET.Element | None:
        """List styles carry their numbering on the style, not the paragraph."""
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            style = self.styles.get(style_id)
            if not style:
                return None
            if style["num_pr"] is not None:
                return style["num_pr"]
            style_id = style["based_on"]
        return None

    def _numbering(self) -> dict[str, dict[int, str]]:
        """numId -> {ilvl: numFmt}, e.g. 'bullet' or 'decimal'."""
        root = self._xml("word/numbering.xml")
        if root is None:
            return {}
        abstract: dict[str, dict[int, str]] = {}
        for a in root.iter(q("w:abstractNum")):
            levels = {}
            for lvl in a.iter(q("w:lvl")):
                fmt = lvl.find("w:numFmt", NS)
                levels[int(lvl.get(q("w:ilvl"), "0"))] = (
                    fmt.get(W_VAL, "bullet") if fmt is not None else "bullet"
                )
            abstract[a.get(q("w:abstractNumId"))] = levels
        result = {}
        for num in root.iter(q("w:num")):
            ref = num.find("w:abstractNumId", NS)
            if ref is not None:
                result[num.get(q("w:numId"))] = abstract.get(ref.get(W_VAL), {})
        return result

    def _footnotes(self) -> dict[str, ET.Element]:
        root = self._xml("word/footnotes.xml")
        if root is None:
            return {}
        return {fn.get(q("w:id")): fn for fn in root.iter(q("w:footnote"))}

    # --- inline content ------------------------------------------------------

    def _image(self, rel_id: str, alt: str) -> str:
        target = self.rels.get(rel_id)
        if not target or target.startswith(("http:", "https:")):
            return ""
        part = posixpath.normpath(posixpath.join("word", target))
        try:
            data = self.zip.read(part)
        except KeyError:
            return ""
        ext = PurePosixPath(part).suffix.lower()
        if ext in (".emf", ".wmf"):
            # Vector formats no browser renders; keep the file, flag it.
            self.warnings.append(
                f"figuur in {ext}-formaat, niet zichtbaar in de meeste viewers"
            )
        path = f"figures/{self.slug}-img{len(self.figures) + 1}{ext}"
        self.figures[path] = data
        return f"\n\n![{alt}]({path})\n\n"

    def _segments(self, parent: ET.Element) -> list[tuple[str, str]]:
        """(text, emphasis marker) pairs for everything inside a paragraph."""
        out: list[tuple[str, str]] = []
        for el in parent:
            tag = el.tag
            if tag == q("w:r"):
                out.extend(self._run(el))
            elif tag == q("w:hyperlink"):
                text = self._render(self._segments(el))
                target = self.rels.get(el.get(R_ID, ""), "")
                out.append(
                    (f"[{text}]({target})" if target and text.strip() else text, "")
                )
            elif tag in (
                q("w:ins"),
                q("w:smartTag"),
                q("w:fldSimple"),
                q("w:customXml"),
            ):
                # Tracked insertions count as text; tracked deletions (w:del)
                # hold w:delText, which _run never reads, so they drop out.
                out.extend(self._segments(el))
            elif tag == q("w:sdt"):
                content = el.find("w:sdtContent", NS)
                if content is not None:
                    out.extend(self._segments(content))
        return out

    @staticmethod
    def _render(segments: list[tuple[str, str]]) -> str:
        """Join segments, merging neighbours with the same emphasis.

        Word splits text into runs for reasons invisible to the reader (spell
        check, revision ids), so "**a****b**" has to become "**ab**".
        """
        merged: list[list[str]] = []
        for text, fmt in segments:
            if not text:
                continue
            if merged and merged[-1][1] == fmt:
                merged[-1][0] += text
            else:
                merged.append([text, fmt])
        out = []
        for text, fmt in merged:
            if fmt and text.strip():
                # Markers must hug the text or Markdown ignores them.
                lead = text[: len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()) :]
                text = f"{lead}{fmt}{text.strip()}{fmt}{trail}"
            out.append(text)
        return "".join(out)

    def _runs(self, parent: ET.Element) -> str:
        return self._render(self._segments(parent))

    def _run(self, r: ET.Element) -> list[tuple[str, str]]:
        parts: list[str] = []
        for el in r:
            if el.tag == q("w:t"):
                parts.append(MD_SPECIAL.sub(r"\\\1", el.text or ""))
            elif el.tag == q("w:tab"):
                parts.append(" ")
            elif el.tag in (q("w:br"), q("w:cr")):
                parts.append("  \n")
            elif el.tag == q("w:footnoteReference"):
                fid = el.get(q("w:id"))
                if fid not in self.used_footnotes:
                    self.used_footnotes.append(fid)
                parts.append(f"[^{self.used_footnotes.index(fid) + 1}]")
            elif el.tag == q("w:drawing"):
                blip = el.find(".//a:blip", NS)
                doc_pr = el.find(".//wp:docPr", NS)
                alt = (
                    (doc_pr.get("descr") or doc_pr.get("title") or "")
                    if doc_pr is not None
                    else ""
                )
                if blip is not None:
                    parts.append(
                        self._image(blip.get(R_EMBED, ""), alt.replace("\n", " "))
                    )
                elif el.find(".//{*}chart") is not None:
                    self.warnings.append("grafiek (Word-chart) niet omgezet")
                    parts.append("\n\n_[Grafiek niet omgezet]_\n\n")
            elif el.tag == q("w:pict"):
                img = el.find(".//v:imagedata", NS)
                if img is not None:
                    parts.append(self._image(img.get(R_ID, ""), ""))
        props = r.find("w:rPr", NS)
        bold = props is not None and _on(props.find("w:b", NS))
        italic = props is not None and _on(props.find("w:i", NS))
        fmt = "***" if bold and italic else "**" if bold else "*" if italic else ""
        return [
            (part, "" if part.startswith(("\n", "  \n", "[^")) else fmt)
            for part in parts
        ]

    # --- blocks --------------------------------------------------------------

    def _paragraph(self, p: ET.Element) -> str:
        text = self._runs(p).strip()
        if not text:
            return ""
        ppr = p.find("w:pPr", NS)
        style_el = ppr.find("w:pStyle", NS) if ppr is not None else None
        style_id = style_el.get(W_VAL) if style_el is not None else None

        level = self._heading_level(style_id)
        if level:
            # Emphasis inside a heading is Word styling, not meaning.
            return "#" * level + " " + text.replace("**", "").strip()

        num = ppr.find("w:numPr", NS) if ppr is not None else None
        if num is None:
            num = self._style_num_pr(style_id)
        if num is not None:
            num_id_el = num.find("w:numId", NS)
            ilvl_el = num.find("w:ilvl", NS)
            num_id = num_id_el.get(W_VAL) if num_id_el is not None else "0"
            ilvl = int(ilvl_el.get(W_VAL, "0")) if ilvl_el is not None else 0
            # Word's "List Bullet 2" styles nest by indentation, not by ilvl.
            depth = LIST_STYLE_DEPTH.match(
                self.styles.get(style_id, {}).get("name", "")
            )
            nesting = int(depth.group(1)) - 1 if depth and ilvl == 0 else ilvl
            if num_id != "0":
                fmt = self.numbering.get(num_id, {}).get(ilvl, "bullet")
                indent = "   " * nesting
                if fmt in ("bullet", "none"):
                    return f"{indent}- {text}"
                key = (num_id, ilvl)
                self._list_counters[key] = self._list_counters.get(key, 0) + 1
                # Deeper levels restart when a shallower item appears.
                for k in list(self._list_counters):
                    if k[0] == num_id and k[1] > ilvl:
                        del self._list_counters[k]
                return f"{indent}{self._list_counters[key]}. {text}"
        if BLOCK_START.match(text):
            # "2024. Een jaar" needs the dot escaped, not the digits.
            text = re.sub(r"^(\d+)([.)])", r"\1\\\2", text)
            if not text[0].isdigit():
                text = "\\" + text
        return text

    def _table(self, tbl: ET.Element) -> str:
        rows: list[list[str]] = []
        for tr in tbl.findall("w:tr", NS):
            cells = []
            for tc in tr.findall("w:tc", NS):
                paras = [self._paragraph(p) for p in tc.iter(q("w:p"))]
                # A table row must stay on one line, so hard breaks become <br>.
                cell = "<br>".join(t for t in paras if t).replace("  \n", "<br>")
                cell = cell.replace("\n", " ").strip()
                cell = cell.replace("|", "\\|")
                span = tc.find("w:tcPr/w:gridSpan", NS)
                cells.append(cell)
                # Merged cells: pad so the columns still line up.
                cells.extend(
                    [""] * (int(span.get(W_VAL, "1")) - 1 if span is not None else 0)
                )
            rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        if width == 1:
            # A one-cell table is a text box used for layout, not data.
            return "\n\n".join(r[0].replace("<br>", "\n\n") for r in rows if r[0])
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    def _blocks(self, body: ET.Element) -> list[tuple[str, list[str]]]:
        """(markdown, warnings raised while converting it) per block."""
        blocks: list[tuple[str, list[str]]] = []
        for el in body:
            before = len(self.warnings)
            if el.tag == q("w:p"):
                blocks.append((self._paragraph(el), self.warnings[before:]))
            elif el.tag == q("w:tbl"):
                blocks.append((self._table(el), self.warnings[before:]))
            elif el.tag == q("w:sdt"):
                # A generated table of contents is page numbers and dot leaders,
                # and the headings it lists are in the document anyway.
                gallery = el.find("w:sdtPr/w:docPartObj/w:docPartGallery", NS)
                if gallery is not None and "Table of Contents" in gallery.get(
                    W_VAL, ""
                ):
                    continue
                content = el.find("w:sdtContent", NS)
                if content is not None:
                    blocks.extend(self._blocks(content))
        return blocks

    def _join(self, blocks: list[str]) -> str:
        """Blank line between blocks, except inside a run of list items."""
        out: list[str] = []
        is_item = re.compile(r"^\s*(-|\d+\.) ")

        def kind(block: str) -> str | None:
            m = is_item.match(block)
            return None if m is None else "-" if m.group(1) == "-" else "1."

        previous = None
        for b in blocks:
            if not b:
                continue
            # Same list continues tight; a bullet list straight after a
            # numbered one is a new list and needs a blank line to say so.
            same_list = (
                previous is not None
                and kind(b) is not None
                and kind(previous) is not None
                and (kind(b) == kind(previous) or b[0] == " " or previous[0] == " ")
            )
            previous = b
            if out and same_list:
                out.append("\n" + b)
            elif out:
                out.append("\n\n" + b)
            else:
                out.append(b)
        return "".join(out)

    # --- document ------------------------------------------------------------

    def _footnote(self, number: int) -> str:
        fn = self.footnotes.get(self.used_footnotes[number - 1])
        text = (
            " ".join(self._paragraph(p) for p in fn.iter(q("w:p")))
            if fn is not None
            else ""
        )
        return f"[^{number}]: {text.strip()}"

    def sections(self) -> list[Section]:
        """The document split at its top-level headings.

        Top-level means the shallowest heading level the document actually
        uses, so a document that starts at Kop 2 still splits into chapters.
        Text before the first heading is a section of its own. Footnote
        definitions go with the section that first refers to them, so a
        reviewer sees a note next to the text it belongs to.
        """
        root = self._xml("word/document.xml")
        if root is None:
            raise ValueError("geen word/document.xml: dit is geen .docx")
        blocks = [(md, w) for md, w in self._blocks(root.find("w:body", NS)) if md]
        levels = [len(m.group(1)) for md, _ in blocks if (m := HEADING_MD.match(md))]
        top = min(levels, default=None)

        groups: list[list[tuple[str, list[str]]]] = [[]]
        for md, warnings in blocks:
            m = HEADING_MD.match(md)
            if m and len(m.group(1)) == top and groups[-1]:
                groups.append([])
            groups[-1].append((md, warnings))

        sections = []
        defined: set[int] = set()
        for group in groups:
            markdown = self._join([md for md, _ in group])
            refs = [int(n) for n in FOOTNOTE_REF.findall(markdown)]
            new = [n for n in dict.fromkeys(refs) if n not in defined]
            defined.update(new)
            if new:
                markdown += "\n\n" + "\n".join(self._footnote(n) for n in new)
            first = HEADING_MD.match(group[0][0]) if group else None
            sections.append(
                Section(
                    markdown=markdown,
                    title=first.group(2) if first else "",
                    warnings=sorted({w for _, ws in group for w in ws}),
                    figures=FIGURE_REF.findall(markdown),
                )
            )
        return sections
