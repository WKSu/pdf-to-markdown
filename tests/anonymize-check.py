"""Check that anonymization masks what it should and leaves policy text alone.

Two halves, and the second matters as much as the first: "not too aggressive"
is a requirement, so every sentence under KEEP is something a policy document
says that a cruder masker destroys -- an amount, a target year, a chart axis,
a quantity in megawatts, a street where a project is.

    python tests/anonymize-check.py          (standard library only)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from anonymize import CATEGORIES, DEFAULT_CATEGORIES, Anonymizer  # noqa: E402

ALL = tuple(CATEGORIES)

# (text, categories, expected) -- expected output after masking.
MASK = [
    (
        "Mail j.jansen@rotterdam.nl of bel 06-12345678.",
        DEFAULT_CATEGORIES,
        "Mail [Email] of bel [Telefoon].",
    ),
    (
        "Bel 010 - 123 45 67 of 010 123 45 67.",
        DEFAULT_CATEGORIES,
        "Bel [Telefoon] of [Telefoon].",
    ),
    (
        "Telefoon: +31 (0)10 1234567, mobiel 0031 6 12345678.",
        DEFAULT_CATEGORIES,
        "Telefoon: [Telefoon], mobiel [Telefoon].",
    ),
    (
        "Neem contact op via [mail](mailto:a.b@c.nl).",
        DEFAULT_CATEGORIES,
        "Neem contact op via mail.",
    ),
    (
        "Rekening NL91 ABNA 0417 1643 00 op naam.",
        DEFAULT_CATEGORIES,
        "Rekening [IBAN] op naam.",
    ),
    (
        "BSN 123456782 en burgerservicenummer: 123456789.",
        DEFAULT_CATEGORIES,
        "BSN [BSN] en burgerservicenummer: [BSN].",
    ),
    (
        "Kaart 4539 5787 6362 1486 betaald.",
        DEFAULT_CATEGORIES,
        "Kaart [Creditcard] betaald.",
    ),
    (
        "Auto met kenteken AB-123-C of 12-ABC-3.",
        DEFAULT_CATEGORIES,
        "Auto met kenteken [Kenteken] of [Kenteken].",
    ),
    (
        "Postbus 70012, 3000 KP Rotterdam.",
        DEFAULT_CATEGORIES,
        "Postbus 70012, [Postcode] Rotterdam.",
    ),
    ("Adres: 3011 AD 40.", DEFAULT_CATEGORIES, "Adres: [Postcode]."),
    (
        "Den Haag, 2511 CV Den Haag.",
        DEFAULT_CATEGORIES,
        "Den Haag, [Postcode] Den Haag.",
    ),
    (
        "Gesprek met dhr. Jansen en mevrouw De Vries.",
        ("naam",),
        "Gesprek met dhr. [Naam] en mevrouw [Naam].",
    ),
    (
        "Van: R. van der Wulp, Kopie: J.P. Bakker.",
        ("naam",),
        "Van: [Naam], Kopie: [Naam].",
    ),
    (
        "Met vriendelijke groet,\n\nA. Smit\nTeamleider",
        ("naam",),
        "Met vriendelijke groet,\n\n[Naam]\nTeamleider",
    ),
    (
        "Hij woont aan de Schiedamseweg 12a, zij op Coolsingel 40.",
        ("adres",),
        "Hij woont aan de [Adres], zij op [Adres].",
    ),
    (
        "Zij woont aan de Bloemstraat 5 in Utrecht.",
        ("adres",),
        "Zij woont aan de [Adres] in Utrecht.",
    ),
    (
        "Geboren op 12-03-1987, nu 37 jaar oud.",
        ("datum",),
        "Geboren op [Datum], nu [Datum].",
    ),
    (
        "Geb. 3 april 1952; geboortedatum: 1-2-03.",
        ("datum",),
        "Geb. [Datum]; geboortedatum: [Datum].",
    ),
]

KEEP = [
    # amounts, numbers, dates, years: policy content
    "Het budget is € 2,5 miljoen voor 2025 tot en met 2030.",
    "Per 1 januari 2030 geldt de nieuwe norm; dat is 1.250.000 inwoners.",
    "De stad telt 150000 inwoners meer en 36000 woningen.",
    "In 2030 EU-breed, en in 2024 NS en RET samen.",
    "Doel: 1500 MW windenergie en 3000 KM fietspad.",
    "Zorg voor 65-jarigen; de bouw wordt opgeleverd op 01-07-2026.",
    # chart axes and tables as PDFs extract them
    "_Labels in figuur:_ 0 25 50 75 100; 0 10 20 30 40 50",
    "| 2022 | 41.2 | 0,5 |",
    # municipal service number, opening hours, ranges
    "Bel 14 010, ma-vr 08.00-17.00 uur, of 0800-1234.",
    # section labels and places named after people
    "A. Inleiding\n\nZie bijlage B. Kaarten en het Mr. Visserplein.",
    "De ir. P. Kosterlaan en Dr. Zamenhofstraat worden heringericht.",
    # a street without a house number is where a project is
    "Herinrichting van de Coolsingel en de Weena, 100 meter fietspad.",
    "Weena 100 meter verderop.",
    # Markdown structure
    "![Kaart](figures/omgevingsvisie-p012-3.jpg)\n\n<!-- page 1234 -->",
    "Zie [de visie](https://www.rotterdam.nl/omgevingsvisie-2040-0612345678).",
    # a 9-digit number that fails the 11-proef
    "Projectnummer 123456789 is gesloten.",
    # a version number and a year range
    "Versie 1.2.3.4 voor de periode 2020-2030.",
]


def main() -> None:
    failures = 0
    for text, categories, expected in MASK:
        got, _ = Anonymizer(categories).mask(text)
        ok = got == expected
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} mask  {text[:60]!r}")
        if not ok:
            print(f"         expected {expected!r}\n         got      {got!r}")
    for text in KEEP:
        bad = []
        for categories, label in ((DEFAULT_CATEGORIES, "default"), (ALL, "all")):
            got, _ = Anonymizer(categories).mask(text)
            if got != text:
                bad.append(f"         ({label}) got {got!r}")
        failures += bool(bad)
        print(f"  {'FAIL' if bad else 'ok  '} keep  {text[:60]!r}")
        for line in bad:
            print(line)
    print(f"\n{'FAIL' if failures else 'PASS'}: {failures} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
