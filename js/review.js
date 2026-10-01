// Side-by-side review: the rendered PDF page next to its Markdown, editable.
// Word and PowerPoint get the Markdown alone, a section or slide at a time:
// there is no faithful rendering of those to put beside it (see office.py).
//
// This is the part that makes the output trustworthy. Extraction from designed
// documents goes wrong in ways only a human looking at the page can see -- a
// sidebar spliced into a sentence, a heading that was really a pull quote --
// so the tool's job is to make those easy to spot and fix, not to claim it got
// everything right.

import { unitOf } from "./units.js";

const el = (id) => document.getElementById(id);

export class Review {
  constructor(onChange) {
    this.doc = null;
    this.index = 0;
    this.onChange = onChange;
    this.blobUrl = null;

    el("prev").onclick = () => this.go(this.index - 1);
    el("next").onclick = () => this.go(this.index + 1);
    el("prev-warn").onclick = () => this.goWarning(-1);
    el("next-warn").onclick = () => this.goWarning(1);
    el("page-no").onchange = (event) => this.go(Number(event.target.value) - 1);
    el("revert").onclick = () => this.revert();

    el("page-md").addEventListener("input", (event) => {
      const page = this.current;
      if (!page) return;
      page.edited = event.target.value;
      this.paintStats(page);
      this.onChange?.();
    });

    el("page-ok").addEventListener("change", (event) => {
      const page = this.current;
      if (!page) return;
      page.reviewed = event.target.checked;
      this.onChange?.();
    });

    // Alt+arrows page through without stealing arrow keys from the textarea.
    document.addEventListener("keydown", (event) => {
      if (!this.doc || !event.altKey) return;
      if (event.key === "ArrowLeft") this.go(this.index - 1);
      if (event.key === "ArrowRight") this.go(this.index + 1);
    });
  }

  get current() {
    return this.doc?.pages[this.index] ?? null;
  }

  attach(doc) {
    this.doc = doc;
    this.index = 0;
    el("review-card").classList.remove("hidden");
    el("review-title").textContent = `Nakijken — ${doc.filename}`;
    el("page-total").textContent = `/ ${doc.pageCount}`;
    const unit = unitOf(doc);
    el("page-unit").textContent = unit.one;
    el("prev").setAttribute("aria-label", `Vorige ${unit.one}`);
    el("next").setAttribute("aria-label", `Volgende ${unit.one}`);
    el("prev-warn").title = `Vorige ${unit.one} met waarschuwing`;
    el("next-warn").title = `Volgende ${unit.one} met waarschuwing`;
    const preview = (doc.kind ?? "pdf") === "pdf";
    el("review-split").classList.toggle("no-preview", !preview);
    el("preview-note").classList.toggle("hidden", preview);
    el("page-no").max = String(doc.pageCount);
    this.render();
  }

  go(index) {
    if (!this.doc) return;
    this.index = Math.max(0, Math.min(this.doc.pageCount - 1, index));
    this.render();
  }

  goWarning(step) {
    if (!this.doc) return;
    for (let i = this.index + step; i >= 0 && i < this.doc.pageCount; i += step) {
      if (this.doc.pages[i]?.warnings?.length) return this.go(i);
    }
  }

  revert() {
    const page = this.current;
    if (!page) return;
    page.edited = null;
    el("page-md").value = page.markdown ?? "";
    this.paintStats(page);
    this.onChange?.();
  }

  /** Called when a freshly converted page arrives for the document on screen. */
  pageArrived(number) {
    if (this.doc && number - 1 === this.index) this.render();
    el("page-total").textContent = `/ ${this.doc?.pageCount ?? 0}`;
  }

  paintStats(page) {
    const value = el("page-md").value;
    const bits = [`${value.length} tekens`];
    if (page.tables) bits.push(`${page.tables} tabel(len)`);
    if (page.figures?.length) bits.push(`${page.figures.length} figuur(en)`);
    if (page.heading_repairs) bits.push(`${page.heading_repairs} kop hersteld`);
    const masked = Object.entries(page.masked ?? {}).filter(([, n]) => n);
    if (masked.length) bits.push(`gemaskeerd: ${masked.map(([k, n]) => `${n} ${k}`).join(", ")}`);
    if (page.edited != null && page.edited !== page.markdown) bits.push("aangepast");
    el("page-stats").textContent = bits.join(" · ");
  }

  render() {
    const doc = this.doc;
    if (!doc) return;
    const page = doc.pages[this.index];
    el("page-no").value = String(this.index + 1);

    // One blob URL at a time. A 200-page document is hundreds of megabytes of
    // preview PNG; holding every URL would keep all of it alive.
    if (this.blobUrl) {
      URL.revokeObjectURL(this.blobUrl);
      this.blobUrl = null;
    }

    const image = el("page-image");
    if (page?.preview) {
      this.blobUrl = URL.createObjectURL(new Blob([page.preview], { type: "image/png" }));
      image.src = this.blobUrl;
      image.alt = `PDF-pagina ${this.index + 1}`;
    } else {
      image.removeAttribute("src");
      image.alt = page ? "Geen weergave beschikbaar" : `${unitOf(doc).one} wordt nog omgezet`;
    }

    const textarea = el("page-md");
    if (page) {
      textarea.value = page.edited ?? page.markdown ?? "";
      textarea.disabled = false;
      el("page-ok").checked = Boolean(page.reviewed);
      el("page-ok").disabled = false;
      this.paintStats(page);
    } else {
      textarea.value = "";
      textarea.disabled = true;
      el("page-ok").checked = false;
      el("page-ok").disabled = true;
      el("page-stats").textContent = "nog niet omgezet";
    }

    const box = el("page-warnings");
    if (page?.warnings?.length) {
      box.innerHTML =
        "<strong>Let op</strong><ul>" +
        page.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("") +
        "</ul>";
      box.classList.remove("hidden");
    } else {
      box.classList.add("hidden");
    }

    el("prev").disabled = this.index === 0;
    el("next").disabled = this.index >= doc.pageCount - 1;
  }

  detach() {
    if (this.blobUrl) URL.revokeObjectURL(this.blobUrl);
    this.blobUrl = null;
    this.doc = null;
    el("review-card").classList.add("hidden");
  }
}

function escapeHtml(text) {
  return String(text).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}
