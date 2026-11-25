#!/usr/bin/env node
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const UIFIX_DIR = path.join(root, ".runtime", "uifix-v1.2.6");
const UIFIX_SHA = "97c4b9d83684c7d607cb8c4b146e3f8aa5356e53";
const DEFAULT_THRESHOLD = 0.08;

export function parseArgs(argv) {
  const values = {};
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (!key.startsWith("--")) throw new Error(`Unexpected argument: ${key}`);
    const value = argv[++i];
    if (!value || value.startsWith("--")) throw new Error(`Missing value for ${key}`);
    if (key in values) throw new Error(`Duplicate option: ${key}`);
    values[key] = value;
  }
  const allowed = new Set(["--reference", "--url", "--viewport", "--out", "--threshold", "--capture"]);
  for (const key of Object.keys(values)) if (!allowed.has(key)) throw new Error(`Unknown option: ${key}`);
  for (const key of ["--reference", "--url", "--viewport", "--out"]) {
    if (!values[key]) throw new Error(`Required option missing: ${key}`);
  }
  const viewportMatch = /^([0-9]{2,4})x([0-9]{2,4})$/i.exec(values["--viewport"]);
  if (!viewportMatch) throw new Error("--viewport must be WxH, for example 1366x768");
  const viewport = { width: Number(viewportMatch[1]), height: Number(viewportMatch[2]) };
  if (viewport.width < 320 || viewport.width > 3840 || viewport.height < 240 || viewport.height > 2160) {
    throw new Error("--viewport dimensions are outside the supported range 320..3840 by 240..2160");
  }
  const threshold = values["--threshold"] === undefined ? DEFAULT_THRESHOLD : Number(values["--threshold"]);
  if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) throw new Error("--threshold must be between 0 and 1");
  const capture = values["--capture"] || "viewport";
  if (!["viewport", "full-page"].includes(capture)) throw new Error("--capture must be viewport or full-page");
  const url = new URL(values["--url"]);
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1", "::1", "[::1]"].includes(url.hostname) || !url.port) {
    throw new Error("--url must be an http loopback URL with an explicit port");
  }
  return { reference: path.resolve(values["--reference"]), url: url.toString(), viewport, out: path.resolve(values["--out"]), threshold, capture };
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function gitHead(cwd) {
  const result = spawnSync("git", ["rev-parse", "HEAD"], { cwd, encoding: "utf8", timeout: 5000 });
  if (result.status !== 0) throw new Error("Cannot pin visual QA artifacts: current directory is not a Git worktree");
  return result.stdout.trim();
}

function pngDimensions(buffer) {
  if (buffer.length < 24 || buffer.toString("ascii", 1, 4) !== "PNG" || buffer.readUInt32BE(12) !== 0x49484452) {
    throw new Error("Reference must be a valid PNG image");
  }
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

function markdown(metrics, command) {
  const status = metrics.passed ? "PASS" : "FAIL";
  return `# Visual QA ${status}\n\n` +
    `- Tool: UIFix 1.2.6 (${UIFIX_SHA})\n` +
    `- Source commit: \`${metrics.sourceCommit}\`\n` +
    `- Reference SHA-256: \`${metrics.referenceSha256}\`\n` +
    `- Route: \`${metrics.url}\`\n` +
    `- Viewport: ${metrics.viewport.width}x${metrics.viewport.height} at DPR ${metrics.deviceScaleFactor}\n` +
    `- Capture mode/output: ${metrics.captureMode}; ${metrics.imageDimensions.width}x${metrics.imageDimensions.height}\n` +
    `- Pixel mismatch: ${metrics.mismatchedPixels}/${metrics.totalPixels} (${(metrics.percentDiff * 100).toFixed(3)}%); threshold ${(metrics.threshold * 100).toFixed(3)}%\n` +
    `- SSIM: ${Number(metrics.ssim).toFixed(4)} (diagnostic only)\n` +
    `- Horizontal overflow: ${metrics.horizontalOverflow ? "FAIL" : "none"}\n` +
    `- JavaScript/page errors: ${metrics.pageErrors.length}\n` +
    `- External requests blocked: ${metrics.blockedRequests.length}\n\n` +
    `## Replay\n\n\`\`\`sh\n${command}\n\`\`\`\n\n` +
    `## Findings\n\n` +
    (metrics.pageErrors.length ? metrics.pageErrors.map((item) => `- Page error: ${item}`).join("\n") + "\n" : "- No page errors.\n") +
    (metrics.blockedRequests.length ? metrics.blockedRequests.map((item) => `- Blocked non-local request: ${item}`).join("\n") + "\n" : "- No external requests.\n") +
    `\nA pixel threshold is only an automated diagnostic. Review content, typography, responsive behavior, overflow, accessibility and business actions before acceptance.\n`;
}

async function main(argv) {
  if (!existsSync(path.join(UIFIX_DIR, "dist/diff/index.js"))) {
    throw new Error("Pinned UIFix is not installed. Run: python3 tools/visual_qa/bootstrap.py");
  }
  const args = parseArgs(argv);
  const runId = process.env.VISUAL_QA_RUN_ID;
  const expectedCommit = process.env.VISUAL_QA_TESTED_COMMIT;
  const evidenceOutput = process.env.VISUAL_QA_EVIDENCE_OUTPUT;
  if (runId || expectedCommit || evidenceOutput) {
    if (!/^\d+$/.test(runId || "") || !/^[0-9a-f]{40}$/.test(expectedCommit || "") || !evidenceOutput) {
      throw new Error("VISUAL_QA_RUN_ID, VISUAL_QA_TESTED_COMMIT and VISUAL_QA_EVIDENCE_OUTPUT must be provided together");
    }
    if (gitHead(process.cwd()) !== expectedCommit) throw new Error("VISUAL_QA_SOURCE_COMMIT_MISMATCH");
    const runRoot = path.resolve(evidenceOutput);
    const reserved = path.resolve(root, ".runtime", "projectflow-visual-qa");
    if (!runRoot.startsWith(`${reserved}${path.sep}`)) throw new Error("Visual evidence output must be inside the reserved .runtime directory");
    args.out = path.join(runRoot, `${args.viewport.width}x${args.viewport.height}`);
  }
  const reference = await readFile(args.reference);
  const refSize = pngDimensions(reference);
  if (refSize.width !== args.viewport.width || (args.capture === "viewport" && refSize.height !== args.viewport.height)) {
    throw new Error(`REFERENCE_DIMENSIONS_MISMATCH: ${refSize.width}x${refSize.height} != ${args.viewport.width}x${args.viewport.height}`);
  }
  const sourceCommit = gitHead(process.cwd());
  await mkdir(args.out, { recursive: true });

  const requireFromUIFix = createRequire(path.join(UIFIX_DIR, "package.json"));
  let requireFromPlaywright = requireFromUIFix;
  const playwrightFrom = process.env.VISUAL_QA_PLAYWRIGHT_FROM || process.cwd();
  try {
    requireFromPlaywright = createRequire(path.join(playwrightFrom, "package.json"));
    requireFromPlaywright.resolve("playwright");
  } catch {
    requireFromPlaywright = requireFromUIFix;
  }
  const { chromium } = requireFromPlaywright("playwright");
  const sharp = requireFromUIFix("sharp");
  const { diffPipeline } = await import(pathToFileURL(path.join(UIFIX_DIR, "dist/diff/index.js")));
  const chromePath = process.env.CHROME_PATH;
  const browser = await chromium.launch({
    headless: true,
    executablePath: chromePath,
  });
  const pageErrors = [];
  const blockedRequests = [];
  let page;
  let actual;
  let horizontalOverflow = false;
  try {
    const context = await browser.newContext({ viewport: args.viewport, deviceScaleFactor: 1, reducedMotion: "reduce", colorScheme: "light" });
    page = await context.newPage();
    const origin = new URL(args.url).origin;
    await page.route("**/*", async (route) => {
      const requestUrl = route.request().url();
      if (requestUrl.startsWith("data:") || requestUrl.startsWith("blob:") || requestUrl.startsWith("about:")) return route.continue();
      if (new URL(requestUrl).origin !== origin) {
        blockedRequests.push(requestUrl);
        return route.abort("blockedbyclient");
      }
      return route.continue();
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    let response;
    try {
      response = await page.goto(args.url, { waitUntil: "networkidle", timeout: 20000 });
    } catch (error) {
      throw new Error(`SERVER_NOT_READY: ${error.message}`);
    }
    if (!response || !response.ok()) throw new Error(`SERVER_NOT_READY: navigation returned ${response?.status() ?? "no response"}`);
    await page.evaluate(async () => { if (document.fonts?.ready) await document.fonts.ready; });
    await page.waitForTimeout(100);
    horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth || document.body.scrollWidth > window.innerWidth);
    actual = await page.screenshot({ type: "png", fullPage: args.capture === "full-page", animations: "disabled" });
  } finally {
    await page?.context().close().catch(() => undefined);
    await browser.close().catch(() => undefined);
  }

  const actualSize = pngDimensions(actual);
  if (actualSize.width !== args.viewport.width || (args.capture === "viewport" && actualSize.height !== args.viewport.height)) {
    throw new Error(`CAPTURE_DIMENSIONS_MISMATCH: ${actualSize.width}x${actualSize.height} is incompatible with ${args.capture} capture at ${args.viewport.width}x${args.viewport.height}`);
  }
  if (actualSize.width !== refSize.width || actualSize.height !== refSize.height) {
    throw new Error(`CAPTURE_DIMENSIONS_MISMATCH: actual ${actualSize.width}x${actualSize.height} != reference ${refSize.width}x${refSize.height}`);
  }
  const diff = await diffPipeline(actual, reference, args.threshold, 0.95, [], args.viewport);
  const passed = diff.percentDiff <= args.threshold && !horizontalOverflow && pageErrors.length === 0 && blockedRequests.length === 0;
  const metrics = {
    schemaVersion: 1,
    tool: "UIFix",
    toolVersion: "1.2.6",
    toolCommit: UIFIX_SHA,
    sourceCommit,
    reference: args.reference,
    referenceSha256: sha256(reference),
    actualSha256: sha256(actual),
    url: args.url,
    viewport: args.viewport,
    captureMode: args.capture,
    imageDimensions: actualSize,
    deviceScaleFactor: 1,
    threshold: args.threshold,
    percentDiff: diff.percentDiff,
    mismatchedPixels: diff.mismatchedPixels,
    totalPixels: diff.totalPixels,
    ssim: diff.ssim,
    changedRegions: diff.bboxes ?? [],
    horizontalOverflow,
    pageErrors,
    blockedRequests,
    passed,
  };
  await copyFile(args.reference, path.join(args.out, "reference.png"));
  await writeFile(path.join(args.out, "actual.png"), actual);
  await writeFile(path.join(args.out, "diff.png"), diff.diffOverlay);
  await sharp(reference).composite([{ input: actual, blend: "over", opacity: 0.5 }]).png().toFile(path.join(args.out, "overlay.png"));
  await writeFile(path.join(args.out, "metrics.json"), JSON.stringify(metrics, null, 2) + "\n");
  const replay = `node tools/visual_qa/visual-qa.mjs --reference ${JSON.stringify(args.reference)} --url ${JSON.stringify(args.url)} --viewport ${args.viewport.width}x${args.viewport.height} --out ${JSON.stringify(args.out)} --threshold ${args.threshold} --capture ${args.capture}`;
  await writeFile(path.join(args.out, "report.md"), markdown(metrics, replay));
  if (runId) {
    const artifactNames = ["reference.png", "actual.png", "diff.png", "overlay.png", "metrics.json", "report.md"];
    const artifacts = {};
    for (const name of artifactNames) {
      const bytes = await readFile(path.join(args.out, name));
      artifacts[name] = { file: name, size: bytes.length, sha256: sha256(bytes) };
    }
    const manifest = {
      schemaVersion: 1, runId, testedCommit: expectedCommit, sourceCommit,
      tool: "UIFix", toolVersion: "1.2.6", toolCommit: UIFIX_SHA,
      route: args.url, referenceSha256: metrics.referenceSha256,
      viewport: args.viewport, deviceScaleFactor: 1, captureMode: args.capture,
      threshold: args.threshold, passed, metrics: {
        percentDiff: metrics.percentDiff, mismatchedPixels: metrics.mismatchedPixels,
        totalPixels: metrics.totalPixels, ssim: metrics.ssim,
        horizontalOverflow: metrics.horizontalOverflow, pageErrors: metrics.pageErrors,
        blockedRequests: metrics.blockedRequests,
      }, artifacts,
    };
    await writeFile(path.join(args.out, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n", { flag: "wx" });
  }
  console.log(`${passed ? "PASS" : "FAIL"} UIFix diff ${(diff.percentDiff * 100).toFixed(3)}% at ${args.viewport.width}x${args.viewport.height}; artifacts: ${args.out}`);
  return passed ? 0 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then((code) => { process.exitCode = code; }).catch((error) => {
    console.error(`visual-qa: ${error.message}`);
    process.exitCode = 2;
  });
}
