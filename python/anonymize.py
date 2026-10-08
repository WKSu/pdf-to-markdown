"""Mask personal data in converted Markdown -- deliberately not aggressively.

Built on the patterns of the Maskeringsscript of the City of Rotterdam
(github.com/minbzk/maskeringsscript, EUPL-1.2; see THIRD_PARTY.md), adapted for
a different kind of text. That script was made for complaint texts and case
notes, where every date, amount and long number is likely to be about a
person, so it masks all of them. In a policy document those are the content:
"EUR 2,5 miljoen", "1 januari 2030", "150000 inwoners". So this module splits
the patterns in two:

- On by default: identifiers that are personal data wherever they appear and
  are almost never policy content -- e-mail, phone, IBAN, BSN, credit card,
  licence plate, postcode. Each pattern is tuned to leave alone what looks
  like them in a policy text (see the comments at each).
- Opt-in: names, addresses (street + house number) and dates/ages. Useful for
  letters and notes, too costly to switch on blindly for policy documents.

What the script does and this module does not: name recognition with the
GLiNER model (needs PyTorch, which does not run in the browser), and its word
lists of first names, surnames and nationalities, which on policy text mask
ordinary words ("Visser", "Bakker", "Mark") and content ("Turkse").

Masks are written as [Telefoon] rather than the script's <Telefoon>: in
Markdown, <Telefoon> is an HTML tag and a Markdown viewer shows nothing.

Like convert.py, nothing here knows a browser exists. No masking is ever
complete -- the result still needs a human look, which the review screen is for.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

CATEGORIES = {
    # key: (mask, on by default)
    "email": ("[Email]", True),
    "telefoon": ("[Telefoon]", True),
    "iban": ("[IBAN]", True),
    "bsn": ("[BSN]", True),
    "creditcard": ("[Creditcard]", True),
    "kenteken": ("[Kenteken]", True),
    "postcode": ("[Postcode]", True),
    "naam": ("[Naam]", False),
    "adres": ("[Adres]", False),
    "datum": ("[Datum]", False),
}
DEFAULT_CATEGORIES = tuple(k for k, (_, on) in CATEGORIES.items() if on)

# --------------------------------------------------------------- patterns

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Stricter than the script's, which accepts any 0 followed by 7-12 digits with
# separators -- and so reads a chart axis extracted from a PDF ("0 25 50 75
# 100") as a phone number. A Dutch number has exactly ten digits and is never
# written with a lone leading 0, so the first two digits must be adjacent.
PHONE = re.compile(
    r"""
    (?<![\w+])(?:
        (?:\+|00)\d{1,3}[ \-]?(?:\(0\)[ \-]?)?\d(?:[ \-]?\d){7,10}  # +31 6 12345678
      | \(0\d{1,3}\)[ \-]?\d(?:[ \-]?\d){5,7}                      # (010) 123 45 67
      | 0\d(?:(?:[ ]?-[ ]?|[ ])?\d){8}                            # 06-12345678, 010 - 123 45 67
    )(?![\d\w])
    """,
    re.VERBOSE,
)

IBAN = re.compile(r"\b[A-Z]{2}\d{2}[ ]?[A-Z]{4}[ ]?(?:\d{4}[ ]?){2}\d{2}\b")

BSN = re.compile(r"\b(?:\d{9}|\d{4}\.\d{2}\.\d{3})\b")
BSN_CONTEXT = re.compile(
    r"(?i)\b(bsn|burgerservicenummer)\b(\W{0,15}?)(\d{9}|\d{4}\.\d{2}\.\d{3})\b"
)

CREDIT_CARD = re.compile(
    r"\b(?:\d{4}(?:[ -]?\d{4}){3}(?:[ -]?\d{3})?|\d{4}[ -]?\d{6}[ -]?\d{4,5}|\d{13,19})\b"
)

# Dutch plate layouts, from the script. Hyphenated forms in either case,
# others upper case only, so "in de 30" or a product code does not match.
_PLATE = r"""
    [A-Z]{2}-\d{2}-\d{2} | \d{2}-[A-Z]{2}-\d{2} | \d{2}-\d{2}-[A-Z]{2}
  | [A-Z]{2}-\d{2}-[A-Z]{2} | [A-Z]{2}-[A-Z]{2}-\d{2} | \d{2}-[A-Z]{2}-[A-Z]{2}
  | [A-Z]{2}-\d{3}-[A-Z] | [A-Z]{3}-\d{2}-[A-Z] | [A-Z]-\d{3}-[A-Z]{2}
  | \d-[A-Z]{3}-\d{2} | \d{2}-[A-Z]{3}-\d
"""
LICENCE_PLATE = re.compile(rf"(?<![\w-])(?:{_PLATE})(?![\w-])", re.VERBOSE)

# 1234 AB. Dutch postcodes never use SA, SD or SS. A year followed by an
# acronym looks the same ("in 2030 EU-breed", "2024 NS"), and so does a
# quantity ("1500 MW windenergie"): those only count with a place name or a
# house number after them.
UNIT_LETTERS = {
    "MW",
    "KW",
    "GW",
    "TW",
    "KM",
    "CM",
    "MM",
    "KG",
    "PJ",
    "TJ",
    "GJ",
    "MJ",
    "HA",
}
POSTCODE = re.compile(
    r"(?<![\w-])(?P<num>[1-9]\d{3})[ ]?(?!SA|SD|SS)(?P<let>[A-Z]{2})(?![\w-])"
    r"(?P<tail>[ ]+(?:\d{1,4}[a-zA-Z]?\b|[A-Z][a-z]))?"
)

# A signature after a closing greeting, anchored on initials (from the script).
SIGN_OFF = re.compile(
    r"(?m)^[ \t>]*"
    r"(?i:(?:met\s+(?:vriendelijke|hartelijke|de\s+meeste)\s+)?"
    r"(?:groet(?:en)?|hoogachtend|hoogachting))"
    r"[ \t]*,?[ \t]*\n+[ \t>]*"
    r"(?P<name>(?:[A-Z]\.[ \t]*){1,3}[^\n]*\S)"
)

_TUSSEN = r"(?:(?i:van|de|der|den|het|ten|ter|te|in\ 't|'t|von|el|al|ben)\s+)*"
_SURNAME = r"[A-Z][a-zà-öø-ÿ'’]+(?:-[A-Z][a-zà-öø-ÿ'’]+)?"
# Streets named after people ("Mr. Visserplein", "Dr. Zamenhofstraat") are
# places, not persons.
_NOT_STREET = (
    r"(?<!straat)(?<!plein)(?<!laan)(?<!weg)(?<!kade)(?<!singel)(?<!dijk)"
    r"(?<!gracht)(?<!hof)(?<!park)(?<!dreef)(?<!pad)(?<!markt)"
)
# A name announced by a form of address: "dhr. Jansen", "mevrouw De Vries",
# "de heer J.P. van den Berg". The form of address stays.
HONORIFIC_NAME = re.compile(
    r"\b(?P<title>(?i:(?:dhr|mevr|mw|hr|mr|dr|drs|ir|ing|prof)\.?|de\ heer|mevrouw|meneer))"
    rf"\s+(?P<name>(?:[A-Z]\.\s?){{0,4}}{_TUSSEN}{_SURNAME}){_NOT_STREET}(?![\w-])",
    re.VERBOSE,
)
# Initials and a surname: "R. van der Wulp", "J.P. de Vries". One initial and
# a capitalised word is far more often a label ("A. Inleiding", "Bijlage B.
# Kaarten"), so it takes two initials or a tussenvoegsel. After a form of
# address (above) one initial is enough.
INITIALS_NAME = re.compile(
    rf"\b(?:(?:[A-Z]\.\s?){{2,4}}{_TUSSEN}|[A-Z]\.\s?(?:(?:van|de|der|den|ten|ter|te|von)\s+)+)"
    rf"{_SURNAME}{_NOT_STREET}(?![\w-])"
)

# Birth dates and ages only. The script masks every full date because in a
# complaint text a date may be a birth date; in a policy document dates are
# deadlines and target years ("per 1 januari 2030"), so here a date needs to
# be announced as a birth date.
_MONTHS = (
    r"januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|"
    r"november|december|jan|feb|mrt|apr|jun|jul|aug|sep|sept|okt|nov|dec"
)
_DATE = (
    rf"\d{{1,2}}[-/.]\d{{1,2}}[-/.](?:19|20)?\d{{2}}"
    rf"|\d{{1,2}}\s+(?i:{_MONTHS})\.?\s+(?:19|20)\d{{2}}"
)
BIRTH_DATE = re.compile(
    rf"(?P<lead>\b(?i:geboren(?:\s+op)?|geb\.|geboortedatum)[ \t:]*)(?:{_DATE})\b"
)
# "37 jaar oud", "de 37-jarige"; not "65-jarigen", which is a group.
AGE = re.compile(r"\b\d{1,3}\s?jaar\s?oud\b|\b\d{1,3}[- ]?jarige\b", re.IGNORECASE)

HOUSE_NUMBER = r"\d{1,4}(?:[ -]?[a-zA-Z]\b)?(?:-\d{1,4})?"
# Units that turn "Weena 100" into a distance rather than an address.
_NOT_ADDRESS_AFTER = re.compile(
    r"\s*(?:%|procent|m\b|km\b|meter|kilometer|woningen|jaar|euro|mln|miljoen)",
    re.IGNORECASE,
)
STREET_SUFFIX = re.compile(
    r"[A-Z][a-zà-ÿ'\-]*(?:straat|laan|weg|plein|kade|singel|dijk|gracht|hof|dreef|"
    r"pad|steeg|park|markt|boulevard|kanaal|haven|wal|plantsoen|erf|veld|baan|"
    r"burcht|passage|promenade|rijweg|polder)$"
)


def _streets() -> frozenset[str]:
    path = Path(__file__).parent / "data" / "straatnamen-rotterdam.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return frozenset()
    return frozenset(s.lower() for s in lines if s and not s.startswith("#"))


# --------------------------------------------------------------- helpers


def is_valid_bsn(digits: str) -> bool:
    """The 11-proef, as in the script."""
    digits = digits.replace(".", "")
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = sum(int(d) * (9 - i) for i, d in enumerate(digits[:8])) - int(digits[8])
    return total % 11 == 0


def is_valid_luhn(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


# Spans Markdown needs intact: link and image targets, HTML comments (page
# separators), code. Masking a digit run inside "figures/x-p012-3.jpg" would
# break the image.
PROTECTED = re.compile(r"\]\([^)\n]*\)|<!--.*?-->|`[^`\n]*`", re.DOTALL)
# A mailto:/tel: link would keep the address in its target; unwrap it first so
# the visible text is all that is left to mask.
CONTACT_LINK = re.compile(r"\[([^\]\n]*)\]\((?:mailto|tel):[^)\n]*\)", re.IGNORECASE)


class Anonymizer:
    def __init__(self, categories: Iterable[str] = DEFAULT_CATEGORIES) -> None:
        unknown = set(categories) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"onbekende categorie(ën): {', '.join(sorted(unknown))}")
        self.categories = [c for c in CATEGORIES if c in set(categories)]
        self.streets = _streets() if "adres" in self.categories else frozenset()

    def __bool__(self) -> bool:
        return bool(self.categories)

    def mask(self, markdown: str) -> tuple[str, Counter]:
        """Masked Markdown, and how often each category was masked."""
        counts: Counter = Counter()
        if not self.categories:
            return markdown, counts
        if {"email", "telefoon"} & set(self.categories):
            markdown = CONTACT_LINK.sub(r"\1", markdown)
        out, last = [], 0
        for m in PROTECTED.finditer(markdown):
            out.append(self._mask_text(markdown[last : m.start()], counts))
            out.append(m.group(0))
            last = m.end()
        out.append(self._mask_text(markdown[last:], counts))
        return "".join(out), counts

    # Order matters, as in the script: specific before broad, so a BSN or IBAN
    # is gone before the phone pattern could read part of it.
    def _mask_text(self, text: str, counts: Counter) -> str:
        steps: list[tuple[str, Callable[[str, str, Counter], str]]] = [
            ("email", self._email),
            ("iban", self._iban),
            ("creditcard", self._credit_card),
            ("bsn", self._bsn),
            ("telefoon", self._phone),
            ("kenteken", self._plate),
            ("adres", self._address),
            ("postcode", self._postcode),
            ("datum", self._date),
            ("naam", self._name),
        ]
        for key, step in steps:
            if key in self.categories:
                text = step(text, CATEGORIES[key][0], counts)
        return text

    @staticmethod
    def _sub(
        pattern: re.Pattern, text: str, tag: str, key: str, counts: Counter
    ) -> str:
        text, n = pattern.subn(tag, text)
        counts[key] += n
        return text

    def _email(self, text, tag, counts):
        return self._sub(EMAIL, text, tag, "email", counts)

    def _iban(self, text, tag, counts):
        return self._sub(IBAN, text, tag, "iban", counts)

    def _phone(self, text, tag, counts):
        return self._sub(PHONE, text, tag, "telefoon", counts)

    def _plate(self, text, tag, counts):
        return self._sub(LICENCE_PLATE, text, tag, "kenteken", counts)

    def _credit_card(self, text, tag, counts):
        def repl(m: re.Match) -> str:
            if is_valid_luhn(re.sub(r"\D", "", m.group(0))):
                counts["creditcard"] += 1
                return tag
            return m.group(0)

        return CREDIT_CARD.sub(repl, text)

    def _bsn(self, text, tag, counts):
        # With "BSN" in front, a 9-digit number is masked even if mistyped;
        # elsewhere only when it passes the 11-proef.
        def context(m: re.Match) -> str:
            counts["bsn"] += 1
            return f"{m.group(1)}{m.group(2)}{tag}"

        def valid(m: re.Match) -> str:
            if is_valid_bsn(m.group(0)):
                counts["bsn"] += 1
                return tag
            return m.group(0)

        return BSN.sub(valid, BSN_CONTEXT.sub(context, text))

    def _postcode(self, text, tag, counts):
        def repl(m: re.Match) -> str:
            tail = m.group("tail") or ""
            ambiguous = (
                1900 <= int(m.group("num")) <= 2099 or m.group("let") in UNIT_LETTERS
            )
            if ambiguous and not tail:
                return m.group(0)
            counts["postcode"] += 1
            # A house number right after the postcode goes with it; a place
            # name stays.
            if tail.strip()[:1].isdigit():
                return tag
            return tag + tail

        return POSTCODE.sub(repl, text)

    def _address(self, text, tag, counts):
        """Street + house number, for Rotterdam streets or any common suffix.

        A street name alone stays: in a policy document it is where a project
        is, not where someone lives.
        """
        number = re.compile(rf"(?<![\w-]){HOUSE_NUMBER}(?![\w-])")
        out, last = [], 0
        for m in number.finditer(text):
            if _NOT_ADDRESS_AFTER.match(text, m.end()):
                continue
            words = list(
                re.finditer(r"[\w'’.\-]+", text[max(last, m.start() - 80) : m.start()])
            )
            offset = max(last, m.start() - 80)
            start = None
            # Longest run of up to five words before the number that is a street.
            for k in range(min(5, len(words)), 0, -1):
                candidate = words[-k:]
                gap = text[offset + candidate[-1].end() : m.start()]
                if gap.strip():
                    break
                name = " ".join(w.group(0) for w in candidate)
                if name.lower() in self.streets or (
                    k == 1 and STREET_SUFFIX.match(name)
                ):
                    start = offset + candidate[0].start()
                    break
            if start is None:
                continue
            out.append(text[last:start] + tag)
            last = m.end()
            counts["adres"] += 1
        out.append(text[last:])
        return "".join(out)

    def _date(self, text, tag, counts):
        def birth(m: re.Match) -> str:
            counts["datum"] += 1
            return m.group("lead") + tag

        return self._sub(AGE, BIRTH_DATE.sub(birth, text), tag, "datum", counts)

    def _name(self, text, tag, counts):
        def sign_off(m: re.Match) -> str:
            counts["naam"] += 1
            return m.group(0).replace(m.group("name"), tag)

        def honorific(m: re.Match) -> str:
            counts["naam"] += 1
            return f"{m.group('title')} {tag}"

        text = SIGN_OFF.sub(sign_off, text)
        text = HONORIFIC_NAME.sub(honorific, text)
        return self._sub(INITIALS_NAME, text, tag, "naam", counts)


# ------------------------------------------------------- page-level helpers


def summary(counts: Counter) -> str:
    """'2 telefoon, 1 email' -- for the review screen and the CLI."""
    return ", ".join(f"{n} {key}" for key, n in counts.most_common() if n)


def apply(result, anonymizer: Anonymizer):
    """A PageResult with its Markdown masked and the counts attached."""
    if not anonymizer:
        return result
    markdown, counts = anonymizer.mask(result.markdown)
    return replace(result, markdown=markdown, masked=dict(counts), chars=len(markdown))


def front_matter(text: str, anonymizer: Anonymizer) -> str:
    """Drop the author and record what was masked.

    The author field is metadata nobody sees while reviewing pages, and it is
    a person's name, so it goes whenever anonymization is on at all.
    """
    if not anonymizer:
        return text
    lines = [line for line in text.splitlines() if not line.startswith("author:")]
    marker = f"anonymized: [{', '.join(anonymizer.categories)}]"
    if lines and lines[-1] == "---":
        lines.insert(len(lines) - 1, marker)
    return "\n".join(lines)
