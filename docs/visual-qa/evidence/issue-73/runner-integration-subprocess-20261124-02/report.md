# ProjectFlow visual QA subprocess integration

Fixture-only end-to-end test: real TestRunner background dispatch, real SQLite, real host subprocess runner, loopback HTML server, pinned UIFix CLI. No production project or provider was used.

- Source HEAD for passing visual capture: `ccbc35c3b759d611c24991c9e2c684284bdbd380`
- Reference SHA-256: `b89caa21bde04741f0909b0e9e62a1d33fa90c4c1d927ead91d7d8a0b119237e` (fixture image, 320x240)
- ProjectFlow policy threshold: 0 for exact synthetic-fixture integration; this is not the product 8% pilot threshold.
- Replay test: `python3 -m pytest -q tests/test_visual_qa_runner_subprocess.py` (set `CHROME_PATH` and `VISUAL_QA_PLAYWRIGHT_FROM` for the authorized local browser when needed).
- Commands run by `TestRunner`: local `python3 -c ...` test stage and pinned `node tools/visual_qa/visual-qa.mjs` visual stage.
- Verified: pass, color-difference fail, dimension fail-closed, unavailable-server fail-closed, missing-reference contract rejection, backend opt-out, and stale commit evidence returns `NOT_RUN`.

Artifacts in this directory include reference, actual, diff, overlay, sanitized UIFix metrics and stage results. The reference is generated for this test only and is not a product design approval.
