// Minimal ZIP writer.
//
// Deliberately not JSZip: the browser already ships deflate via
// CompressionStream, and a hundred kilobytes of minified third-party
// JavaScript is exactly the thing a municipal security review has to take on
// trust. This is the whole format we need -- local headers, central directory,
// end-of-central-directory -- and it is short enough to read.
//
// No ZIP64: a converted document is megabytes, not gigabytes.

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

async function deflateRaw(bytes) {
  if (typeof CompressionStream === "undefined") return null;
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** DOS date/time. Fixed to 1980-01-01 so output is byte-identical run to run. */
const DOS_TIME = 0;
const DOS_DATE = 0x0021;

function bytesOf(content) {
  return typeof content === "string" ? new TextEncoder().encode(content) : content;
}

/**
 * @param {Array<{name: string, content: string|Uint8Array}>} entries
 * @returns {Promise<Blob>}
 */
export async function makeZip(entries) {
  const chunks = [];
  const directory = [];
  let offset = 0;

  for (const entry of entries) {
    const name = new TextEncoder().encode(entry.name);
    const raw = bytesOf(entry.content);
    const crc = crc32(raw);

    // PNGs are already deflated; compressing them again wastes time for ~0%.
    const compressible = !/\.(png|jpe?g|zip)$/i.test(entry.name) && raw.length > 256;
    const packed = compressible ? await deflateRaw(raw) : null;
    const useDeflate = packed !== null && packed.length < raw.length;
    const body = useDeflate ? packed : raw;
    const method = useDeflate ? 8 : 0;

    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true); // local file header
    local.setUint16(4, 20, true); // version needed
    local.setUint16(6, 0x0800, true); // UTF-8 filename flag
    local.setUint16(8, method, true);
    local.setUint16(10, DOS_TIME, true);
    local.setUint16(12, DOS_DATE, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, body.length, true);
    local.setUint32(22, raw.length, true);
    local.setUint16(26, name.length, true);
    local.setUint16(28, 0, true); // extra field length

    chunks.push(new Uint8Array(local.buffer), name, body);

    const central = new DataView(new ArrayBuffer(46));
    central.setUint32(0, 0x02014b50, true); // central directory header
    central.setUint16(4, 20, true); // version made by
    central.setUint16(6, 20, true); // version needed
    central.setUint16(8, 0x0800, true);
    central.setUint16(10, method, true);
    central.setUint16(12, DOS_TIME, true);
    central.setUint16(14, DOS_DATE, true);
    central.setUint32(16, crc, true);
    central.setUint32(20, body.length, true);
    central.setUint32(24, raw.length, true);
    central.setUint16(28, name.length, true);
    central.setUint32(42, offset, true); // offset of local header
    directory.push(new Uint8Array(central.buffer), name);

    offset += 30 + name.length + body.length;
  }

  const directorySize = directory.reduce((sum, part) => sum + part.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); // end of central directory
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, directorySize, true);
  end.setUint32(16, offset, true);

  return new Blob([...chunks, ...directory, new Uint8Array(end.buffer)], {
    type: "application/zip",
  });
}

export function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  // Revoked on the next tick: the click has already started the download, and
  // holding the URL keeps the whole blob in memory.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Only the figures the final Markdown still points at.
 *
 * A reviewer who deletes a decorative full-page image from a page should not
 * find it in the ZIP anyway. Scans the per-page text rather than the joined
 * document so it does not need the worker.
 */
export function referencedFigures(doc) {
  const referenced = new Set();
  for (const page of doc.pages) {
    if (!page) continue;
    const markdown = page.edited ?? page.markdown ?? "";
    // Any alt text: Word and PowerPoint images carry theirs, and an escaped
    // "\]" inside it must not end the match early.
    for (const match of markdown.matchAll(/!\[(?:\\.|[^\]\\])*\]\(([^)]+)\)/g)) {
      referenced.add(match[1]);
    }
  }
  return [...doc.figures.entries()]
    .filter(([path]) => referenced.has(path))
    .map(([path, bytes]) => ({ name: path, content: bytes }));
}

/** `name`, or `name-2`, `name-3`... when a batch has two documents of that name. */
export function uniqueName(name, used) {
  let candidate = name;
  for (let n = 2; used.has(candidate); n++) candidate = `${name}-${n}`;
  used.add(candidate);
  return candidate;
}

/**
 * CSV the way Dutch Excel opens it without an import wizard: semicolons, CRLF,
 * and a byte-order mark so "Bijlage é" is not read as Latin-1.
 */
export function csvTable(rows) {
  const cell = (value) => {
    let text = String(value ?? "");
    // A file name like "=HYPERLINK(...)" would run as a formula in Excel.
    if (/^[=+\-@\t]/.test(text)) text = `'${text}`;
    return /[;"\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return "\ufeff" + rows.map((row) => row.map(cell).join(";")).join("\r\n") + "\r\n";
}
