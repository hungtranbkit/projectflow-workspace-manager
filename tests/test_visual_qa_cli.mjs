import assert from "node:assert/strict";
import test from "node:test";
import { parseArgs } from "../tools/visual_qa/visual-qa.mjs";

test("visual-qa parses local URL, exact viewport, output, threshold and full-page capture", () => {
  const parsed = parseArgs([
    "--reference", "design.png", "--url", "http://127.0.0.1:4173/orders",
    "--viewport", "1366x768", "--out", "artifacts/run", "--threshold", "0.04", "--capture", "full-page",
  ]);
  assert.equal(parsed.viewport.width, 1366);
  assert.equal(parsed.viewport.height, 768);
  assert.equal(parsed.threshold, 0.04);
  assert.equal(parsed.capture, "full-page");
  assert.match(parsed.reference, /design\.png$/);
});

test("visual-qa rejects network URLs to prevent accidental external access", () => {
  assert.throws(() => parseArgs([
    "--reference", "design.png", "--url", "https://example.com",
    "--viewport", "1366x768", "--out", "artifacts/run",
  ]), /loopback URL/);
});

test("visual-qa rejects invalid viewport and threshold", () => {
  assert.throws(() => parseArgs([
    "--reference", "design.png", "--url", "http://localhost:4173/orders",
    "--viewport", "100x100", "--out", "artifacts/run",
  ]), /outside the supported range/);
  assert.throws(() => parseArgs([
    "--reference", "design.png", "--url", "http://localhost:4173/orders",
    "--viewport", "1366x768", "--out", "artifacts/run", "--threshold", "1.5",
  ]), /between 0 and 1/);
  assert.throws(() => parseArgs([
    "--reference", "design.png", "--url", "http://localhost:4173/orders",
    "--viewport", "1366x768", "--out", "artifacts/run", "--capture", "zoomed",
  ]), /viewport or full-page/);
});

test("visual-qa rejects missing required arguments", () => {
  assert.throws(() => parseArgs(["--url", "http://localhost:4173", "--viewport", "430x932", "--out", "out"]), /--reference/);
});
