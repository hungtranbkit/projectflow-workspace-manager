import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  assertVisualQaPlaywrightVersion,
  launchVisualQaChromium,
  loadVisualQaChromium,
} from "../tools/visual_qa/browser-launch.mjs";

const root = new URL("../", import.meta.url);

test("CLI and smoke route Chromium through the shared secure launch policy", async () => {
  for (const file of ["tools/visual_qa/visual-qa.mjs", "tools/visual_qa/smoke.mjs"]) {
    const source = await readFile(new URL(file, root), "utf8");
    assert.match(source, /launchVisualQaChromium/, `${file} must use the shared launch policy`);
    assert.doesNotMatch(source, /chromium\.launch\s*\(/, `${file} must not launch Chromium directly`);
  }
});

test("sandbox-required minimum Playwright version is enforced", () => {
  assert.doesNotThrow(() => assertVisualQaPlaywrightVersion("1.60.0"));
  assert.doesNotThrow(() => assertVisualQaPlaywrightVersion("1.61.2"));
  assert.throws(() => assertVisualQaPlaywrightVersion("1.59.9"), /needs >=1\.60\.0/);
  assert.throws(() => assertVisualQaPlaywrightVersion("not-a-version"), /Unsupported Playwright version/);
});

test("an explicit unsupported runtime fails closed instead of falling back to the pinned package", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "visual-qa-playwright-resolution-"));
  try {
    const selected = path.join(temp, "selected");
    const pinned = path.join(temp, "pinned");
    await mkdir(path.join(selected, "node_modules", "playwright"), { recursive: true });
    await writeFile(path.join(selected, "node_modules", "playwright", "package.json"), JSON.stringify({ name: "playwright", version: "1.59.9", main: "index.js" }));
    await writeFile(path.join(selected, "node_modules", "playwright", "index.js"), "exports.chromium = { launch() {} };\n");
    await mkdir(path.join(pinned, "node_modules", "playwright"), { recursive: true });
    await writeFile(path.join(pinned, "node_modules", "playwright", "package.json"), JSON.stringify({ name: "playwright", version: "1.60.0", main: "index.js" }));
    await writeFile(path.join(pinned, "node_modules", "playwright", "index.js"), "exports.chromium = { launch() {} };\n");
    assert.throws(
      () => loadVisualQaChromium({ selectedPackageFrom: selected, pinnedPackageFrom: pinned }),
      /Unsupported Playwright version 1\.59\.9/,
    );
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
});

test("sandbox stays enabled with or without the optional executable override", async () => {
  for (const executablePath of [undefined, "/opt/verified/chromium"]) {
    const calls = [];
    const browser = { ok: true };
    const result = await launchVisualQaChromium({ launch: async (options) => { calls.push(options); return browser; } }, { executablePath });
    assert.equal(result, browser);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].headless, true);
    assert.equal(calls[0].chromiumSandbox, true);
    assert.equal("args" in calls[0], false);
    if (executablePath) assert.equal(calls[0].executablePath, executablePath);
    else assert.equal("executablePath" in calls[0], false);
  }
});

test("sandbox launch failure is reported without a weaker retry", async () => {
  const expected = new Error("sandbox unavailable in this runtime");
  const calls = [];
  await assert.rejects(
    launchVisualQaChromium({ launch: async (options) => { calls.push(options); throw expected; } }),
    (error) => error.message.includes("no insecure retry was attempted") && error.cause === expected,
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0].chromiumSandbox, true);
  assert.equal("args" in calls[0], false);
});

test("an unsupported browser object fails before any launch attempt", async () => {
  await assert.rejects(launchVisualQaChromium({}), /does not expose Chromium launch/);
});
