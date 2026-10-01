// Wiring: file intake, worker orchestration, queue state, export.
import { Review } from "./review.js";
import { csvTable, download, makeZip, referencedFigures, uniqueName } from "./export.js";
import { ACCEPTED, LEGACY_OFFICE, unitOf } from "./units.js";
import { filesFromDrop, isJunkFile } from "./intake.js";

const el = (id) => document.getElementById(id);

const worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });

/** @type {Map<string, object>} docId -> document state */
const docs = new Map();
let queue = [];
let activeId = null;
let ready = false;
let nextId = 1;

// Probes run one at a time. A folder of eighty PDFs would otherwise be read
// into memory all at once and handed to the worker in one go.
const probeQueue = [];
let probing = null;

const review = new Review(refreshExportNote);

// ---------------------------------------------------------------- worker in

worker.onmessage = ({ data }) => {
  switch (data.type) {
    case "boot-progress":
      el("boot-bar").style.width = `${data.pct}%`;
      el("boot-stage").textContent = data.stage;
      break;

    case "ready":
      ready = true;
      el("boot").classList.add("hidden");
      el("pick").classList.remove("hidden");
      el("engine-badge").textContent = `PyMuPDF ${data.version}`;
      probeNext();
      pump();
      break;

    case "probed": {
      if (probing === data.id) probing = null;
      probeNext();
      const doc = docs.get(data.id);
      if (!doc) break;
      // Each document keeps its own detected margins: a batch can mix layouts,
      // and giving document 2 document 1's header height would crop the wrong
      // strip off every page.
      doc.suggested = { top: data.margin_top ?? 0, bottom: data.margin_bottom ?? 0 };
      doc.pageCount = data.pages;
      doc.kind = data.kind;
      doc.unit = data.unit;
      // Margins only exist for PDFs; a Word file in the batch should not reset
      // the fields to zero under a PDF that is still waiting.
      if (data.kind === "pdf" && !el("options").dataset.touched) {
        el("opt-margin-top").value = String(doc.suggested.top);
        el("opt-margin-bottom").value = String(doc.suggested.bottom);
      }
      const units = `${data.pages} ${data.pages === 1 ? unitOf(doc).one : unitOf(doc).many}`;
      const outline = data.kind === "pdf" ? `, ${data.toc_entries} bladwijzers` : "";
      setState(doc, `${units}${outline} — in wachtrij`);
      // Queued only now: starting before the probe lands would convert with
      // margins of zero and leave running headers in the Markdown.
      queue.push(doc.id);
      pump();
      break;
    }

    case "started": {
      const doc = docs.get(data.id);
      if (!doc) break;
      Object.assign(doc, {
        kind: data.kind,
        unit: data.unit,
        pageCount: data.pages,
        slug: data.slug,
        frontMatter: data.front_matter,
        pages: new Array(data.pages).fill(null),
        emphasisStripped: data.emphasis_stripped,
        boldCoverage: data.bold_coverage,
      });
      doc.li.classList.add("q-active");
      // Open the first document automatically, but never pull the reviewer
      // away from one they are working on when the next in a batch starts.
      if (!review.doc) review.attach(doc);
      break;
    }

    case "page": {
      const doc = docs.get(data.id);
      if (!doc) break;
      const page = data.page;
      doc.pages[page.number - 1] = page;
      for (const figure of page.figures ?? []) doc.figures.set(figure.path, figure.bytes);
      doc.converted = page.number;
      if (page.warnings?.length) doc.warned++;
      for (const n of Object.values(page.masked ?? {})) doc.masked += n;
      setState(
        doc,
        `${page.number}/${doc.pageCount} ${unitOf(doc).many}` +
          (doc.warned ? ` — ${doc.warned} met waarschuwing` : ""),
        (page.number / doc.pageCount) * 100,
      );
      if (review.doc?.id === data.id) review.pageArrived(page.number);
      refreshExportNote();
      if (page.number === 1) refreshQueueNote();
      break;
    }

    case "done": {
      const doc = docs.get(data.id);
      if (doc) {
        doc.li.classList.remove("q-active");
        doc.finished = true;
        const note = [];
        if (doc.warned) note.push(`${doc.warned} ${unitOf(doc).many} met waarschuwing`);
        if (doc.emphasisStripped) note.push("vet weggehaald (huisstijlfont)");
        if (doc.masked) note.push(`${doc.masked} gemaskeerd`);
        setState(
          doc,
          (data.cancelled ? "gestopt" : "klaar") + (note.length ? ` — ${note.join(", ")}` : ""),
          100,
        );
      }
      activeId = null;
      el("cancel").classList.add("hidden");
      refreshQueueNote();
      pump();
      break;
    }

    case "joined": {
      pendingJoins.get(data.id)?.resolve(data.markdown);
      pendingJoins.delete(data.id);
      break;
    }

    case "log":
      // Python's stdout/stderr. pymupdf4llm prints a suggestion about an
      // optional add-on we deliberately do not ship; not worth surfacing.
      if (!/pymupdf_layout/.test(data.line)) console.debug("[py]", data.line);
      break;

    case "error":
      showError(data.message);
      if (pendingJoins.has(data.id)) {
        pendingJoins.get(data.id).reject(new Error(data.message));
        pendingJoins.delete(data.id);
        break;
      }
      if (data.id) {
        const doc = docs.get(data.id);
        if (doc) {
          doc.failed = true;
          setState(doc, `mislukt: ${data.message}`);
          doc.li.classList.remove("q-active");
        }
        // A failed probe must not end the conversion that is running.
        if (probing === data.id) {
          probing = null;
          probeNext();
        }
        refreshQueueNote();
        if (activeId === data.id) {
          activeId = null;
          el("cancel").classList.add("hidden");
          pump();
        }
      }
      break;
  }
};

worker.onerror = (event) => showError(event.message || "worker kon niet starten");
worker.postMessage({ type: "init" });

// ---------------------------------------------------------------- intake

// PNG size is not monotonic in dpi on these documents, so these are measured
// pairs rather than a dpi slider -- see the note in python/convert.py.
const FIGURE_PRESETS = {
  compact: { dpi: 110, image_format: "jpg", ignore_images: false },
  sharp: { dpi: 150, image_format: "png", ignore_images: false },
  none: { dpi: 110, image_format: "jpg", ignore_images: true },
};

function readOptions(doc) {
  // Manual entry wins; otherwise each document uses its own detected margins.
  const touched = Boolean(el("options").dataset.marginsTouched);
  const suggested = doc?.suggested ?? { top: 0, bottom: 0 };
  return {
    heading_mode: el("opt-heading").value,
    emphasis: el("opt-emphasis").value,
    margin_top: touched ? Number(el("opt-margin-top").value) || 0 : suggested.top,
    margin_bottom: touched ? Number(el("opt-margin-bottom").value) || 0 : suggested.bottom,
    figure_legends: el("opt-legends").checked,
    page_separators: el("opt-separators").checked,
    speaker_notes: el("opt-notes").checked,
    anonymize: [...document.querySelectorAll("#opt-anon input[data-cat]:checked")].map(
      (input) => input.dataset.cat,
    ),
    previews: true,
    ...(FIGURE_PRESETS[el("opt-figures").value] ?? FIGURE_PRESETS.compact),
  };
}

for (const id of ["opt-margin-top", "opt-margin-bottom"]) {
  el(id).addEventListener("input", () => {
    el("options").dataset.touched = "1";
    el("options").dataset.marginsTouched = "1";
  });
}

/**
 * @param {Iterable<File>} fileList
 * @param {Map<File, string>} [paths] where each file sat in a dropped folder
 */
function addFiles(fileList, paths = new Map()) {
  const all = [...fileList].filter((f) => !isJunkFile(f.name));
  const files = all.filter((f) => f.type === "application/pdf" || ACCEPTED.test(f.name));
  const legacy = all.filter((f) => LEGACY_OFFICE.test(f.name));
  const ignored = all.length - files.length - legacy.length;
  const notes = [];
  if (legacy.length) {
    // The old binary formats are a different file format altogether, not an
    // older version of the same XML; saying so beats a silent skip.
    notes.push(
      `${legacy.map((f) => f.name).join(", ")}: oud Word/PowerPoint-formaat. ` +
        "Open het bestand en sla het op als .docx of .pptx.",
    );
  }
  if (ignored && files.length) notes.push(`${ignored} ander(e) bestand(en) overgeslagen.`);
  if (!files.length && !legacy.length) notes.push("Geen PDF-, Word- of PowerPoint-bestanden in die selectie.");
  if (notes.length) showError(notes.join(" "));
  else hideError();
  if (!files.length) return;
  el("queue-card").classList.remove("hidden");

  for (const file of files) {
    const id = `doc${nextId++}`;
    const doc = {
      id,
      // Kept as a File, not bytes: the browser reads it from disk when it is
      // this document's turn, so a large batch does not sit in memory twice.
      file,
      filename: file.name,
      path: paths.get(file) || file.webkitRelativePath || file.name,
      pageCount: 0,
      pages: [],
      figures: new Map(),
      converted: 0,
      warned: 0,
      masked: 0,
      finished: false,
      failed: false,
      opts: null,
      li: null,
      state: null,
      bar: null,
    };
    renderQueueItem(doc);
    docs.set(id, doc);
    probeQueue.push(id);
  }
  refreshQueueNote();
  probeNext();
}

async function probeNext() {
  if (!ready || probing || !probeQueue.length) return;
  const doc = docs.get(probeQueue.shift());
  if (!doc) return probeNext();
  probing = doc.id;
  try {
    const bytes = new Uint8Array(await doc.file.arrayBuffer());
    // Transferred: the worker's copy is the only one. Queueing for conversion
    // happens when the probe replies -- see the "probed" handler.
    worker.postMessage({ type: "probe", id: doc.id, bytes, filename: doc.filename }, [
      bytes.buffer,
    ]);
  } catch (error) {
    doc.failed = true;
    setState(doc, `mislukt: bestand niet te lezen (${error?.message ?? error})`);
    probing = null;
    probeNext();
  }
}

async function pump() {
  if (!ready || activeId || !queue.length) return;
  const id = queue.shift();
  const doc = docs.get(id);
  if (!doc) return pump();
  activeId = id;
  doc.opts = { ...readOptions(doc), filename: doc.filename };
  setState(doc, "omzetten…", 0);
  el("cancel").classList.remove("hidden");
  let bytes;
  try {
    bytes = new Uint8Array(await doc.file.arrayBuffer());
  } catch (error) {
    doc.failed = true;
    setState(doc, `mislukt: bestand niet te lezen (${error?.message ?? error})`);
    activeId = null;
    return pump();
  }
  // Transfer the bytes: the worker owns them from here.
  worker.postMessage({ type: "convert", id, bytes, opts: doc.opts }, [bytes.buffer]);
}

// ---------------------------------------------------------------- queue UI

function renderQueueItem(doc) {
  const li = document.createElement("li");
  const head = document.createElement("div");
  head.className = "q-head";

  const name = document.createElement("span");
  name.className = "q-name";
  // The path inside a dropped folder tells two "rapport.pdf" files apart.
  name.textContent = doc.path;

  const state = document.createElement("span");
  state.className = "q-state";
  state.textContent = "wachten…";

  const open = document.createElement("button");
  open.className = "secondary small-btn";
  open.type = "button";
  open.textContent = "nakijken";
  open.onclick = () => {
    activeReview(doc.id);
  };

  head.append(name, state, open);

  const progress = document.createElement("div");
  progress.className = "progress";
  const bar = document.createElement("div");
  bar.className = "bar";
  progress.append(bar);

  li.append(head, progress);
  el("queue").append(li);

  doc.li = li;
  doc.state = state;
  doc.bar = bar;
}

function setState(doc, text, pct) {
  if (doc.state) doc.state.textContent = text;
  if (doc.bar && pct != null) doc.bar.style.width = `${pct}%`;
}

function activeReview(id) {
  const doc = docs.get(id);
  if (!doc || !doc.pages.length) return;
  review.attach(doc);
  refreshExportNote();
  el("review-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

el("dropzone").onclick = () => el("file-input").click();
el("dropzone").onkeydown = (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    el("file-input").click();
  }
};
el("file-input").onchange = (event) => {
  addFiles(event.target.files);
  event.target.value = "";
};
el("folder-input").onchange = (event) => {
  addFiles(event.target.files);
  event.target.value = "";
};
el("pick-folder").onclick = () => el("folder-input").click();
el("add-more").onclick = () => el("file-input").click();
el("cancel").onclick = () => {
  if (activeId) worker.postMessage({ type: "cancel", id: activeId });
};

for (const type of ["dragenter", "dragover"]) {
  document.addEventListener(type, (event) => {
    event.preventDefault();
    el("dropzone").classList.add("over");
  });
}
for (const type of ["dragleave", "drop"]) {
  document.addEventListener(type, (event) => {
    event.preventDefault();
    if (type === "dragleave" && event.relatedTarget) return;
    el("dropzone").classList.remove("over");
  });
}
document.addEventListener("drop", async (event) => {
  if (!event.dataTransfer?.items?.length && !event.dataTransfer?.files?.length) return;
  // Read the entries before anything is awaited: the browser empties the
  // DataTransfer as soon as the event handler yields.
  const pending = filesFromDrop(event.dataTransfer);
  try {
    const { files, paths } = await pending;
    addFiles(files, paths);
  } catch (error) {
    showError(`Kon de map niet lezen: ${error?.message ?? error}`);
  }
});

// ---------------------------------------------------------------- export

function currentDoc() {
  return review.doc;
}

function refreshExportNote() {
  const doc = currentDoc();
  if (!doc) return;
  const done = doc.pages.filter(Boolean).length;
  const reviewed = doc.pages.filter((p) => p?.reviewed).length;
  const edited = doc.pages.filter((p) => p && p.edited != null && p.edited !== p.markdown).length;
  const bits = [`${done}/${doc.pageCount} omgezet`, `${reviewed} nagekeken`];
  if (edited) bits.push(`${edited} aangepast`);
  let figureBytes = 0;
  for (const bytes of doc.figures.values()) figureBytes += bytes.length;
  if (figureBytes) bits.push(`${doc.figures.size} figuren, ${formatSize(figureBytes)}`);
  if (done < doc.pageCount) {
    bits.push(`nog niet klaar — download bevat alleen omgezette ${unitOf(doc).many}`);
  }
  el("export-note").textContent = bits.join(" · ");
}

// Joining is done in Python, by the same convert.assemble() the CLI uses. Two
// implementations of "stitch the pages together" would drift, and the CLI is
// the reference we diff browser output against.
const pendingJoins = new Map();

function joinDocument(doc) {
  const id = `join${nextId++}`;
  const pages = doc.pages
    .filter(Boolean)
    .map((page) => ({ number: page.number, markdown: page.edited ?? page.markdown ?? "" }));
  return new Promise((resolve, reject) => {
    pendingJoins.set(id, { resolve, reject });
    worker.postMessage({
      type: "join",
      id,
      frontMatter: doc.frontMatter,
      pages,
      separators: doc.opts?.page_separators ?? true,
      unit: doc.unit ?? "page",
    });
  });
}

async function withBusyButton(button, work) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "bezig…";
  try {
    await work();
  } catch (error) {
    showError(String(error?.message ?? error));
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

el("download-zip").onclick = () =>
  withBusyButton(el("download-zip"), async () => {
    const doc = currentDoc();
    if (!doc) return;
    const entries = [
      { name: `${doc.slug}.md`, content: await joinDocument(doc) },
      ...referencedFigures(doc),
    ];
    download(await makeZip(entries), `${doc.slug}.zip`);
  });

el("download-md").onclick = () =>
  withBusyButton(el("download-md"), async () => {
    const doc = currentDoc();
    if (!doc) return;
    const blob = new Blob([await joinDocument(doc)], { type: "text/markdown;charset=utf-8" });
    download(blob, `${doc.slug}.md`);
  });

// ---------------------------------------------------------------- bulk

function refreshQueueNote() {
  const all = [...docs.values()];
  const finished = all.filter((d) => d.finished).length;
  const failed = all.filter((d) => d.failed).length;
  const bits = [`${all.length} document(en)`, `${finished} klaar`];
  if (failed) bits.push(`${failed} mislukt`);
  el("queue-note").textContent = bits.join(" · ");
  el("download-all").disabled = !all.some((d) => d.pages.some(Boolean));
}

/**
 * A folder name that says which document it is: the path inside the chosen
 * folder, so rapport/sample.pdf and sample.pptx do not both become "sample".
 * The top folder is the one the user picked and the same for every file.
 */
function folderName(doc) {
  const parts = doc.path.split("/");
  const relative = parts.length > 1 ? parts.slice(1).join("/") : doc.path;
  const base = relative
    .replace(/\.(pdf|docx|pptx)$/i, "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return base || doc.slug;
}

/** Totals per category over a document, e.g. "2 telefoon, 1 email". */
function maskedSummary(doc) {
  const totals = {};
  for (const page of doc.pages) {
    for (const [key, n] of Object.entries(page?.masked ?? {})) totals[key] = (totals[key] ?? 0) + n;
  }
  return Object.entries(totals)
    .filter(([, n]) => n)
    .sort((a, b) => b[1] - a[1])
    .map(([key, n]) => `${n} ${key}`)
    .join(", ");
}

// One ZIP for the whole batch: a folder per document with its Markdown and
// figures (figure links are relative, so each folder stands on its own), and
// overzicht.csv listing every document -- including the ones that failed, so
// a batch of eighty does not silently come back as seventy-eight.
el("download-all").onclick = () =>
  withBusyButton(el("download-all"), async () => {
    const entries = [];
    const used = new Set();
    const rows = [
      [
        "bestand", "pad", "formaat", "eenheden", "omgezet", "nagekeken", "aangepast",
        "met waarschuwing", "gemaskeerd", "map in zip", "status",
      ],
    ];
    for (const doc of docs.values()) {
      const pages = doc.pages.filter(Boolean);
      let folder = "";
      if (pages.length && doc.slug) {
        // Same name, different format (sample.pdf, sample.pptx): say which.
        const base = folderName(doc);
        folder = uniqueName(used.has(base) ? `${base}-${doc.kind}` : base, used);
        entries.push({ name: `${folder}/${doc.slug}.md`, content: await joinDocument(doc) });
        for (const figure of referencedFigures(doc)) {
          entries.push({ name: `${folder}/${figure.name}`, content: figure.content });
        }
      }
      rows.push([
        doc.filename,
        doc.path,
        doc.kind ?? "",
        doc.pageCount ? `${doc.pageCount} ${doc.pageCount === 1 ? unitOf(doc).one : unitOf(doc).many}` : "",
        pages.length,
        pages.filter((p) => p.reviewed).length,
        pages.filter((p) => p.edited != null && p.edited !== p.markdown).length,
        doc.warned,
        maskedSummary(doc),
        folder,
        doc.state?.textContent ?? "",
      ]);
    }
    entries.unshift({ name: "overzicht.csv", content: csvTable(rows) });
    const stamp = new Date().toISOString().slice(0, 10);
    download(await makeZip(entries), `markdown-${stamp}.zip`);
  });

// ---------------------------------------------------------------- errors

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} kB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function showError(message) {
  const box = el("error");
  box.textContent = message;
  box.classList.remove("hidden");
}
function hideError() {
  el("error").classList.add("hidden");
}
