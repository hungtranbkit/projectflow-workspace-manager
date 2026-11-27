# VQ-03e2 — owner review handoff for private Visual QA drafts

## Result

The existing Task page now gives an actionable, read-only handoff for an uploaded design draft. For a valid draft on a clean, unchanged source, it proposes a unique repository-relative PNG destination and the `visual_qa.references` line for that viewport. The user downloads the image, places it on the intended Builder branch, and opens a normal owner-reviewed PR for the image and configuration. If source is dirty or has moved since upload, the handoff instructions are withheld.

The page preserves `DRAFT · UNREVIEWED`. It never writes the worktree or `PROJECT.yaml`, changes a configured reference, approves the design, opts in, or starts TestRunner. The handoff asks the owner to add the viewport only if absent; if that mapping already exists, replacing it requires explicit owner review. Route, threshold, viewport list and command remain untouched.

No in-app approval action was added. `HumanDecisionService` decisions are scoped to Change/Plan/SpecProposal, not this Task draft; generic WorkProduct status has no draft-bound owner actor record, and `AUTH_MODE=none` has no authenticated actor. Using those as approval would be untrustworthy. The only handoff is to normal repository-owner review; no approval is inferred.

## Source and tests

- Starting published PR #74 head: `b85dba75e95c6a72a7a54addaf071aaaefa2d2a1`.
- Separate VQ-03e1 evidence hash correction commit: `1e2dc7afd05bff846e5f451156f8732ceee47989`.
- VQ-03e2 source commit: `674196199a5bdd4fdb152006922cb58d6bfab82e`; tree `540a2f7a6959b1ba5211489ce35668acf9b3017e`.
- Changed source/test/docs: `app/main.py`, `app/templates/task_detail.html`, `tests/test_visual_qa_drafts.py`, `docs/VISUAL_QA_HUONG_DAN.md`.
- New regressions cover valid handoff with opt-out unchanged and no repository mutation, plus withholding instructions after source changes.

RED ran the new regression against code baseline `1e2dc7a` (the test was copied into a clean archive). Task page returned 200 but had no handoff section: 1 test, 1 failure, exit 1. JUnit starts `2026-11-26T23:16:16+07:00`.

GREEN ran on the exact committed source SHA above with the existing task venv and a nonexistent task-local Docker socket. Result: **140 passed, 0 failed, 0 errors, 0 skipped, exit 0**; JUnit suite time 115.586s, start `2026-11-26T23:22:04+07:00`. One pinned browser subprocess test was deselected because the compatible browser runtime is unavailable; this is not a browser or full-suite pass. The JUnit and sanitized stdout are in this directory. Python 3.14.4 and pytest 9.2.0 were used. `py_compile` and `git diff --check` passed.

## VQ-03e1 evidence hash correction

The prior summary’s JUnit hashes described pre-sanitization bytes, while `SHA256SUMS` described published sanitized files. I confirmed this by hashing the actual tracked files. Commit `1e2dc7a` replaced stale fields with hashes recomputed from published sanitized `red.xml`, `red.log`, `green.xml`, and `green.log`, and regenerated the checksum manifest. Exit codes/counts were not changed. Original unsanitized test files are unavailable and are not claimed by the manifest. The corrected package passes `sha256sum -c`.

## Replay and limits

The panel displays a generated destination like `docs/design/visual-qa/drafts/task-<task>-workspace-<workspace>-draft-<draft>-<viewport>.png` and the matching reference-map line. The owner must download, review, place it on the intended branch, and submit the config/image through normal PR review. If a viewport mapping already exists, do not overwrite it automatically. Only after owner review and committing the image/config at a new clean SHA should the user opt into Visual QA and run the existing Test action.

No browser, provider, Docker daemon, full suite, CI, production, merge or deploy was used. Overall #73 gates are unchanged: full-suite A and exact-head CI E remain unqualified; approved Nova B/C remain blocked; D is historical; F remains uncertified pending Docker incident disposition.
