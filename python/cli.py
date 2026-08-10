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

    uv run python/cli.py test-pdfs/omgevingsvisie.pdf -o out/
    uv run python/cli.py test-pdfs/omgevingsvisie.pdf --pages 12-14 --stdout
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymupdf
from convert import Converter, Options, assemble, suggest_margins


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert a PDF to Markdown.")
    ap.add_argument("pdf", type=Path)
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
        "--stdout", action="store_true", help="print Markdown instead of writing"
    )
    args = ap.parse_args()

    pdf_bytes = args.pdf.read_bytes()

    if args.margin_top is None or args.margin_bottom is None:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
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
    )

    conv = Converter(pdf_bytes, args.pdf.name, opts)
    pages = parse_pages(args.pages, conv.page_count)
    print(
        f"{args.pdf.name}: {conv.page_count} pages, converting {len(pages)}, "
        f"toc={len(conv.toc)} entries",
        file=sys.stderr,
    )

    started = time.perf_counter()
    results = []
    for i, number in enumerate(pages, 1):
        results.append(conv.page(number))
        if i % 10 == 0 or i == len(pages):
            elapsed = time.perf_counter() - started
            print(
                f"  {i}/{len(pages)} pages  {elapsed:.1f}s  {elapsed / i:.2f}s/page",
                file=sys.stderr,
            )

    markdown = assemble(conv.front_matter(), results, opts.page_separators)

    if args.stdout:
        sys.stdout.write(markdown)
    else:
        out = args.out
        (out / "figures").mkdir(parents=True, exist_ok=True)
        md_path = out / f"{conv.slug}.md"
        # newline="\n" so output is byte-identical to the browser's, which lets
        # the two be diffed directly instead of "diffed apart from line endings".
        md_path.write_text(markdown, encoding="utf-8", newline="\n")
        for path, data in conv._figures.items():
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(data)
        print(
            f"wrote {md_path} ({len(markdown):,} chars) "
            f"+ {len(conv._figures)} figures in {out / 'figures'}",
            file=sys.stderr,
        )

    warned = [r for r in results if r.warnings]
    if warned:
        print(f"\n{len(warned)} page(s) with warnings:", file=sys.stderr)
        for r in warned[:20]:
            print(f"  p{r.number}: {'; '.join(r.warnings)}", file=sys.stderr)

    conv.close()


if __name__ == "__main__":
    main()
