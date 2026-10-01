// Wiring: file intake, worker orchestration, queue state, export.
import { Review } from "./review.js";
import { download, makeZip, referencedFigures } from "./export.js";
import { ACCEPTED, LEGACY_OFFICE, unitOf } from "./units.js";

const el = (id) => document.getElementById(id);

const worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });

/** @type {Map<string, object>} docId -> document state */
const docs = new Map();
let queue = [];
let activeId = null;
let ready = false;
let nextId = 1;

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
      pump();
      break;

    case "probed": {
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
      review.attach(doc);
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
      setState(
        doc,
        `${page.number}/${doc.pageCount} ${unitOf(doc).many}` +
          (doc.warned ? ` — ${doc.warned} met waarschuwing` : ""),
        (page.number / doc.pageCount) * 100,
      );
      if (activeId === data.id) review.pageArrived(page.number);
      refreshExportNote();
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
        setState(
          doc,
          (data.cancelled ? "gestopt" : "klaar") + (note.length ? ` — ${note.join(", ")}` : ""),
          100,
        );
      }
      activeId = null;
      el("cancel").classList.add("hidden");
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
          setState(doc, `mislukt: ${data.message}`);
          doc.li.classList.remove("q-active");
        }
        activeId = null;
        el("cancel").classList.add("hidden");
        pump();
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

async function addFiles(fileList) {
  const all = [...fileList];
  const files = all.filter((f) => f.type === "application/pdf" || ACCEPTED.test(f.name));
  const legacy = all.filter((f) => LEGACY_OFFICE.test(f.name));
  if (legacy.length) {
    // The old binary formats are a different file format altogether, not an
    // older version of the same XML; saying so beats a silent skip.
    showError(
      `${legacy.map((f) => f.name).join(", ")}: oud Word/PowerPoint-formaat. ` +
        "Open het bestand en sla het op als .docx of .pptx.",
    );
  }
  if (!files.length) {
    if (!legacy.length) showError("Geen PDF-, Word- of PowerPoint-bestanden in die selectie.");
    return;
  }
  if (!legacy.length) hideError();
  el("queue-card").classList.remove("hidden");

  for (const file of files) {
    const id = `doc${nextId++}`;
    const bytes = new Uint8Array(await file.arrayBuffer());
    const doc = {
      id,
      filename: file.name,
      bytes,
      pageCount: 0,
      pages: [],
      figures: new Map(),
      converted: 0,
      warned: 0,
      finished: false,
      opts: null,
      li: null,
      state: null,
      bar: null,
    };
    renderQueueItem(doc);
    docs.set(id, doc);
    // A copy, because the probe transfers nothing and we still need the bytes.
    // Queueing happens when the probe replies -- see the "probed" handler.
    worker.postMessage({ type: "probe", id, bytes: bytes.slice(), filename: file.name });
  }
}

function pump() {
  if (!ready || activeId || !queue.length) return;
  const id = queue.shift();
  const doc = docs.get(id);
  if (!doc) return pump();
  activeId = id;
  doc.opts = { ...readOptions(doc), filename: doc.filename };
  setState(doc, "omzetten…", 0);
  el("cancel").classList.remove("hidden");
  // Transfer the bytes: the worker owns them from here.
  worker.postMessage({ type: "convert", id, bytes: doc.bytes, opts: doc.opts }, [
    doc.bytes.buffer,
  ]);
  doc.bytes = null;
}

// ---------------------------------------------------------------- queue UI

function renderQueueItem(doc) {
  const li = document.createElement("li");
  const head = document.createElement("div");
  head.className = "q-head";

  const name = document.createElement("span");
  name.className = "q-name";
  name.textContent = doc.filename;

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
document.addEventListener("drop", (event) => {
  if (event.dataTransfer?.files?.length) addFiles(event.dataTransfer.files);
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
