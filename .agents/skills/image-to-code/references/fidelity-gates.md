# Visual fidelity gates — typography, icons, CTA and original artwork

This is a **blocking** acceptance protocol for screenshot-to-code implementations, complementary to the existing visual audit and browser QA. Passing a Python utility alone is not a 9.5/10 verdict.

## Independent specialist reviews (parallel if subagents are available)

**Agent A — Interface fidelity (read-only during audit)**

1. Compare the **same page, viewport, DPR, loaded fonts, content and interaction state**. Measure original and rendered bounds of each representative icon, heading/body text and CTA.
2. Inspect SVG path geometry, viewBox, stroke/fill, cap/join, visual weight, glyph baseline, optical alignment and original file provenance. A generic icon from a library is not automatically the original.
3. Inspect project `@font-face` / font imports; verify browser `document.fonts.check`, actual computed `font-family`, weight, line-height, letter spacing and wrapping. A silently loaded fallback font is not acceptable. Font family cannot be proven by screenshot appearance alone.
4. For the CTA record `[x, y, width, height]`, padding, line height, borders and placement. Never resize the reference to conceal an oversized button.
5. Produce `artifacts/reviews/interface-fidelity.md` with matching annotated screenshots, objective discrepancies and an independently assessed rubric score.

**Agent B — Asset forensics (read-only during audit)**

1. Inventory approved originals, screenshot derivatives and rendered images with SHA-256 hashes, pixel dimensions, alpha, provenance and crop lineage.
2. **Classify glow as `baked-in`, `separate-layer` or `unknown` before implementing it.** If subject and glow are one composite, preserve the entire original. In **preserve-original mode**, never regenerate the cat or synthesize hidden anatomy; for **user-authorized regeneration**, follow `references/reference-guided-regeneration.md` to generate a complete scene from both reference images without duplicated CSS glow.
3. Check the entire illustration, including **Catty's paw**, at reference breakpoints. Review source crop, `object-fit`, `object-position`, `overflow`, transforms, masks and stacking. Inspect without overlays to diagnose clipping before modifying the source.
4. Protect original assets with SHA-256 checksums. A crop extracted from a screenshot is **not** the original isolated image. If original missing, raise `BLOCKED: SOURCE_ASSET_MISSING`, do not claim pixel-perfect fidelity.
5. Audit upper-glow, lower-glow and paw regions independently. Produce `artifacts/reviews/asset-forensics.md` with browser evidence and provenance.

**Agent C — Independent acceptance (after A and B)**

- Challenge unsupported claims in both reviews, inspect source and final browser captures directly, and rerun all strict gates.
- Fail on substituted icons, unloaded/mismatched fonts, oversized CTA, absent original asset, extra light bands, lost paws, clipped imagery and unexplained masking.
- Check real browser interactions and responsive screenshots; record any blocking limitation.
- If subagents are unavailable, perform three **distinct auditing passes** with separate reports and disclose that they were not independent agents.
- Deliver `artifacts/reviews/acceptance.md` with browser evidence and chat-visible comparison boards. No unverified ≥9.5/10 grade.

## Reproducible region-level quality gate

Start from `examples/catty-analysis-2.contract.example.json`, replacing all **placeholder coordinates** and adding **verified known-good SHA-256 hashes** from the real project. The regions need manual measurement against approved reference screenshots. All measurements must be made before evaluating the final render, not adjusted after failing.

```bash
python scripts/audit_fidelity_contract.py \
  --contract artifacts/visual-fidelity.contract.json \
  --project /path/to/project \
  --out artifacts/audit/fidelity-contract-report.json
```

A contract includes `reference`, `render`, `regions`, `required_categories`, `require_protected_assets`, and `protected_assets`. Independent regions can cover each icon, text, button, upper/lower glow and a paw. Geometry and pixels are checked separately. Resizing mismatched crops is prohibited: it could falsely accept a large CTA. A changed or missing protected asset fails even if a substitute renders successfully. Screenshot sizes must match exactly.

Automated comparison uses color thresholds and user-specified bounds, not automatic recognition of a font, exact artwork provenance or a subjective score. Human reviewers must inspect the actual source and browser output; an insufficient or absent reference is a blocker.

## Catty analysis 2 — two duplicated requests, one regression set

| Observed user report | Gate |
| --- | --- |
| Different icons | Agent A plus individual icon crop and original SVG/source check |
| Slightly different font | Agent A plus rendered text crop, computed font and font-load evidence |
| Oversized “Conversar com a Catty” button | Agent A plus strict CTA reference/render XYWH geometry |
| Glow already part of the illustration | Agent B plus locked original composite and baked-in-glow declaration |
| New upper/lower blur bands | Agent B plus upper and lower artwork comparison regions |
| Catty paw missing | Agent B plus dedicated paw reference/render crop, overflow and z-index check |

This skill repository provides **quality gates**; it does not itself change the Catty application. Catty-specific UI and CSS corrections belong in `lenixeduardo/catty-minhaamiga` and require a separate browser-tested implementation.

## Authorized regeneration exception (v1.5.0)

Original-asset preservation/hash blocking remains mandatory where the user requests unchanged assets. When the user explicitly requests new generated artwork, follow the two-stage reference-guided protocol and `scripts/validate_generation_cycle.py`; the final image has its own provenance and is never a relabeled source crop. The complete scene remains text-free until the second text overlay pass.
