# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pymupdf==1.28.0",
# ]
# ///
"""Generate a synthetic stand-in for a Rotterdam policy PDF.

Exercises the features that actually break naive extraction: a two-column
spread, a running header/footer on every page, a sidebar, a pull quote, a
table, a bookmark tree, and a "map" figure with vector text inside it.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

OUT = Path(__file__).parent.parent / "test-pdfs" / "sample.pdf"

BODY = (
    "De gemeente Rotterdam werkt aan een gezonde, groene en toegankelijke stad. "
    "In deze visie staan de ambities voor de periode tot 2040 beschreven, met "
    "aandacht voor verdichting, mobiliteit en klimaatadaptatie. De opgaven zijn "
    "groot en vragen om samenwerking met bewoners, ondernemers en partners in de "
    "regio. Per gebied wordt een afweging gemaakt tussen ruimte voor wonen en "
    "ruimte voor groen. "
) * 3


def header_footer(page: pymupdf.Page, n: int) -> None:
    page.insert_text(
        (40, 30), "Omgevingsvisie Rotterdam 2040", fontsize=8, color=(0.5, 0.5, 0.5)
    )
    page.insert_text((500, 812), f"| {n}", fontsize=8, color=(0.5, 0.5, 0.5))


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()

    # p1 — title page
    p = doc.new_page()
    p.insert_text((60, 200), "Omgevingsvisie", fontsize=34)
    p.insert_text((60, 245), "Rotterdam 2040", fontsize=34)
    p.insert_text((60, 300), "Gemeente Rotterdam", fontsize=12, color=(0.3, 0.3, 0.3))
    header_footer(p, 1)

    # p2 — two-column body + a sidebar box + a pull quote
    p = doc.new_page()
    p.insert_text((60, 90), "1. Een gezonde stad", fontsize=20)
    p.insert_textbox(pymupdf.Rect(60, 120, 285, 500), BODY, fontsize=9)
    p.insert_textbox(pymupdf.Rect(310, 120, 535, 500), BODY, fontsize=9)
    p.draw_rect(
        pymupdf.Rect(60, 520, 535, 620),
        color=(0.85, 0.9, 0.85),
        fill=(0.94, 0.97, 0.94),
    )
    p.insert_textbox(
        pymupdf.Rect(70, 530, 525, 610),
        "Kader: in 2040 heeft iedere Rotterdammer binnen 300 meter toegang tot "
        "een groene plek van minimaal 0,5 hectare.",
        fontsize=9,
    )
    p.insert_textbox(
        pymupdf.Rect(60, 640, 535, 720),
        '"De stad groeit, maar niet ten koste van het groen."',
        fontsize=17,
        color=(0.2, 0.4, 0.2),
    )
    header_footer(p, 2)

    # p3 — a "map" drawn as vector graphics with labels inside it
    p = doc.new_page()
    p.insert_text((60, 90), "2. Groene long", fontsize=20)
    map_rect = pymupdf.Rect(60, 120, 535, 480)
    p.draw_rect(map_rect, color=(0.6, 0.7, 0.8), fill=(0.90, 0.94, 0.97))
    p.draw_circle(
        pymupdf.Point(200, 260), 55, color=(0.2, 0.5, 0.3), fill=(0.75, 0.88, 0.78)
    )
    p.draw_circle(
        pymupdf.Point(400, 330), 70, color=(0.2, 0.5, 0.3), fill=(0.75, 0.88, 0.78)
    )
    p.draw_line(pymupdf.Point(70, 400), pymupdf.Point(525, 380))
    # labels *inside* the figure — extractable vector text, invisible once cropped
    for pt, label in [
        ((165, 262), "Kralingse Plas"),
        ((360, 332), "Zuiderpark"),
        ((80, 395), "Maas"),
        ((430, 150), "zone A"),
        ((430, 165), "2030-2040"),
        ((70, 465), "legenda: groen = bestaand, blauw = water"),
    ]:
        p.insert_text(pt, label, fontsize=7)
    p.insert_text(
        (60, 505), "Figuur 4: Groene long 2040", fontsize=9, color=(0.3, 0.3, 0.3)
    )
    p.insert_textbox(pymupdf.Rect(60, 530, 535, 700), BODY[:600], fontsize=9)
    header_footer(p, 3)

    # p4 — a table with ruling lines (lines_strict needs them)
    p = doc.new_page()
    p.insert_text((60, 90), "3. Indicatoren", fontsize=20)
    rows = [
        ["Indicator", "2025", "2030", "2040"],
        ["Groen per inwoner (m2)", "18", "22", "28"],
        ["Woningen (x1000)", "310", "345", "390"],
        ["Modal split OV (%)", "24", "31", "40"],
    ]
    x0, y0, cw, rh = 60, 120, 118, 26
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            cell_rect = pymupdf.Rect(
                x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh
            )
            p.draw_rect(cell_rect, color=(0.4, 0.4, 0.4), width=0.6)
            p.insert_text((cell_rect.x0 + 5, cell_rect.y0 + 17), cell, fontsize=8)
    p.insert_textbox(pymupdf.Rect(60, 260, 535, 500), BODY[:700], fontsize=9)
    header_footer(p, 4)

    # p5 — an image-only page: no text layer at all (the OCR-warning case)
    p = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 400))
    pix.set_rect(pix.irect, (220, 225, 235))
    p.insert_image(pymupdf.Rect(60, 120, 535, 460), pixmap=pix)

    doc.set_toc(
        [
            [1, "Omgevingsvisie Rotterdam 2040", 1],
            [1, "1. Een gezonde stad", 2],
            [1, "2. Groene long", 3],
            [2, "Figuur 4: Groene long 2040", 3],
            [1, "3. Indicatoren", 4],
        ]
    )
    doc.set_metadata(
        {"title": "Omgevingsvisie Rotterdam 2040", "author": "Gemeente Rotterdam"}
    )
    doc.save(OUT)
    print(f"wrote {OUT} pages={doc.page_count} bytes={OUT.stat().st_size}")


if __name__ == "__main__":
    main()
