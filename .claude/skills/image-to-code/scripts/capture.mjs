#!/usr/bin/env node
/** Capture reproducible browser evidence at responsive and reference-matched viewports. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const DEFAULT_VIEWPORTS = {
  mobile: { width: 390, height: 844 },
  tablet: { width: 820, height: 1180 },
  desktop: { width: 1440, height: 900 },
};

export function viewport(value) {
  const match = /^(\d{2,5})x(\d{2,5})$/.exec(value ?? '');
  if (!match) throw new Error(`Expected WIDTHxHEIGHT, received: ${value}`);
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (width < 240 || width > 7680 || height < 240 || height > 10000) {
    throw new Error('Viewport must be 240..7680 pixels wide and 240..10000 pixels tall');
  }
  return { width, height };
}

export function parseArgs(argv) {
  const args = { url: null, out: 'artifacts/screenshots', fullPage: false, reference: null,
    smokeWidths: [], failOnWarnings: true, timeoutMs: 30000 };
  for (let i = 0; i < argv.length; i++) {
    const option = argv[i];
    const take = () => { if (!argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error(`Missing value for ${option}`); return argv[++i]; };
    if (option === '--url') args.url = take();
    else if (option === '--out') args.out = take();
    else if (option === '--full-page') args.fullPage = true;
    else if (option === '--reference-viewport') args.reference = viewport(take());
    else if (option === '--smoke-widths') {
      args.smokeWidths = take().split(',').map(s => Number(s.trim()));
      if (args.smokeWidths.some(n => !Number.isInteger(n) || n < 240 || n > 7680)) throw new Error('Invalid smoke widths');
      args.smokeWidths = [...new Set(args.smokeWidths)];
    } else if (option === '--allow-warnings') args.failOnWarnings = false;
    else if (option === '--timeout-ms') {
      args.timeoutMs = Number(take());
      if (!Number.isInteger(args.timeoutMs) || args.timeoutMs < 1000 || args.timeoutMs > 120000) throw new Error('Invalid timeout');
    } else throw new Error(`Unknown option: ${option}`);
  }
  if (!args.url) throw new Error('Usage: node scripts/capture.mjs --url http://localhost:3000 [--reference-viewport 1179x616] [--smoke-widths 360,430,768,1024,1280,1920]');
  const url = new URL(args.url);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Only HTTP(S) URLs are supported');
  return args;
}

export function inspectResult(result) {
  return Boolean((result.status && (result.status < 200 || result.status >= 400)) ||
    result.horizontalOverflow || result.runtimeErrors?.length || result.brokenImages?.length ||
    result.unlabeledControls?.length || result.missingMain || result.failedRequests?.length || result.error);
}

async function capturePage(browser, args, name, size, screenshot = true) {
  const context = await browser.newContext({ viewport: size, deviceScaleFactor: 1, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const runtimeErrors = [];
  const failedRequests = [];
  page.on('pageerror', error => runtimeErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') runtimeErrors.push(message.text()); });
  page.on('requestfailed', request => failedRequests.push({ url: request.url(), failure: request.failure()?.errorText ?? 'unknown' }));
  try {
    const response = await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: args.timeoutMs });
    await page.evaluate(async () => {
      const timeout = ms => new Promise(resolve => setTimeout(resolve, ms));
      await Promise.race([document.fonts.ready, timeout(10000)]);
      await Promise.race([Promise.all([...document.images].map(img => img.complete ? Promise.resolve() : new Promise(resolve => {
        img.addEventListener('load', resolve, { once: true });
        img.addEventListener('error', resolve, { once: true });
      }))), timeout(10000)]);
    });
    await page.addStyleTag({ content: '*, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; caret-color: transparent !important; }' });
    const dom = await page.evaluate(() => {
      const visible = el => { const rect = el.getBoundingClientRect(); const style = getComputedStyle(el); return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && rect.width > 0 && rect.height > 0; };
      const unlabeledControls = [...document.querySelectorAll('button, a, input, select, textarea, [role="button"]')]
        .filter(visible).filter(el => !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby') &&
          !(el.textContent ?? '').trim() && !(el.getAttribute('title') ?? '').trim() &&
          !(el.querySelector('img[alt]:not([alt=""])')) && !(el instanceof HTMLInputElement && (el.labels?.length || el.placeholder)))
        .map(el => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}`).slice(0, 25);
      return {
        documentWidth: document.documentElement.scrollWidth,
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
        brokenImages: [...document.images].filter(img => !img.complete || img.naturalWidth === 0).map(img => img.currentSrc || img.src || '(unknown)').slice(0, 25),
        unlabeledControls,
        missingMain: ![...document.querySelectorAll('main, [role="main"]')].some(visible),
        title: document.title,
        fontStatus: document.fonts.status,
      };
    });
    if (screenshot) await page.screenshot({ path: path.join(args.out, `${name}.png`), fullPage: args.fullPage, animations: 'disabled' });
    return { name, viewport: size, dpr: 1, status: response?.status() ?? null,
      screenshot: screenshot ? `${name}.png` : null, ...dom, runtimeErrors, failedRequests };
  } catch (error) {
    return { name, viewport: size, error: String(error) };
  } finally { await context.close(); }
}

export async function run(args, chromium) {
  await fs.mkdir(args.out, { recursive: true });
  const browsers = { ...DEFAULT_VIEWPORTS };
  if (args.reference) browsers['desktop-same-viewport'] = args.reference;
  for (const width of args.smokeWidths) browsers[`smoke-${width}`] = { width, height: 900 };
  const evidence = [];
  const warnings = [];
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    for (const [name, size] of Object.entries(browsers)) {
      const record = await capturePage(browser, args, name, size, !name.startsWith('smoke-'));
      evidence.push(record);
      if (record.error || inspectResult(record)) warnings.push(`${name}: browser checks failed`);
      if (record.failedRequests?.length) warnings.push(`${name}: ${record.failedRequests.length} requests failed`);
      process.stdout.write(JSON.stringify(record) + '\n');
    }
  } catch (error) {
    warnings.push(`Browser unavailable or failed: ${String(error)}`);
  } finally {
    if (browser) await browser.close();
    await fs.writeFile(path.join(args.out, 'capture-report.json'), JSON.stringify({ url: args.url, captures: evidence, warnings, passed: warnings.length === 0 }, null, 2) + '\n');
  }
  return !args.failOnWarnings || (warnings.length === 0 && evidence.length > 0);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  try {
    const args = parseArgs(process.argv.slice(2));
    const { chromium } = await import('playwright');
    if (!(await run(args, chromium))) process.exitCode = 1;
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
