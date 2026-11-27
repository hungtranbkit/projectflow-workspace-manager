# Browser smoke attempt boundary

- Bounded attempt: `node tools/visual_qa/smoke.mjs`, with task-local copied Chromium 153.0.8010.12, UIFix 1.2.6, `VISUAL_QA_PLAYWRIGHT_FROM` pointing at the pinned package, and a nonexistent task-local `DOCKER_HOST`.
- Chromium's stderr timestamp: `2026-11-26 17:01:43` ICT (`1126/170143` in Chromium log format).
- Observed failure: `browser.newPage: Target page, context or browser has been closed`; Chromium printed `GPU process launch failed: error_code=1002` and `FATAL: GPU process isn't usable. Goodbye.`
- Playwright's emitted launch argv included its default `--no-sandbox`, although the task did not pass that option. No more browser launch was attempted after seeing it. No usable screenshot or visual-CLI artifact was produced; the smoke did not reach its PASS summary.
- This is a browser blocker observation, not a successful canary. The application behavior in this milestone is covered by the HTTP/template and synthetic TestRunner tests; no real-browser behavior is claimed.
