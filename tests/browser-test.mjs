// End-to-end browser check against the local server.
//
// Verifies the things only a real browser can tell us: that the CSP does not
// block Pyodide, that the worker boots, that a real document converts, that the
// ZIP is a valid archive -- and, most importantly, that the page makes no
// request to any other host.
//
//   node tests/browser-test.mjs [document.pdf|.docx|.pptx] [--headed]
//
// Uses Edge by default; set PW_CHROMIUM to a Chromium binary to use that
// instead (e.g. PW_CHROMIUM=/opt/pw-browsers/chromium on a Linux box).
import { chromium } from "playwright";
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const ORIGIN = "http://127.0.0.1:8765";
const pdf =
  process.argv.find((a) => /\.(pdf|docx|pptx)$/i.test(a)) ?? "test-pdfs/sample.pdf";
const isPdf = /\.pdf$/i.test(pdf);
const headed = process.argv.includes("--headed");

const browser = await chromium.launch(
  process.env.PW_CHROMIUM
    ? { executablePath: process.env.PW_CHROMIUM, headless: !headed }
    : { channel: "msedge", headless: !headed },
);
const page = await browser.newPage({ acceptDownloads: true });

const cspViolations = [];
const consoleErrors = [];
const offOrigin = [];

page.on("console", (msg) => {
  const text = msg.text();
  if (/Content Security Policy|Refused to/i.test(text)) cspViolations.push(text);
  else if (msg.type() === "error") consoleErrors.push(text);
});
page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err.message}`));
page.on("request", (req) => {
  const url = req.url();
  // blob:/data: are in-memory and same-origin by construction; anything else
  // that is not our own server would mean data leaving the machine.
  if (url.startsWith(ORIGIN) || url.startsWith(`blob:${ORIGIN}`) || url.startsWith("data:")) {
    return;
  }
  offOrigin.push(url);
});
page.on("response", (res) => {
  if (res.status() >= 400) consoleErrors.push(`HTTP ${res.status()} ${res.url()}`);
});

const step = (m) => console.log(`\n=== ${m}`);
const t0 = Date.now();
const ms = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;

step("load page");
await page.goto(`${ORIGIN}/index.html`, { waitUntil: "domcontentloaded" });

step("wait for engine (Pyodide + PyMuPDF boot)");
await page.waitForSelector("#pick:not(.hidden)", { timeout: 180_000 });
console.log(`engine badge: ${await page.textContent("#engine-badge")}  ${ms()}`);

step(`upload ${pdf}`);
await page.setInputFiles("#file-input", pdf);
await page.waitForSelector("#queue-card:not(.hidden)");

step("wait for conversion");
await page.waitForFunction(
  () => /klaar|mislukt|gestopt/.test(document.querySelector(".q-state")?.textContent ?? ""),
  null,
  { timeout: 900_000 },
);
const state = await page.textContent(".q-state");
console.log(`queue state: ${state}  ${ms()}`);
if (!/klaar/.test(state)) {
  console.log(`\nconsole errors:\n${consoleErrors.map((e) => "  ! " + e).join("\n")}`);
  console.log(`\n=== FAIL: conversion did not finish`);
  await browser.close();
  process.exit(1);
}

step("review pane");
console.log(`title:  ${await page.textContent("#review-title")}`);
console.log(`pages:  ${await page.textContent("#page-total")}`);
console.log(`stats:  ${await page.textContent("#page-stats")}`);
const hasImage = await page.evaluate(() => {
  const img = document.getElementById("page-image");
  return Boolean(img?.src?.startsWith("blob:")) && img.naturalWidth > 0;
});
console.log(`page preview rendered: ${hasImage}${isPdf ? "" : " (not expected for Word/PowerPoint)"}`);
const noteShown = await page.isVisible("#preview-note");
console.log(`no-preview note shown: ${noteShown}`);

step("download unedited markdown (for CLI parity diff)");
{
  const wait = page.waitForEvent("download", { timeout: 120_000 });
  await page.click("#download-md");
  await (await wait).saveAs("out/browser-parity.md");
  console.log(`saved out/browser-parity.md`);
}

// The CLI is the reference implementation for conversion quality; if the browser
// drifts from it, one of them is wrong and the CLI is no longer a valid place to
// iterate. Same document, same defaults, so the bytes should match exactly.
step("CLI parity");
let parityOk = false;
try {
  // Fresh directory every run: reading a leftover .md from an earlier document
  // turns this check into a coin toss.
  rmSync("out/parity-check", { recursive: true, force: true });
  execFileSync("uv", ["run", "python/cli.py", pdf, "-o", "out/parity-check"], {
    stdio: "pipe",
  });
  const fromBrowser = readFileSync("out/browser-parity.md", "utf8");
  const slug = /source_file: "(.+?)"/.exec(fromBrowser)?.[1] ?? "";
  const fromCli = readFileSync(
    `out/parity-check/${slug.replace(/\.(pdf|docx|pptx)$/i, "").toLowerCase()}.md`,
    "utf8",
  );
  // Not byte-equality. MuPDF's font and glyph caches differ between a fresh
  // process and one that has already handled hundreds of pages, which
  // occasionally reorders a figure reference relative to an adjacent text block.
  // Content equality is what actually matters, and it still catches the drift
  // worth catching -- a margin setting that failed to apply, say, shows up as
  // hundreds of unmatched running-header lines.
  const lines = (text) =>
    text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  const browserLines = lines(fromBrowser);
  const cliLines = lines(fromCli);
  const cliSet = new Map();
  for (const line of cliLines) cliSet.set(line, (cliSet.get(line) ?? 0) + 1);

  const missing = [];
  for (const line of browserLines) {
    const count = cliSet.get(line) ?? 0;
    if (count > 0) cliSet.set(line, count - 1);
    else missing.push(line);
  }
  const extra = [...cliSet.entries()].flatMap(([line, n]) => Array(n).fill(line));
  const worst = Math.max(missing.length, extra.length);
  const agreement = 1 - worst / Math.max(browserLines.length, cliLines.length);

  parityOk = agreement >= 0.995;
  console.log(
    `browser vs CLI: ${(agreement * 100).toFixed(2)}% of lines agree ` +
      `(${browserLines.length} lines; ${missing.length} only in browser, ${extra.length} only in CLI)` +
      `${fromBrowser === fromCli ? " — byte-identical" : ""}`,
  );
  for (const line of missing.slice(0, 4)) console.log(`  browser only: ${line.slice(0, 110)}`);
  for (const line of extra.slice(0, 4)) console.log(`  CLI only:     ${line.slice(0, 110)}`);
} catch (error) {
  console.log(`could not run the CLI for comparison: ${String(error.message).slice(0, 200)}`);
}

step("navigate and edit");
await page.fill("#page-no", "3");
await page.dispatchEvent("#page-no", "change");
await page.waitForTimeout(300);
const before = await page.inputValue("#page-md");
console.log(`page 3 markdown: ${before.length} chars`);
console.log(`  ${before.slice(0, 160).replace(/\n/g, " / ")}`);
await page.fill("#page-md", before + "\n\nAANGEPAST DOOR REVIEWER");
await page.check("#page-ok");
console.log(`export note: ${await page.textContent("#export-note")}`);

step("download zip");
const wait = page.waitForEvent("download", { timeout: 120_000 });
await page.click("#download-zip");
const dl = await wait;
const zipPath = "out/browser-test.zip";
await dl.saveAs(zipPath);
const zip = readFileSync(zipPath);
console.log(`saved ${dl.suggestedFilename()} -> ${zipPath} (${zip.length.toLocaleString()} bytes)`);
console.log(`zip magic: ${zip.subarray(0, 2).toString("latin1")} (expect PK)`);

step("download md");
const wait2 = page.waitForEvent("download", { timeout: 60_000 });
await page.click("#download-md");
const dl2 = await wait2;
await dl2.saveAs("out/browser-test.md");
const md = readFileSync("out/browser-test.md", "utf8");
writeFileSync("out/browser-test.md", md);
console.log(`markdown: ${md.length.toLocaleString()} chars`);
console.log(`edit present in export: ${md.includes("AANGEPAST DOOR REVIEWER")}`);

// Every figure the Markdown points at must be in the ZIP. Word and PowerPoint
// images carry alt text, which an earlier version of the export did not match.
const wanted = [...md.matchAll(/!\[(?:\\.|[^\]\\])*\]\((figures\/[^)]+)\)/g)].map((m) => m[1]);
const zipText = zip.toString("latin1");
const missingFigures = wanted.filter((name) => !zipText.includes(name));
console.log(`figures referenced: ${wanted.length}, missing from zip: ${missingFigures.length}`);
for (const name of missingFigures.slice(0, 5)) console.log(`  ! ${name}`);

step("offline: convert again with the network cut");
await page.context().setOffline(true);
await page.setInputFiles("#file-input", pdf);
await page.waitForFunction(
  () => document.querySelectorAll(".q-state").length > 1,
  null,
  { timeout: 30_000 },
);
await page.waitForFunction(
  () => {
    const states = [...document.querySelectorAll(".q-state")];
    return /klaar|mislukt|gestopt/.test(states[states.length - 1]?.textContent ?? "");
  },
  null,
  { timeout: 900_000 },
);
const offlineState = await page.evaluate(() => {
  const s = [...document.querySelectorAll(".q-state")];
  return s[s.length - 1].textContent;
});
console.log(`offline conversion: ${offlineState}`);
await page.context().setOffline(false);

step("results");
console.log(`off-origin requests: ${offOrigin.length}`);
for (const url of [...new Set(offOrigin)].slice(0, 10)) console.log(`  ! ${url}`);
console.log(`CSP violations: ${cspViolations.length}`);
for (const v of cspViolations.slice(0, 6)) console.log(`  ! ${v.slice(0, 200)}`);
console.log(`console errors: ${consoleErrors.length}`);
for (const e of consoleErrors.slice(0, 8)) console.log(`  ! ${e.slice(0, 200)}`);

await browser.close();

const failed =
  offOrigin.length > 0 ||
  cspViolations.length > 0 ||
  consoleErrors.length > 0 ||
  !parityOk ||
  (isPdf ? !hasImage || noteShown : hasImage || !noteShown) ||
  missingFigures.length > 0 ||
  !md.includes("AANGEPAST DOOR REVIEWER") ||
  !/klaar/.test(offlineState);
console.log(`\n=== ${failed ? "FAIL" : "PASS"} in ${ms()}`);
process.exit(failed ? 1 : 0);
