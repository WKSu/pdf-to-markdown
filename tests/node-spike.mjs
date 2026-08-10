// Milestone 0 spike: prove PyMuPDF + pymupdf4llm work in Pyodide/WASM.
// Node hosts the same wasm build as the browser, so this validates the wheel
// without a browser in the loop. Run: node tests/node-spike.mjs <pdf>
import { loadPyodide } from "../vendor/pyodide/pyodide.mjs";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const wheels = join(root, "vendor", "wheels");
const pdfPath = process.argv[2] ?? join(root, "test-pdfs", "sample.pdf");
// loadPackage takes a URL; on Windows a bare path parses as scheme "c:".
const wheelUrl = (name) => pathToFileURL(join(wheels, name)).href;

const step = (msg) => console.log(`\n=== ${msg}`);
const t0 = Date.now();
const ms = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;

step("loadPyodide");
const py = await loadPyodide({ indexURL: join(root, "vendor", "pyodide") });
console.log(`ok ${ms()}`);

// pymupdf by NAME: it is in pyodide-lock.json, so Pyodide resolves it against
// indexURL — i.e. our vendored copy, built for this exact Pyodide ABI (2026_0).
// The PyPI wheel is cp313/ABI 2025_0 and will not load here.
step("loadPackage: pymupdf (17.5 MB, from local index)");
await py.loadPackage("pymupdf");
console.log(`ok ${ms()}`);

// pymupdf4llm and tabulate are not in the lock. They are pure-python wheels,
// i.e. plain zips, so unpacking them onto sys.path IS installing them. Avoids
// both micropip (which resolves deps over the network) and loadPackage's URL
// handling, and behaves identically in Node and the browser.
step("install pymupdf4llm + tabulate by unpacking wheels onto sys.path");
py.FS.mkdirTree("/wheels");
for (const name of ["tabulate-0.10.0-py3-none-any.whl", "pymupdf4llm-0.3.4-py3-none-any.whl"]) {
  py.FS.writeFile(`/wheels/${name}`, readFileSync(join(wheels, name)));
}
console.log(
  py.runPython(`
import pathlib, sys, zipfile
target = "/pkgs"
for whl in sorted(pathlib.Path("/wheels").glob("*.whl")):
    zipfile.ZipFile(whl).extractall(target)
if target not in sys.path:
    sys.path.insert(0, target)
sorted(p.name for p in pathlib.Path(target).iterdir() if not p.name.endswith("dist-info"))
`),
);
console.log(`ok ${ms()}`);

step("import pymupdf, pymupdf4llm");
console.log(
  py.runPython(`
import pymupdf, pymupdf4llm
f"pymupdf {pymupdf.__doc__.strip().splitlines()[0] if pymupdf.__doc__ else pymupdf.version} / pymupdf4llm {pymupdf4llm.__version__}"
`),
);

step(`open ${pdfPath}`);
py.FS.writeFile("/in.pdf", readFileSync(pdfPath));
console.log(
  py.runPython(`
doc = pymupdf.open("/in.pdf")
toc = doc.get_toc()
f"pages={doc.page_count} toc_entries={len(toc)} metadata_title={doc.metadata.get('title')!r}"
`),
);

step("get_pixmap -> png bytes  (review UI depends on this)");
console.log(
  py.runPython(`
pix = doc[0].get_pixmap(dpi=100)
png = pix.tobytes("png")
f"{pix.width}x{pix.height} png_bytes={len(png)}"
`),
);

step("to_markdown(page_chunks=True, write_images=True) -> MEMFS");
console.log(
  py.runPython(`
import os, pathlib
os.makedirs("/figures", exist_ok=True)
chunks = pymupdf4llm.to_markdown(
    doc,
    page_chunks=True,
    write_images=True,
    image_path="/figures",
    image_format="png",
    dpi=150,
    force_text=True,
    table_strategy="lines_strict",
    show_progress=False,
)
files = sorted(os.listdir("/figures"))
sizes = [len(pathlib.Path("/figures", f).read_bytes()) for f in files[:3]]
f"chunks={len(chunks)} keys={sorted(chunks[0].keys())}\\nfigure_files={len(files)} first3={files[:3]} sizes={sizes}"
`),
);

step("sample markdown from the densest page");
console.log(
  py.runPython(`
best = max(chunks, key=lambda c: len(c["text"]))
n = best["metadata"]["page"]
imgs = len(best.get("images", []))
tbls = len(best.get("tables", []))
gfx  = len(best.get("graphics", []))
toci = best.get("toc_items", [])
head = best["text"][:1200]
f"page={n} chars={len(best['text'])} images={imgs} tables={tbls} graphics={gfx} toc_items={toci}\\n---\\n{head}"
`),
);

step(`DONE in ${ms()}`);
