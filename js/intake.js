// Getting files in: dropped folders, and what to leave out of them.

// Office leaves "~$rapport.docx" lock files next to every open document, and
// macOS leaves "._rapport.pdf" and ".DS_Store" in every folder it touches.
// None of them is a document; in a dropped folder they would show up as
// conversions that fail for no reason the user can see.
export const isJunkFile = (name) => /^(~\$|\._|\.)/.test(name) || /^thumbs\.db$/i.test(name);

/**
 * Every file in a drop, descending into folders.
 *
 * DataTransfer.files lists a dropped folder as one useless entry; the entry
 * API is the only way to see inside it. Falls back to the flat list where the
 * entry API is missing.
 *
 * @returns {Promise<{files: File[], paths: Map<File, string>}>}
 */
export async function filesFromDrop(dataTransfer) {
  const entries = [...(dataTransfer.items ?? [])]
    .filter((item) => item.kind === "file")
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean);
  if (!entries.length) return { files: [...(dataTransfer.files ?? [])], paths: new Map() };

  const files = [];
  const paths = new Map();
  const walk = async (entry) => {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      files.push(file);
      paths.set(file, entry.fullPath.replace(/^\//, ""));
    } else if (entry.isDirectory && !isJunkFile(entry.name)) {
      const reader = entry.createReader();
      // readEntries hands out a directory in batches (100 in Chromium) and
      // signals the end with an empty one.
      for (;;) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await walk(child);
      }
    }
  };
  for (const entry of entries) await walk(entry);
  return { files, paths };
}
