// Pyodide + PyMuPDF live here so the main thread stays responsive: booting the
// runtime takes seconds and a 200-page document takes minutes, all of it
// synchronous Python. Pages are posted back one at a time so the UI can show
// page 1 while page 200 is still converting.
import { loadPyodide } from "../vendor/pyodide/pyodide.mjs";

const WHEELS = [
  "../vendor/wheels/tabulate-0.10.0-py3-none-any.whl",
  "../vendor/wheels/pymupdf4llm-0.3.4-py3-none-any.whl",
];
const PY_SOURCES = ["../python/convert.py", "../python/bridge.py"];

let py = null;
let bridge = null;
const cancelled = new Set();

const say = (message) => self.postMessage(message);
const boot = (stage, pct) => say({ type: "boot-progress", stage, pct });

async function init() {
  boot("Python-runtime laden", 5);
  py = await loadPyodide({
    indexURL: new URL("../vendor/pyodide/", import.meta.url).href,
    stdout: (line) => say({ type: "log", line }),
    stderr: (line) => say({ type: "log", line }),
  });

  // PyMuPDF by name: it is listed in pyodide-lock.json, so it resolves against
  // indexURL -- our vendored copy, built for this exact Pyodide ABI. The wheel
  // published on PyPI targets a different ABI and will not load here.
  boot("PDF-engine laden (17 MB)", 25);
  await py.loadPackage("pymupdf");

  // The remaining wheels are pure Python, i.e. plain zips, so unpacking them
  // onto sys.path is what installing them means. Deliberately not micropip:
  // that resolves dependencies over the network, and this page makes no
  // network requests once loaded.
  boot("Markdown-omzetter laden", 70);
  py.FS.mkdirTree("/pkgs");
  for (const url of WHEELS) {
    const bytes = new Uint8Array(await (await fetch(new URL(url, import.meta.url))).arrayBuffer());
    py.FS.writeFile(`/pkgs/${url.split("/").pop()}`, bytes);
  }
  for (const url of PY_SOURCES) {
    const text = await (await fetch(new URL(url, import.meta.url))).text();
    py.FS.writeFile(`/pkgs/${url.split("/").pop()}`, text);
  }

  py.runPython(`
import pathlib, sys, zipfile
for whl in sorted(pathlib.Path("/pkgs").glob("*.whl")):
    zipfile.ZipFile(whl).extractall("/pkgs")
    whl.unlink()
if "/pkgs" not in sys.path:
    sys.path.insert(0, "/pkgs")
`);

  boot("Gereed", 95);
  bridge = py.pyimport("bridge");
  const version = py.runPython("import pymupdf, pymupdf4llm; f'{pymupdf.version[0]}/{pymupdf4llm.__version__}'");
  say({ type: "ready", version });
}

// Python dicts -> plain JS objects, with bytes arriving as Uint8Array.
function toJs(proxy) {
  const value = proxy.toJs({ dict_converter: Object.fromEntries });
  proxy.destroy();
  return value;
}

function transferables(page) {
  const list = [];
  if (page.preview) list.push(page.preview.buffer);
  for (const figure of page.figures ?? []) list.push(figure.bytes.buffer);
  return list;
}

async function convert({ id, bytes, opts }) {
  const info = toJs(bridge.start(id, bytes, py.toPy(opts)));
  say({ type: "started", id, ...info });

  try {
    for (let number = 1; number <= info.pages; number++) {
      const page = toJs(bridge.page(id, number));
      // Transfer the pixel buffers instead of copying: a 200-page document is
      // hundreds of megabytes of PNG that would otherwise be duplicated.
      self.postMessage({ type: "page", id, page }, transferables(page));
      // Yield so a cancel message can be seen between pages.
      await new Promise((resolve) => setTimeout(resolve, 0));
      if (cancelled.has(id)) break;
    }
    say({ type: "done", id, cancelled: cancelled.has(id) });
  } finally {
    cancelled.delete(id);
    bridge.finish(id);
  }
}

self.onmessage = async (event) => {
  const message = event.data;
  try {
    switch (message.type) {
      case "init":
        await init();
        break;
      case "probe":
        say({ type: "probed", id: message.id, ...toJs(bridge.probe(message.bytes)) });
        break;
      case "convert":
        await convert(message);
        break;
      case "cancel":
        cancelled.add(message.id);
        break;
      case "join":
        say({
          type: "joined",
          id: message.id,
          markdown: bridge.join_pages(
            message.frontMatter,
            py.toPy(message.pages),
            message.separators,
          ),
        });
        break;
      default:
        say({ type: "error", message: `unknown message: ${message.type}` });
    }
  } catch (error) {
    say({ type: "error", id: message.id, message: String(error?.message ?? error) });
  }
};
