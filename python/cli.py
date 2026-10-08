# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pymupdf==1.27.2.2",
#     "pymupdf4llm==0.3.4",
#     "tabulate",
# ]
# ///
"""Desktop harness for the conversion core.

Same convert.py the browser runs, so conversion quality can be iterated on
without a browser in the loop, and browser output can be diffed against a
known-good reference. Pins match the wheels in vendor/ -- if they drift, the
CLI stops being a valid reference for what the site produces.

Takes PDF, Word (.docx) and PowerPoint (.pptx). For Word, --pages counts
sections (split at the top-level headings); for PowerPoint, slides. Several
files or a folder give a folder per document plus overzicht.csv, the same as
"Download alles" in the browser.

    uv run python/cli.py test-pdfs/omgevingsvisie.pdf -o out/
    uv run python/cli.py test-pdfs/omgevingsvisie.pdf --pages 12-14 --stdout
    uv run python/cli.py test-pdfs/sample.pptx --no-notes --stdout
    uv run python/cli.py beleidsstukken/ -o out/ --anonymize email,telefoon,naam
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymupdf
import anonymize
from convert import Converter, Options, assemble, suggest_margins
from office import UNITS, OfficeConverter, detect_format


def parse_pages(spec: str | None, total: int) -> list[int]:
    """Parse "3", "3-9", "3,7,11-13" into a sorted list of 1-based pages."""
    if not spec:
        return list(range(1, total + 1))
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            wanted.update(range(int(lo), int(hi) + 1))
        elif part:
            wanted.add(int(part))
    return sorted(p for p in wanted if 1 <= p <= total)


ACCEPTED = {".pdf", ".docx", ".pptx"}


def is_junk(name: str) -> bool:
    """Office lock files, macOS metadata: never documents (same rule as intake.js)."""
    return name.startswith(("~$", "._", ".")) or name.lower() == "thumbs.db"


def collect(paths: list[Path]) -> list[tuple[Path, str]]:
    """(file, path relative to what was given) for every document to convert."""
    found = []
    for path in paths:
        if path.is_dir():
            for f in sorted(path.rglob("*")):
                rel = f.relative_to(path)
                if (
                    f.is_file()
                    and f.suffix.lower() in ACCEPTED
                    and not any(is_junk(part) for part in rel.parts)
                ):
                    found.append((f, rel.as_posix()))
        else:
            found.append((path, path.name))
    return found


def folder_name(relative: str, kind: str, used: set[str]) -> str:
    """Same naming as the browser's "Download alles": path-based, unique."""
    base = re.sub(r"\.(pdf|docx|pptx)$", "", relative, flags=re.IGNORECASE)
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "document"
    if base in used:
        base = f"{base}-{kind}"
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}-{n}", n + 1
    used.add(candidate)
    return candidate


def convert_one(path: Path, args: argparse.Namespace, out: Path | None) -> dict:
    """Convert one document; write it to `out`, or to stdout when `out` is None."""
    data = path.read_bytes()
    kind = detect_format(data, path.name)

    if kind != "pdf":
        top = bottom = 0.0  # nothing to crop: headers and footers are not read
    elif args.margin_top is None or args.margin_bottom is None:
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            auto_top, auto_bottom = suggest_margins(doc)
        finally:
            doc.close()
        top = auto_top if args.margin_top is None else args.margin_top
        bottom = auto_bottom if args.margin_bottom is None else args.margin_bottom
        print(f"margins: top={top} bottom={bottom} (auto-detected)", file=sys.stderr)
    else:
        top, bottom = args.margin_top, args.margin_bottom

    opts = Options(
        dpi=args.dpi,
        image_format=args.image_format,
        margin_top=top,
        margin_bottom=bottom,
        heading_mode=args.heading_mode,
        figure_legends=not args.no_legends,
        ignore_images=args.no_images,
        previews=False,  # the CLI has nothing to preview into
        speaker_notes=not args.no_notes,
        anonymize=tuple(
            c.strip()
            for c in args.anonymize.split(",")
            if c.strip() not in ("", "none")
        ),
    )
    masker = anonymize.Anonymizer(opts.anonymize)

    if kind == "pdf":
        conv: Converter | OfficeConverter = Converter(data, path.name, opts)
    else:
        conv = OfficeConverter(data, path.name, opts, kind=kind)
    unit = UNITS[kind]
    pages = parse_pages(args.pages, conv.page_count)
    print(
        f"{path.name}: {conv.page_count} {unit}s, converting {len(pages)}, "
        f"toc={len(conv.toc)} entries",
        file=sys.stderr,
    )

    started = time.perf_counter()
    results = []
    for i, number in enumerate(pages, 1):
        results.append(anonymize.apply(conv.page(number), masker))
        if i % 10 == 0 or i == len(pages):
            elapsed = time.perf_counter() - started
            print(
                f"  {i}/{len(pages)} {unit}s  {elapsed:.1f}s  {elapsed / i:.2f}s/{unit}",
                file=sys.stderr,
            )

    front = anonymize.front_matter(conv.front_matter(), masker)
    markdown = assemble(front, results, opts.page_separators, unit)

    if out is None:
        sys.stdout.write(markdown)
    else:
        (out / "figures").mkdir(parents=True, exist_ok=True)
        md_path = out / f"{conv.slug}.md"
        # newline="\n" so output is byte-identical to the browser's, which lets
        # the two be diffed directly instead of "diffed apart from line endings".
        md_path.write_text(markdown, encoding="utf-8", newline="\n")
        for fig_path, fig_bytes in conv._figures.items():
            (out / fig_path).parent.mkdir(parents=True, exist_ok=True)
            (out / fig_path).write_bytes(fig_bytes)
        print(
            f"wrote {md_path} ({len(markdown):,} chars) "
            f"+ {len(conv._figures)} figures in {out / 'figures'}",
            file=sys.stderr,
        )

    total: Counter = Counter()
    for r in results:
        total.update(r.masked)
    if masker:
        print(f"masked: {anonymize.summary(total) or 'nothing'}", file=sys.stderr)

    warned = [r for r in results if r.warnings]
    if warned:
        print(f"\n{len(warned)} {unit}(s) with warnings:", file=sys.stderr)
        for r in warned[:20]:
            print(f"  {unit} {r.number}: {'; '.join(r.warnings)}", file=sys.stderr)

    count = conv.page_count  # read before close: a closed PDF cannot answer
    conv.close()
    return {
        "kind": kind,
        "units": f"{count} {unit}s",
        "converted": len(results),
        "warned": len(warned),
        "masked": anonymize.summary(total),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert PDF, Word and PowerPoint documents to Markdown. "
        "Give one file, several, or a folder (searched recursively)."
    )
    ap.add_argument("documents", type=Path, nargs="+")
    ap.add_argument("-o", "--out", type=Path, default=Path("out"))
    ap.add_argument("--pages", help='e.g. "12-14" or "1,5,9"')
    ap.add_argument("--dpi", type=int, default=110, help="figure resolution")
    ap.add_argument(
        "--image-format",
        choices=["png", "jpg"],
        default="jpg",
        help="jpg is ~10x smaller; with png use --dpi 150 (see convert.py)",
    )
    ap.add_argument(
        "--margin-top", type=float, default=None, help="default: auto-detect"
    )
    ap.add_argument(
        "--margin-bottom", type=float, default=None, help="default: auto-detect"
    )
    ap.add_argument(
        "--heading-mode", choices=["hybrid", "toc", "fontsize"], default="hybrid"
    )
    ap.add_argument("--no-legends", action="store_true", help="skip figure label lists")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument(
        "--no-notes", action="store_true", help="PowerPoint: skip speaker notes"
    )
    ap.add_argument(
        "--anonymize",
        default=",".join(anonymize.DEFAULT_CATEGORIES),
        help="comma-separated categories to mask, or 'none' (default: %(default)s; "
        f"also available: {', '.join(c for c in anonymize.CATEGORIES if c not in anonymize.DEFAULT_CATEGORIES)})",
    )
    ap.add_argument(
        "--stdout", action="store_true", help="print Markdown instead of writing"
    )
    args = ap.parse_args()

    inputs = collect(args.documents)
    single = len(args.documents) == 1 and not args.documents[0].is_dir()
    if args.stdout and len(inputs) != 1:
        ap.error("--stdout works with exactly one document")
    if single:
        # One file: written straight into --out, as browser-test.mjs expects.
        convert_one(inputs[0][0], args, None if args.stdout else args.out)
        return

    # A batch: a folder per document plus overzicht.csv, the same layout as
    # "Download alles" in the browser. One failure does not stop the rest.
    used: set[str] = set()
    rows = [
        [
            "bestand",
            "pad",
            "formaat",
            "eenheden",
            "omgezet",
            "met waarschuwing",
            "gemaskeerd",
            "map",
            "status",
        ]
    ]
    for path, relative in inputs:
        try:
            data = path.read_bytes()
            kind = detect_format(data, path.name)
            folder = folder_name(relative, kind, used)
            info = convert_one(path, args, args.out / folder)
            rows.append(
                [
                    path.name,
                    relative,
                    info["kind"],
                    info["units"],
                    info["converted"],
                    info["warned"],
                    info["masked"],
                    folder,
                    "klaar",
                ]
            )
        except Exception as error:  # noqa: BLE001 -- reported per document
            print(f"{relative}: mislukt: {error}", file=sys.stderr)
            rows.append(
                [path.name, relative, "", "", 0, 0, "", "", f"mislukt: {error}"]
            )
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "overzicht.csv", "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";", lineterminator="\r\n").writerows(rows)
    failed = sum(1 for r in rows[1:] if r[-1] != "klaar")
    print(
        f"\n{len(rows) - 1} documents, {failed} failed -> {args.out}", file=sys.stderr
    )


if __name__ == "__main__":
    main()
