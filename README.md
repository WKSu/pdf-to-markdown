# PDF, Word en PowerPoint naar Markdown — lokaal in de browser

Beleidsdocumenten (omgevingsvisies, woonvisies, ambitiedocumenten, notities,
presentaties) omzetten naar Markdown, zodat ze als invoer voor AI-modellen
gebruikt kunnen worden. **Alle verwerking gebeurt in je eigen browser. Er gaat
geen document naar een server.**

Zet je `.pdf`, `.docx` of `.pptx` in het venster, kijk het resultaat na, en
download een ZIP met `document.md` en een map `figures/`. Een batch mag de drie
formaten door elkaar bevatten. Persoonsgegevens worden gemaskeerd, zonder de
beleidsinhoud aan te tasten (zie [Persoonsgegevens maskeren](#persoonsgegevens-maskeren)).

## In bulk

Sleep een hele map in het venster, of gebruik **Kies een map**: alle PDF-,
Word- en PowerPoint-bestanden erin worden omgezet, ook die in submappen.
Lock-bestanden van Office (`~$…`) en macOS-rommel (`.DS_Store`) worden
overgeslagen, andere bestandstypen gemeld. Bestanden worden pas gelezen als ze
aan de beurt zijn, zodat een grote map niet in één keer in het geheugen staat.

**Download alles** geeft één ZIP met een map per document (Markdown plus
figuren) en `overzicht.csv`: per bestand het pad, het formaat, hoeveel er is
omgezet en nagekeken, waarschuwingen, wat er gemaskeerd is en de status, ook
van bestanden die mislukten. De CSV opent direct in een Nederlandse Excel.

Op de desktop doet de CLI hetzelfde met een map: `uv run python/cli.py
beleidsstukken/ -o out/`.

## Waarom dit niet gewoon "tekst kopiëren" is

Deze documenten komen uit InDesign: kolommen, kaders, citaten in groot
corps, paginabrede kaarten, infographics. Wie er platte tekst uit trekt krijgt de
leesvolgorde door elkaar en verliest elke kaart. Wat deze tool daaraan doet:

- **Leesvolgorde en kolommen** — via `pymupdf4llm`, dat meerkolomspagina's
  herkent en tabellen naar Markdown omzet.
- **Koppenstructuur uit de bladwijzers** — de echte hiërarchie van het document
  in plaats van een gok op lettergrootte, met terugval op lettergrootte waar het
  document geen bladwijzer heeft. Koppen die de vormgeving over twee regels heeft
  gebroken worden weer aan elkaar geplakt.
- **Vet dat geen vet is** — Rotterdams huisstijlfont heet *Bolder*, en PyMuPDF
  leidt "vet" af uit de fontnaam. Daardoor is 99,9% van de tekst als vet
  gemarkeerd en betekent `**` niets meer. De tool meet dat en haalt de markering
  dan weg.
- **Tekst die door vormgeving werd opgeslokt** — de belangrijkste vondst bij het
  bouwen. Een pagina met vijf uitgelichte kaders levert standaard vijf plaatjes
  op en géén van de tekst die erin staat: op dit document 8.000 tekens echte
  beleidstekst op zes pagina's, stilzwijgend weg. Elke pagina wordt daarom
  vergeleken met wat PyMuPDF er zelf aan tekst op ziet; blijft de opbrengst
  achter, dan wordt alleen die pagina opnieuw gedaan met vectorvormen
  uitgeschakeld. Tekstopbrengst over het hele document gaat daarmee van 92% naar
  **98,1%**, en er is geen pagina meer die meer dan de helft van zijn tekst
  kwijtraakt. Kost ongeveer 1 seconde op 173 pagina's.
- **Nep-tabellen** — een rond diagram is opgebouwd uit lijnen, precies waar
  tabeldetectie op afgaat. Zulke blokken komen eruit als `|Col1|Col3|`-soep;
  die worden herkend en terugveranderd in tekst, met een waarschuwing.
- **Stuurtekens** — kapotte glyph-mappings leveren soms een BEL-teken op in plaats
  van een aanhalingsteken. Onzichtbaar in een editor, rommel voor wat het daarna
  leest; die worden verwijderd.
- **Kaarten en figuren** — als losse afbeelding, met de labels die *in* de kaart
  staan eronder (`_Labels in figuur:_ Rozenburg; Hoek van Holland; …`). Een
  model ziet dan niet alleen een plaatje, maar ook waar het over gaat.
- **Terugkerende kop- en voetregels** — worden per document automatisch opgemeten
  en weggesneden.
- **Waarschuwingen** — pagina's zonder tekstlaag (scans), pagina's met opvallend
  weinig tekst, en gedetecteerde tabellen worden gemarkeerd zodat je weet waar je
  moet kijken.

Wat de tool **niet** doet: OCR. PyMuPDF's eigen OCR heeft een Tesseract-binary
nodig die niet in de WebAssembly-build zit. Pagina's zonder tekstlaag worden
daarom gemeld, niet stilzwijgend leeg gelaten.

## Persoonsgegevens maskeren

Gebaseerd op de [Maskeringsscript](https://github.com/minbzk/maskeringsscript)
van de gemeente Rotterdam, maar met een andere afweging. Die script is gemaakt
voor klachtteksten en zaaknotities, waar elke datum, elk bedrag en elk lang
getal over een persoon kan gaan, en maskeert ze daarom allemaal. In een
beleidsstuk zijn dat juist de inhoud: "€ 2,5 miljoen", "per 1 januari 2030",
"150000 inwoners". Daarom:

| Standaard aan | Zelf aan te zetten |
|---|---|
| e-mail, telefoon, IBAN, BSN (met 11-proef), creditcard (met Luhn-controle), kenteken, postcode | namen, adressen (straat + huisnummer), geboortedata en leeftijden |

Wat er bewust blijft staan: bedragen, datums en jaartallen, aantallen,
straatnamen zonder huisnummer (de plek van een project), het servicenummer
14 010, en Markdown-structuur zoals links en figuurpaden. Een paar keuzes
die het verschil maken:

- *Telefoon* vraagt tien cijfers met de eerste twee aaneen. Het patroon van de
  script leest ook een grafiekas uit een PDF ("0 25 50 75 100") als nummer.
- *Postcode* telt bij een jaartal of eenheid ("2030 EU", "1500 MW") alleen
  met een plaatsnaam of huisnummer erachter.
- *Namen* alleen na een aanhef (dhr., mevrouw), met twee voorletters of een
  tussenvoegsel ("J.P. Jansen", "R. van der Wulp"), of onder een groet. Eén
  voorletter en een woord is vaker een kopje ("A. Inleiding"). Straten naar
  personen ("Mr. Visserplein") blijven staan.
- *Datums* alleen als geboortedatum ("geboren op …") en leeftijden ("37 jaar
  oud"); de script maskeert elke datum.
- Gemaskeerd wordt als `[Telefoon]`, niet `<Telefoon>`: dat laatste is in
  Markdown een HTML-tag en verdwijnt in een viewer.

Met maskeren aan verdwijnt ook de auteur uit de front matter; die staat
`anonymized: [...]` met de gebruikte categorieën. Per pagina toont het
nakijkscherm wat er gemaskeerd is.

Niet overgenomen uit de script: naamherkenning met het GLiNER-model (PyTorch
draait niet in de browser) en de woordenlijsten met voor- en achternamen en
nationaliteiten, die op beleidstekst gewone woorden en inhoud maskeren
("Visser", "Bakker", "Turkse").

**Maskeren is nooit volledig.** Een naam zonder aanhef of voorletters
("Fatima belde") wordt niet herkend, en afbeeldingen (foto's, gescande
handtekeningen) worden niet gemaskeerd. Kijk het resultaat na voordat je het
deelt.

## Word en PowerPoint

Een `.docx` of `.pptx` is een zip met XML die al zegt wat alles is: een kop is
een alinea met een kopstijl, een lijst heeft een nummering, een tabel is een
tabel. Er valt dus geen opmaak terug te rekenen zoals bij PDF. Deze bestanden
worden gelezen met eigen code op de Python-standaardbibliotheek
(`python/docx_convert.py`, `python/pptx_convert.py`): geen extra download,
geen extra afhankelijkheid.

PyMuPDF kan beide formaten ook openen, maar verliest daarbij te veel: bij Word
de lijsttekens, tabellen, vet en afbeeldingen; bij PowerPoint de grenzen tussen
dia's, de tabellen, grafieken en notities. Daarom wordt het hier niet gebruikt.

**Word** — koppen, geneste lijsten (ook via Word's lijststijlen), tabellen met
samengevoegde cellen, vet en cursief, links, afbeeldingen met alt-tekst en
voetnoten. Bijgehouden wijzigingen: invoegingen tellen mee, verwijderingen niet.
Een automatische inhoudsopgave wordt overgeslagen (alleen paginanummers en
puntjes), kop- en voetteksten worden niet gelezen. Omdat Word geen vaste
pagina's heeft, wordt het document voor het nakijken opgedeeld in hoofdstukken
bij de hoogste kop die het gebruikt.

**PowerPoint** — één hoofdstuk per dia (`## Dia 4: titel`), in de volgorde van
de presentatie. Wat er voor PowerPoint bij komt:

- *Leesvolgorde.* Tekstvakken staan in het bestand in tekenvolgorde, niet in
  leesvolgorde. Ze worden gesorteerd op positie: van boven naar onder, en
  binnen een rij van links naar rechts.
- *Opsommingstekens via sjabloon.* Of een alinea een opsommingsteken heeft,
  bepaalt meestal de indeling of het hoofdsjabloon. Het Rotterdamse sjabloon
  gebruikt alineaniveaus als tekststijl (intro, quote, bijschrift) en alleen een
  paar niveaus als echte opsomming; zonder die overerving te volgen wordt een
  dia een lijst van negen niveaus diep.
- *Grafieken als tabel.* De getallen in een grafiek staan in het bestand, en
  komen eruit als Markdown-tabel in plaats van als plaatje dat een model niet
  kan lezen. Het zijn de waarden die PowerPoint het laatst toonde; is de
  gekoppelde Excel daarna gewijzigd zonder de grafiek bij te werken, dan wijken
  ze af.
- *Sprekersnotities* als citaatblok. Vaak staat het eigenlijke verhaal daar,
  soms ook wat niet voor iedere lezer bedoeld is. Ze staan standaard aan en
  zijn uit te zetten; de front matter vermeldt `speaker_notes: true|false`.
- Voettekst, datum en dianummer worden overgeslagen; verborgen dia's worden
  meegenomen en gemarkeerd.

Niet ondersteund: de oude formaten `.doc` en `.ppt`, en bestanden met een
wachtwoord (beide worden herkend en gemeld). SmartArt levert alleen de tekst,
ingesloten objecten (Excel, Visio) alleen een waarschuwing.

Bij het nakijken staat er geen weergave van het origineel naast: PyMuPDF tekent
deze formaten niet getrouw, en een verkeerd plaatje is voor een nakijker erger
dan geen. Let bij dia's vooral op de volgorde van tekstvakken en op de getallen
in grafieken.

## Gebruiken

Open de gepubliceerde pagina, of lokaal:

```bash
python -m http.server 8765
# http://127.0.0.1:8765/
```

De eerste keer downloadt de pagina ongeveer 32 MB engine (Pyodide + PyMuPDF).
Daarna staat het in de cache van de browser en werkt alles zonder internet.

Een document van 173 pagina's duurt ongeveer een minuut.

## Controleren dat er niets weggaat

Niet op ons woord vertrouwen — het is te controleren:

1. Open de pagina, wacht tot de engine geladen is.
2. DevTools → Network → zet **Offline** aan.
3. Zet een PDF in het venster. De omzetting werkt gewoon.

De pagina zet daarnaast in `index.html` een Content Security Policy met
`connect-src 'self'`. Daarmee weigert de *browser zelf* elk verzoek naar een
andere host; het is geen belofte in een README maar een afdwingbare regel. Onder
Network blijft de lijst na het laden leeg.

## Hoe het in elkaar zit

Geen buildstap, geen npm in het eindproduct. De bestanden die er staan zijn de
bestanden die de browser krijgt.

```
index.html               pagina, CSP, opties
app.css
js/main.js               bestanden binnenhalen, wachtrij, export
js/worker.js             Pyodide starten, per pagina resultaat terugsturen
js/review.js             pagina naast Markdown, aanpasbaar
js/units.js              pagina / hoofdstuk / dia in de interface
js/export.js             ZIP (via de ingebouwde CompressionStream van de browser)
python/convert.py        PDF-omzetting — zonder browser-afhankelijkheden
python/docx_convert.py   Word-omzetting, standaardbibliotheek
python/pptx_convert.py   PowerPoint-omzetting, standaardbibliotheek
python/office.py         geeft Word en PowerPoint dezelfde interface als PDF
python/anonymize.py      persoonsgegevens maskeren (zie hierboven)
python/data/             Rotterdamse straatnamen, voor het maskeren van adressen
js/intake.js             mappen inlezen, rommelbestanden overslaan
python/bridge.py         de laag die de omzetting aan JavaScript koppelt
python/cli.py            dezelfde logica op de desktop, via uv
vendor/             Pyodide, PyMuPDF, pymupdf4llm, tabulate
tests/              testharnassen (zie onder)
```

Twee dingen zijn met opzet zo:

- **Zwaar werk in een Web Worker.** Pyodide starten en een document omzetten
  blokkeren seconden tot minuten. Pagina's komen één voor één terug, zodat je
  pagina 1 kunt nakijken terwijl pagina 200 nog loopt.
- **`convert.py` weet niet dat er een browser is.** Daardoor draait exact
  dezelfde code op de desktop via `uv`, en dat is waar aan omzetkwaliteit
  gewerkt kan worden zonder browser in de weg.

### Versies zijn aan elkaar gekoppeld

Pyodide 314.0.3 is Python 3.14 met ABI `2026_0`, en de PyMuPDF-wheel in
`vendor/pyodide/` is daarvoor gebouwd. De wheel op PyPI (1.28.0) is `cp313` /
ABI `2025_0` en laadt hier **niet**. Bij het bijwerken van Pyodide moet de
PyMuPDF-wheel uit dezelfde distributie komen, en moeten de pins in `python/cli.py`
mee, anders wijken browser en CLI stil van elkaar af.

## Testen

```bash
# omzetlogica op de desktop, snelste manier om aan kwaliteit te werken
uv run python/cli.py test-pdfs/omgevingsvisie.pdf -o out/
uv run python/cli.py test-pdfs/omgevingsvisie.pdf --pages 40-70 --stdout

# testdocument met de lastige gevallen (kolommen, kaart, tabel, scan-pagina)
uv run tests/make-sample.py

# maskeren: wat gemaskeerd moet worden, en beleidstekst die moet blijven staan
python tests/anonymize-check.py

# Word- en PowerPoint-testdocumenten maken en de uitvoer controleren
# (voetnoten, wijzigingen bijhouden, lijststijlen, kolommen, grafiek, notities)
uv run tests/office-check.py
uv run python/cli.py test-pdfs/sample.pptx --stdout

# wheels + API onder Pyodide, zonder browser
node tests/node-spike.mjs test-pdfs/sample.pdf

# end-to-end in een echte browser
python -m http.server 8765 &
npm install --no-save playwright
node tests/browser-test.mjs test-pdfs/sample.pdf
node tests/browser-test.mjs test-pdfs/sample.docx
node tests/browser-test.mjs test-pdfs/sample.pptx
```

`browser-test.mjs` gebruikt standaard Edge; met `PW_CHROMIUM=<pad naar
chromium>` draait het op een andere Chromium.

`browser-test.mjs` controleert de dingen die alleen een echte browser kan
aantonen: dat de CSP Pyodide niet blokkeert, dat de worker start, dat een echt
document omzet, dat de ZIP een geldig archief is, dat een aanpassing in de review
ook in de download staat, dat omzetten offline werkt, en dat er **nul** verzoeken
naar een andere host gaan.

Het draait ook de CLI en vergelijkt de uitvoer met wat de browser downloadt.
In de praktijk is die byte voor byte gelijk, maar de test eist dat niet: de
font- en glyphcaches van MuPDF verschillen tussen een verse desktopprocess en
een browsersessie die al honderden pagina's heeft gedaan, en dat wisselt af en
toe de volgorde van een figuurverwijzing en het tekstblok ernaast. De test
vergelijkt daarom regelinhoud en eist 99,5% overeenkomst. Dat vangt nog steeds
waar het om gaat — een marge-instelling die niet is toegepast, bijvoorbeeld,
levert honderden niet-overeenkomende kopregels op.

Chromium downloaden wordt op werkplekken vaak door Group Policy geblokkeerd; de
test gebruikt daarom de geïnstalleerde Edge (`channel: "msedge"`).

`test-pdfs/` staat in `.gitignore` — zet daar je eigen documenten in.

## Publiceren op GitHub Pages

Geen buildstap, dus: Settings → Pages → Deploy from a branch → `main` / root.
Er is geen Actions-workflow nodig. GitHub Pages levert `.wasm` met het juiste
MIME-type.

De repository bevat ongeveer 32 MB binaries in `vendor/`. Dat past ruim binnen de
limieten van GitHub.

## Licentie

AGPL-3.0 — zie [LICENSE](LICENSE) en [THIRD_PARTY.md](THIRD_PARTY.md). Dat is geen
keuze maar een gevolg: MuPDF is AGPL, en deze pagina stuurt die code naar de
browser van de bezoeker. Een niet-publieke afgeleide heeft een commerciële
licentie van Artifex nodig.
