# Derde-partijcomponenten

Alles wat deze site nodig heeft staat in de repository onder `vendor/`. Er wordt
tijdens gebruik niets van een CDN of andere server gehaald.

| Component | Versie | Licentie | Locatie |
|---|---|---|---|
| [MuPDF](https://mupdf.com/) / [PyMuPDF](https://pymupdf.io/) | 1.27.2.2 | AGPL-3.0 (of commercieel via Artifex) | `vendor/pyodide/pymupdf-1.27.2.2-*.whl` |
| [pymupdf4llm](https://github.com/pymupdf/pymupdf4llm) | 0.3.4 | AGPL-3.0 (of commercieel via Artifex) | `vendor/wheels/pymupdf4llm-0.3.4-py3-none-any.whl` |
| [tabulate](https://github.com/astanin/python-tabulate) | 0.10.0 | MIT | `vendor/wheels/tabulate-0.10.0-py3-none-any.whl` |
| [Pyodide](https://pyodide.org/) | 314.0.3 | MPL-2.0 | `vendor/pyodide/` |
| CPython (in Pyodide) | 3.14 | PSF-2.0 | `vendor/pyodide/python_stdlib.zip` |

Geen npm-afhankelijkheden in het eindproduct. De ZIP-export gebruikt de
ingebouwde `CompressionStream` van de browser in plaats van een
JavaScript-bibliotheek, zodat er geen geminificeerde derde-partijcode in de
pagina zit. Playwright staat alleen in `package.json`-loze `--no-save`-vorm voor
de browsertest en komt niet in de gepubliceerde site terecht.

## Wat de licentiekeuze betekent

MuPDF en PyMuPDF zijn AGPL-3.0. Deze site distribueert die code als
WebAssembly naar de browser van de bezoeker, en dat is distributie in de zin van
de licentie. Daarom is deze repository zelf AGPL-3.0 en publiek: dat is precies
wat de AGPL vraagt.

Twee praktische gevolgen:

- Een afgeleide versie die niet publiek is, mag niet onder de AGPL. Daarvoor is
  een commerciële licentie van [Artifex](https://artifex.com/licensing) nodig.
- `pymupdf4llm` is bewust vastgezet op **0.3.4**, de laatste versie in de AGPL-lijn
  die alleen `pymupdf` en `tabulate` nodig heeft. De nieuwere 1.27/1.28-versies
  hangen aan `pymupdf-layout`, dat onder Polyform Noncommercial valt én
  `onnxruntime` vereist — waarvoor geen WebAssembly-build bestaat. Die versies
  zijn hier dus zowel juridisch als technisch onbruikbaar.

## Herkomst controleren

De PyMuPDF-wheel komt uit de officiële Pyodide-distributie en de checksum staat
in `vendor/pyodide/pyodide-lock.json`:

```bash
python - <<'PY'
import hashlib, json, pathlib
lock = json.load(open("vendor/pyodide/pyodide-lock.json"))
name = lock["packages"]["pymupdf"]["file_name"]
data = pathlib.Path("vendor/pyodide", name).read_bytes()
print(name)
print("verwacht:", lock["packages"]["pymupdf"]["sha256"])
print("gevonden:", hashlib.sha256(data).hexdigest())
PY
```

De twee pure-Python wheels komen ongewijzigd van PyPI en kunnen daar tegen
worden gecontroleerd.
