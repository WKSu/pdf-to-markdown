"""PowerPoint (.pptx) to Markdown -- spike, not yet wired into the UI.

Same approach as docx_convert.py: a .pptx is a zip of XML that names what
everything is, so the slide title, bullets, tables and chart data are read
directly instead of reconstructed from a rendering. Standard library only, so
it runs unchanged under Pyodide and adds nothing to the download.

PyMuPDF 1.27 can open .pptx, but reflows the deck into portrait pages: on a
14-slide test deck it produced 6 pages with no slide boundaries, flattened the
table into one cell per line, dropped the chart and the speaker notes, and read
a right-hand column before the left one. It is not used here.

What a slide deck needs that a Word document does not:

- Reading order. Shapes are stored in z-order (what was drawn last), not in
  reading order, so they are sorted by position on the slide.
- Charts. The values a chart shows are cached in the chart XML, so they come
  out as a Markdown table rather than as a picture a model cannot read.
- Speaker notes. Often the actual argument of a deck lives there.

    python python/pptx_convert.py some.pptx -o out/
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


def q(tag: str) -> str:
    """'p:sp' -> '{namespace}sp', for comparing against element tags."""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


R_ID = q("r:id")
R_EMBED = q("r:embed")

TITLE_TYPES = {"title", "ctrTitle"}

# Footer, date and slide number repeat on every slide: the deck's equivalent
# of the running headers the PDF path crops away.
SKIPPED_PLACEHOLDERS = {"sldNum", "ftr", "dt", "hdr"}

# Placeholders that take the master's body text style, which is where a deck's
# default bullets come from. Titles and plain text boxes have none by default.
BODY_PLACEHOLDERS = {"body", "obj"}

# Shapes whose tops are within this share of the slide height count as one row,
# so two columns that start at slightly different heights still read left to
# right instead of whichever is a few pixels higher first.
ROW_TOLERANCE = 0.05

MD_SPECIAL = re.compile(r"([\\`*_\[\]])")


def _bullet(ppr: ET.Element) -> tuple[str | None, bool]:
    """(marker, whether this element decides it) from a paragraph's properties."""
    if ppr.find("a:buNone", NS) is not None:
        return None, True
    if ppr.find("a:buAutoNum", NS) is not None:
        return "1.", True
    if ppr.find("a:buChar", NS) is not None or ppr.find("a:buBlip", NS) is not None:
        return "-", True
    return None, False


# Office writes this after alt text it generated itself; it says nothing about
# the image.
GENERATED_ALT = re.compile(
    r"\s*(Automatisch gegenereerde beschrijving|Description automatically generated)\.?\s*$",
    re.IGNORECASE,
)


@dataclass
class SlideResult:
    number: int
    markdown: str
    title: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class PptxResult:
    slides: list[SlideResult]
    front_matter: str
    figures: dict[str, bytes] = field(default_factory=dict)

    @property
    def markdown(self) -> str:
        return "\n\n".join(s.markdown for s in self.slides) + "\n"


class PptxConverter:
    def __init__(self, data: bytes, filename: str, notes: bool = True) -> None:
        self.filename = filename
        self.notes = notes
        self.slug = (
            re.sub(r"[^a-z0-9]+", "-", PurePosixPath(filename).stem.lower()).strip("-")
            or "presentatie"
        )
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.figures: dict[str, bytes] = {}
        self._ph_cache: dict[str | None, dict[str, ET.Element]] = {}
        pres = self._xml("ppt/presentation.xml")
        if pres is None:
            raise ValueError("geen ppt/presentation.xml: dit is geen .pptx")
        size = pres.find("p:sldSz", NS)
        self.slide_height = (
            int(size.get("cy", "6858000")) if size is not None else 6858000
        )
        rels = self._rels("ppt/presentation.xml")
        # Slide order is the order of sldIdLst, not the numbers in the file
        # names: slide7.xml can be the second slide after reordering.
        self.slide_parts = [
            rels[s.get(R_ID)]["target"]
            for s in pres.iterfind("p:sldIdLst/p:sldId", NS)
            if s.get(R_ID) in rels
        ]

    # --- package parts -------------------------------------------------------

    def _xml(self, name: str) -> ET.Element | None:
        try:
            return ET.fromstring(self.zip.read(name))
        except KeyError:
            return None

    def _rels(self, part: str) -> dict[str, dict]:
        """Relationship id -> resolved target, for one part of the package."""
        folder, name = posixpath.split(part)
        root = self._xml(posixpath.join(folder, "_rels", name + ".rels"))
        if root is None:
            return {}
        rels = {}
        for r in root.iter(q("rel:Relationship")):
            target = r.get("Target", "")
            external = r.get("TargetMode") == "External"
            if not external:
                target = posixpath.normpath(posixpath.join(folder, target))
            rels[r.get("Id")] = {
                "target": target,
                "type": r.get("Type", "").rsplit("/", 1)[-1],
                "external": external,
            }
        return rels

    def _related(self, rels: dict[str, dict], kind: str) -> str | None:
        for rel in rels.values():
            if rel["type"] == kind:
                return rel["target"]
        return None

    # --- layout inheritance --------------------------------------------------

    # A placeholder on a slide is mostly empty of formatting: where it sits and
    # whether its paragraphs carry bullets is decided by the matching
    # placeholder on the layout, then on the master. Without following that
    # chain a deck in the Rotterdam template reads as a nine-deep list, because
    # its master uses paragraph levels as text styles (intro, quote, caption)
    # and only levels 3-4 and 6-7 as actual bullets and numbering.

    def _placeholders(self, part: str | None) -> dict[str, ET.Element]:
        """Placeholder shapes on a layout or master, keyed by idx and by type."""
        if part in self._ph_cache:
            return self._ph_cache[part]
        root = self._xml(part) if part else None
        found: dict[str, ET.Element] = {}
        for sp in root.iter(q("p:sp")) if root is not None else ():
            ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
            if ph is None:
                continue
            if ph.get("idx"):
                found.setdefault(f"idx:{ph.get('idx')}", sp)
            found.setdefault(f"type:{ph.get('type', 'body')}", sp)
        self._ph_cache[part] = found
        return found

    @staticmethod
    def _ph_keys(ph: ET.Element) -> list[str]:
        keys = [f"idx:{ph.get('idx')}"] if ph.get("idx") else []
        return keys + [f"type:{ph.get('type', 'body')}"]

    def _inherited(self, ph: ET.Element, ctx: dict) -> list[ET.Element]:
        """Matching placeholders on the layout, then the master."""
        chain = []
        for source in (ctx["layout"], ctx["master"]):
            for key in self._ph_keys(ph):
                if key in source:
                    chain.append(source[key])
                    break
        return chain

    def _level_styles(
        self, styles: list[ET.Element]
    ) -> dict[int, tuple[str | None, int | None]]:
        """Paragraph level -> (list marker, left margin), first definition wins.

        `styles` are list-style elements ordered from most to least specific.
        """
        resolved: dict[int, tuple[str | None, int | None]] = {}
        for level in range(9):
            marker: str | None = None
            margin: int | None = None
            marker_known = False
            for style in styles:
                lvl = style.find(f"a:lvl{level + 1}pPr", NS)
                if lvl is None:
                    continue
                if margin is None and lvl.get("marL") is not None:
                    margin = int(lvl.get("marL"))
                if not marker_known:
                    marker, marker_known = _bullet(lvl)
            resolved[level] = (marker, margin)
        return resolved

    # --- inline content ------------------------------------------------------

    def _paragraph_text(self, p: ET.Element, rels: dict[str, dict]) -> str:
        segments: list[tuple[str, str]] = []
        for el in p:
            if el.tag in (q("a:r"), q("a:fld")):
                t = el.find("a:t", NS)
                text = MD_SPECIAL.sub(r"\\\1", t.text or "") if t is not None else ""
                props = el.find("a:rPr", NS)
                bold = props is not None and props.get("b") == "1"
                italic = props is not None and props.get("i") == "1"
                fmt = (
                    "***"
                    if bold and italic
                    else "**"
                    if bold
                    else "*"
                    if italic
                    else ""
                )
                link = props.find("a:hlinkClick", NS) if props is not None else None
                target = rels.get(link.get(R_ID, ""), {}) if link is not None else {}
                if target.get("external") and text.strip():
                    # Spaces belong outside the brackets, or the link swallows them.
                    lead = text[: len(text) - len(text.lstrip())]
                    trail = text[len(text.rstrip()) :]
                    text = f"{lead}[{text.strip()}]({target['target']}){trail}"
                    fmt = ""
                segments.append((text, fmt))
            elif el.tag == q("a:br"):
                segments.append(("  \n", ""))
        return self._render(segments).strip()

    @staticmethod
    def _render(segments: list[tuple[str, str]]) -> str:
        """Join runs, merging neighbours with the same emphasis (see docx)."""
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
                lead = text[: len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()) :]
                text = f"{lead}{fmt}{text.strip()}{fmt}{trail}"
            out.append(text)
        return "".join(out)

    def _text_body(
        self, body: ET.Element, rels: dict[str, dict], styles: list[ET.Element]
    ) -> str:
        levels = self._level_styles(styles)
        items: list[tuple[str, int, str | None, int]] = []
        for p in body.findall("a:p", NS):
            text = self._paragraph_text(p, rels)
            if not text:
                continue
            ppr = p.find("a:pPr", NS)
            level = int(ppr.get("lvl", "0")) if ppr is not None else 0
            marker, margin = levels.get(level, (None, None))
            if ppr is not None:
                own, known = _bullet(ppr)
                if known:
                    marker = own
                if ppr.get("marL") is not None:
                    margin = int(ppr.get("marL"))
            items.append((text, level, marker, margin if margin is not None else level))
        # A body placeholder holding a single bullet is a text block, not a
        # one-item list, even though the master gives it a bullet.
        if len(items) == 1:
            items = [(items[0][0], 0, None, 0)]

        # Nesting follows indentation among the list items of this text body,
        # not the raw level number: in a template that uses levels as styles,
        # level 3 can be the outermost bullet.
        # Depth is counted within each run of consecutive list items, so a
        # numbered list after some body text starts at the margin again.
        run_margins: list[list[int]] = []
        for i, (_, _, marker, margin) in enumerate(items):
            if marker is None:
                continue
            if i == 0 or items[i - 1][2] is None:
                run_margins.append([])
            run_margins[-1].append(margin)
        runs = iter(sorted(set(m)) for m in run_margins)

        lines: list[str] = []
        counters: dict[int, int] = {}
        margins: list[int] = []
        for i, (text, level, marker, margin) in enumerate(items):
            if marker is None:
                if lines:
                    lines.append("")
                lines.append(text)
                counters.clear()
                continue
            if i == 0 or items[i - 1][2] is None:
                margins = next(runs)
            depth = margins.index(margin)
            if marker == "1.":
                counters[depth] = counters.get(depth, 0) + 1
                for deeper in [k for k in counters if k > depth]:
                    del counters[deeper]
                marker = f"{counters[depth]}."
            if lines and not re.match(r"^\s*(-|\d+\.) ", lines[-1]):
                lines.append("")
            lines.append("   " * depth + f"{marker} {text}")
        return "\n".join(lines)

    # --- shapes --------------------------------------------------------------

    def _image(self, rel_id: str, rels: dict[str, dict], alt: str, slide: int) -> str:
        rel = rels.get(rel_id)
        if not rel or rel["external"]:
            return ""
        try:
            data = self.zip.read(rel["target"])
        except KeyError:
            return ""
        ext = PurePosixPath(rel["target"]).suffix.lower()
        path = f"figures/{self.slug}-dia{slide}-img{len(self.figures) + 1}{ext}"
        self.figures[path] = data
        alt = MD_SPECIAL.sub(r"\\\1", alt)
        return f"![{alt}]({path})"

    def _table(self, tbl: ET.Element, rels: dict[str, dict]) -> str:
        rows = []
        for tr in tbl.findall("a:tr", NS):
            cells = []
            for tc in tr.findall("a:tc", NS):
                # Cells swallowed by a merge are still present in the XML.
                if tc.get("hMerge") == "1" or tc.get("vMerge") == "1":
                    cells.append("")
                    continue
                body = tc.find("a:txBody", NS)
                paras = (
                    [self._paragraph_text(p, rels) for p in body.findall("a:p", NS)]
                    if body is not None
                    else []
                )
                cells.append(
                    "<br>".join(t for t in paras if t)
                    .replace("  \n", "<br>")
                    .replace("|", "\\|")
                )
            rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    def _chart(self, part: str) -> str:
        """The chart's cached values as a table: categories down, series across."""
        root = self._xml(part)
        if root is None:
            return ""

        def points(el: ET.Element | None) -> dict[int, str]:
            if el is None:
                return {}
            return {
                int(pt.get("idx", 0)): (pt.findtext("c:v", "", NS) or "")
                for pt in el.iter(q("c:pt"))
            }

        title = " ".join(
            t.text or "" for t in root.iterfind("c:chart/c:title//a:t", NS)
        ).strip()
        categories: dict[int, str] = {}
        series: list[tuple[str, dict[int, str]]] = []
        for ser in root.iter(q("c:ser")):
            name = (
                " ".join(points(ser.find("c:tx", NS)).values())
                or f"Reeks {len(series) + 1}"
            )
            categories = (
                categories
                or points(ser.find("c:cat", NS))
                or points(ser.find("c:xVal", NS))
            )
            series.append(
                (name, points(ser.find("c:val", NS)) or points(ser.find("c:yVal", NS)))
            )
        if not series:
            return "_[Grafiek zonder gegevens]_"
        indices = sorted(set(categories) | {i for _, vals in series for i in vals})
        header = ["", *(name for name, _ in series)]
        lines = [
            f"_Grafiek{': ' + title if title else ''}_",
            "",
            "| " + " | ".join(header) + " |",
            "|" + "---|" * len(header),
        ]
        for i in indices:
            row = [
                categories.get(i, str(i + 1)),
                *(vals.get(i, "") for _, vals in series),
            ]
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    def _smartart(self, part: str) -> str:
        """SmartArt keeps its text in a data part; the diagram itself is lost."""
        root = self._xml(part)
        if root is None:
            return ""
        texts = []
        for pt in root.iter(q("dgm:pt")):
            text = " ".join(t.text or "" for t in pt.iter(q("a:t"))).strip()
            if text:
                texts.append("- " + MD_SPECIAL.sub(r"\\\1", text))
        return "\n".join(texts)

    def _position(self, el: ET.Element, ctx: dict) -> tuple[int, int]:
        ph = el.find("*/p:nvPr/p:ph", NS)
        candidates = [el] + (self._inherited(ph, ctx) if ph is not None else [])
        for shape in candidates:
            off = shape.find("*/a:xfrm/a:off", NS)
            if off is None:
                off = shape.find("p:xfrm/a:off", NS)  # graphicFrame
            if off is not None:
                return int(off.get("y", 0)), int(off.get("x", 0))
        return (10**12, 0)  # unknown: after everything that has a position

    def _shapes(
        self,
        tree: ET.Element,
        rels: dict[str, dict],
        ctx: dict,
        slide: int,
        warnings: list[str],
    ) -> list[tuple[tuple[int, int], str, str | None]]:
        """(position, markdown, placeholder type) for each shape, unsorted."""
        found = []
        for el in tree:
            pos = self._position(el, ctx)
            if el.tag == q("p:sp"):
                ph = el.find("p:nvSpPr/p:nvPr/p:ph", NS)
                ph_type = ph.get("type", "body") if ph is not None else None
                if ph_type in SKIPPED_PLACEHOLDERS:
                    continue
                body = el.find("p:txBody", NS)
                if body is None:
                    continue
                styles = [body.find("a:lstStyle", NS)]
                if ph is not None:
                    styles += [
                        sp.find("p:txBody/a:lstStyle", NS)
                        for sp in self._inherited(ph, ctx)
                    ]
                    if ph_type in BODY_PLACEHOLDERS:
                        styles.append(ctx["body_style"])
                styles = [s for s in styles if s is not None]
                text = self._text_body(body, rels, styles)
                if text:
                    found.append((pos, text, ph_type))
            elif el.tag == q("p:grpSp"):
                # A group is read as one unit at its own position, so its parts
                # stay together; inside, its children are sorted the same way.
                children = self._shapes(el, rels, ctx, slide, warnings)
                children.sort(key=lambda s: self._order_key(s[0]))
                text = "\n\n".join(md for _, md, _ in children)
                if text:
                    found.append((pos, text, None))
            elif el.tag == q("p:pic"):
                blip = el.find("p:blipFill/a:blip", NS)
                pr = el.find("p:nvPicPr/p:cNvPr", NS)
                alt = (pr.get("descr") or "") if pr is not None else ""
                alt = GENERATED_ALT.sub("", alt)
                if blip is not None:
                    md = self._image(
                        blip.get(R_EMBED, ""), rels, alt.replace("\n", " "), slide
                    )
                    if md:
                        found.append((pos, md, None))
            elif el.tag == q("p:graphicFrame"):
                data = el.find("a:graphic/a:graphicData", NS)
                if data is None:
                    continue
                tbl = data.find("a:tbl", NS)
                chart = data.find("c:chart", NS)
                diagram = data.find("dgm:relIds", NS)
                if tbl is not None:
                    found.append((pos, self._table(tbl, rels), None))
                elif chart is not None and chart.get(R_ID) in rels:
                    found.append(
                        (pos, self._chart(rels[chart.get(R_ID)]["target"]), None)
                    )
                elif diagram is not None and diagram.get(q("r:dm")) in rels:
                    warnings.append("SmartArt: alleen de tekst is overgenomen")
                    found.append(
                        (
                            pos,
                            self._smartart(rels[diagram.get(q("r:dm"))]["target"]),
                            None,
                        )
                    )
                else:
                    warnings.append("ingesloten object niet omgezet")
        return found

    def _order_key(self, pos: tuple[int, int]) -> tuple[int, int]:
        y, x = pos
        return (y // max(1, int(self.slide_height * ROW_TOLERANCE)), x)

    def _looks_like_title(self, shape: tuple[tuple[int, int], str, str | None]) -> bool:
        (y, _), text, ph_type = shape
        return (
            ph_type is None
            and y < self.slide_height * 0.25
            and "\n" not in text
            and len(text) <= 120
            and not re.match(r"^(-|\d+\.) |!\[|\|", text)
        )

    # --- slides --------------------------------------------------------------

    def slide(self, number: int) -> SlideResult:
        part = self.slide_parts[number - 1]
        root = self._xml(part)
        rels = self._rels(part)
        warnings: list[str] = []
        layout = self._related(rels, "slideLayout")
        master = self._related(self._rels(layout), "slideMaster") if layout else None
        master_root = self._xml(master) if master else None
        ctx = {
            "layout": self._placeholders(layout),
            "master": self._placeholders(master),
            "body_style": master_root.find("p:txStyles/p:bodyStyle", NS)
            if master_root is not None
            else None,
        }

        tree = root.find("p:cSld/p:spTree", NS)
        shapes = (
            self._shapes(tree, rels, ctx, number, warnings) if tree is not None else []
        )
        shapes.sort(key=lambda s: self._order_key(s[0]))

        title = next((md for _, md, t in shapes if t in TITLE_TYPES), "")
        if not title and shapes and self._looks_like_title(shapes[0]):
            # Decks built from the blank layout put the title in a text box.
            title = shapes[0][1]
        body = [md for _, md, t in shapes if md != title]
        title = title.replace("  \n", " ")
        heading = f"## Dia {number}" + (f": {title}" if title else "")
        if root.get("show") == "0":
            heading += " _(verborgen dia)_"
        blocks = [heading, *body]

        if self.notes:
            notes_part = self._related(rels, "notesSlide")
            notes_root = self._xml(notes_part) if notes_part else None
            if notes_root is not None:
                notes = []
                for sp in notes_root.iter(q("p:sp")):
                    ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
                    body_el = sp.find("p:txBody", NS)
                    if (
                        ph is not None
                        and ph.get("type") == "body"
                        and body_el is not None
                    ):
                        notes.append(
                            self._text_body(body_el, self._rels(notes_part), [])
                        )
                text = "\n\n".join(n for n in notes if n)
                if text:
                    blocks.append(
                        "**Notities:**\n\n"
                        + "\n".join(
                            f"> {line}" if line else ">" for line in text.split("\n")
                        )
                    )

        if not body:
            warnings.append("dia zonder tekst")
        return SlideResult(number, "\n\n".join(blocks), title, sorted(set(warnings)))

    def front_matter(self) -> str:
        core = self._xml("docProps/core.xml")

        def get(path: str) -> str:
            el = core.find(path, NS) if core is not None else None
            return (el.text or "").strip() if el is not None else ""

        def esc(value: str) -> str:
            return '"' + value.replace('"', '\\"') + '"'

        title = get("dc:title") or self.slug.replace("-", " ")
        lines = [
            "---",
            f"title: {esc(title)}",
            f"source_file: {esc(self.filename)}",
            f"slides: {len(self.slide_parts)}",
        ]
        for key, path in (
            ("author", "dc:creator"),
            ("created", "dcterms:created"),
            ("modified", "dcterms:modified"),
        ):
            if get(path):
                lines.append(f"{key}: {esc(get(path))}")
        lines += ['extracted_with: "pptx_convert (stdlib)"', "---"]
        return "\n".join(lines)

    def convert(self) -> PptxResult:
        slides = [self.slide(n) for n in range(1, len(self.slide_parts) + 1)]
        return PptxResult(slides, self.front_matter(), self.figures)


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Convert a .pptx to Markdown.")
    ap.add_argument("pptx", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    ap.add_argument("--no-notes", action="store_true", help="skip speaker notes")
    args = ap.parse_args()

    conv = PptxConverter(
        args.pptx.read_bytes(), args.pptx.name, notes=not args.no_notes
    )
    result = conv.convert()
    text = result.front_matter + "\n\n" + result.markdown
    if args.out is None:
        sys.stdout.write(text)
    else:
        (args.out / "figures").mkdir(parents=True, exist_ok=True)
        (args.out / f"{conv.slug}.md").write_text(text, encoding="utf-8", newline="\n")
        for path, data in result.figures.items():
            (args.out / path).write_bytes(data)
    for s in result.slides:
        for w in s.warnings:
            print(f"dia {s.number}: {w}", file=sys.stderr)
