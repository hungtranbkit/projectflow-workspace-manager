import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";

const MINIMUM_PLAYWRIGHT = { major: 1, minor: 60 };

export function assertVisualQaPlaywrightVersion(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(String(version));
  if (!match) throw new Error(`Unsupported Playwright version for sandboxed visual QA: ${version}`);
  const major = Number(match[1]);
  const minor = Number(match[2]);
  if (major < MINIMUM_PLAYWRIGHT.major || (major === MINIMUM_PLAYWRIGHT.major && minor < MINIMUM_PLAYWRIGHT.minor)) {
    throw new Error(`Unsupported Playwright version ${version}; sandbox-required visual QA needs >=1.60.0`);
  }
}

export function loadVisualQaChromium({ selectedPackageFrom, pinnedPackageFrom }) {
  // An explicit app runtime selection is authoritative. Never fall back to a
  // different package if it is missing or too old: that would hide drift.
  const packageFrom = selectedPackageFrom || pinnedPackageFrom;
  if (!packageFrom) throw new Error("No supported Playwright package was selected for Visual QA");
  const requireFrom = createRequire(path.join(packageFrom, "package.json"));
  let entry;
  try {
    entry = requireFrom.resolve("playwright");
  } catch (error) {
    throw new Error(`Playwright is unavailable from the selected Visual QA runtime: ${error.message}`, { cause: error });
  }
  let metadata;
  try {
    metadata = JSON.parse(readFileSync(path.join(path.dirname(entry), "package.json"), "utf8"));
  } catch (error) {
    throw new Error(`Cannot verify selected Playwright package metadata: ${error.message}`, { cause: error });
  }
  assertVisualQaPlaywrightVersion(metadata.version);
  const { chromium } = requireFrom("playwright");
  if (!chromium || typeof chromium.launch !== "function") {
    throw new Error("Selected Playwright package does not expose Chromium launch");
  }
  return chromium;
}

export async function launchVisualQaChromium(chromium, { executablePath, headless = true } = {}) {
  if (!chromium || typeof chromium.launch !== "function") {
    throw new Error("Selected Playwright runtime does not expose Chromium launch");
  }
  const options = { headless, chromiumSandbox: true };
  if (typeof executablePath === "string" && executablePath.length > 0) {
    options.executablePath = executablePath;
  }
  try {
    return await chromium.launch(options);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`SECURE_CHROMIUM_LAUNCH_FAILED: sandbox-required launch failed; no insecure retry was attempted. ${detail}`, { cause: error });
  }
}
