// What one reviewable unit is called. The Python side decides the unit
// (office.py: a PDF has pages, a Word document sections, a deck slides); this
// is only how the interface says it.
export const UNITS = {
  page: { one: "pagina", many: "pagina's" },
  section: { one: "hoofdstuk", many: "hoofdstukken" },
  slide: { one: "dia", many: "dia's" },
};

export const unitOf = (doc) => UNITS[doc?.unit] ?? UNITS.page;

// Accepted by extension here so the file picker and drag-and-drop can say no
// early; the worker still decides by the bytes (office.detect_format).
export const ACCEPTED = /\.(pdf|docx|pptx)$/i;
export const LEGACY_OFFICE = /\.(doc|ppt|dot|pot)$/i;
