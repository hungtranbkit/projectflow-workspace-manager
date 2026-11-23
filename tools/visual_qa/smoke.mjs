#!/usr/bin/env node
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { chmod, cp, mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const uifix = path.join(root, ".runtime", "uifix-v1.2.6");
const requireFromUIFix = createRequire(path.join(uifix, "package.json"));
const playwrightFrom = process.env.VISUAL_QA_PLAYWRIGHT_FROM || root;
let requireFromPlaywright = createRequire(path.join(playwrightFrom, "package.json"));
try { requireFromPlaywright.resolve("playwright"); } catch { requireFromPlaywright = requireFromUIFix; }
const { chromium } = requireFromPlaywright("playwright");
const chromePath = process.env.CHROME_PATH;
const cli = path.join(root, "tools/visual_qa/visual-qa.mjs");
function fixtureHtml(color) {
  return `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{margin:0;width:100%;height:100%;background:${color}}</style></head><body></body></html>`;
}

function invoke(args) {
  return new Promise((resolve, reject) => {
    const child = spawn("node", [cli, ...args], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => child.kill("SIGKILL"), 60000);
    child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (status) => {
      clearTimeout(timeout);
      resolve({ status: status ?? 2, stdout, stderr });
    });
  });
}

const tmp = await mkdtemp(path.join(os.tmpdir(), "visual-qa-smoke-"));
const server = http.createServer((request, response) => {
  const color = request.url === "/wrong" ? "#a53179" : "#18395a";
  response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  response.end(fixtureHtml(color));
});

try {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const url = `http://127.0.0.1:${port}/same`;
  const browser = await chromium.launch({
    headless: true,
    executablePath: chromePath,
  });
  let reference;
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 240 }, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil: "networkidle" });
    reference = await page.screenshot({ type: "png", fullPage: false });
    await page.close();
  } finally {
    await browser.close();
  }
  const referencePath = path.join(tmp, "reference.png");
  await (await import("node:fs/promises")).writeFile(referencePath, reference);

  const exactOut = path.join(tmp, "exact");
  const exact = await invoke(["--reference", referencePath, "--url", url, "--viewport", "320x240", "--out", exactOut, "--threshold", "0"]);
  assert.equal(exact.status, 0, exact.stderr || exact.stdout);
  assert.equal(JSON.parse(await readFile(path.join(exactOut, "metrics.json"), "utf8")).percentDiff, 0);
  for (const file of ["reference.png", "actual.png", "diff.png", "overlay.png", "metrics.json", "report.md"]) {
    assert.ok((await readFile(path.join(exactOut, file))).length > 0, file);
  }

  const wrong = await invoke(["--reference", referencePath, "--url", `http://127.0.0.1:${port}/wrong`, "--viewport", "320x240", "--out", path.join(tmp, "wrong"), "--threshold", "0"]);
  assert.equal(wrong.status, 1, wrong.stderr || wrong.stdout);
  const negativeMetrics = JSON.parse(await readFile(path.join(tmp, "wrong/metrics.json"), "utf8"));
  assert.equal(negativeMetrics.passed, false);

  const repairedOut = path.join(tmp, "repaired");
  const repaired = await invoke(["--reference", referencePath, "--url", url, "--viewport", "320x240", "--out", repairedOut, "--threshold", "0"]);
  assert.equal(repaired.status, 0, repaired.stderr || repaired.stdout);
  const repairedMetrics = JSON.parse(await readFile(path.join(repairedOut, "metrics.json"), "utf8"));
  assert.equal(repairedMetrics.percentDiff, 0);
  const baselineMetrics = JSON.parse(await readFile(path.join(exactOut, "metrics.json"), "utf8"));
  assert.equal(repairedMetrics.referenceSha256, baselineMetrics.referenceSha256);

  const missing = await invoke(["--reference", path.join(tmp, "missing.png"), "--url", url, "--viewport", "320x240", "--out", path.join(tmp, "missing")]);
  assert.equal(missing.status, 2);

  const wrongSize = await invoke(["--reference", referencePath, "--url", url, "--viewport", "321x240", "--out", path.join(tmp, "wrong-size")]);
  assert.equal(wrongSize.status, 2);
  assert.match(wrongSize.stderr, /REFERENCE_DIMENSIONS_MISMATCH/);

  await new Promise((resolve) => server.close(resolve));
  const unavailable = await invoke(["--reference", referencePath, "--url", `http://127.0.0.1:${port}/same`, "--viewport", "320x240", "--out", path.join(tmp, "unavailable")]);
  assert.equal(unavailable.status, 2);
  assert.match(unavailable.stderr, /SERVER_NOT_READY/);
  console.log(`PASS visual-qa smoke: positive ${baselineMetrics.percentDiff}; negative ${negativeMetrics.percentDiff}; repaired ${repairedMetrics.percentDiff}; reference SHA unchanged; missing reference, dimension mismatch, unavailable server`);
} finally {
  if (server.listening) await new Promise((resolve) => server.close(resolve));
  if (process.env.VISUAL_QA_SMOKE_KEEP === "1") {
    const artifactRoot = process.env.VISUAL_QA_SMOKE_ARTIFACTS;
    if (artifactRoot) {
      await mkdir(artifactRoot, { recursive: true });
      const preserved = path.join(artifactRoot, path.basename(tmp));
      await cp(tmp, preserved, { recursive: true });
      await chmod(preserved, 0o755);
      console.log(`Preserved smoke artifacts: ${preserved}`);
    } else {
      console.log(`Preserved smoke artifacts: ${tmp}`);
    }
  } else {
    await rm(tmp, { recursive: true, force: true });
  }
}
