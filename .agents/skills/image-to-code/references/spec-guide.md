# `design-spec.json` guide

Use `references/design-spec.schema.json` to validate structure. Copy `examples/design-spec.example.json` for shape only; its values are synthetic placeholders, not inferred from user-provided imagery.

## Required data fields

- `source`: reference path, source size in px, viewport (if known), origin (screenshot/mockup/photo).
- `breakpoints`: three entries (mobile/tablet/desktop) with representative width and explicit structural behavior.
- `tokens`: CSS-ready colors and typography tokens. Token names should identify semantics rather than arbitrary hues.
- `typography`: at least heading/body definitions with family candidates, px sizes per viewport, weight, line-height, letter spacing, evidence and confidence; use `null` for unknown exact family.
- `layers`: ordered list, bounding boxes in **source screenshot pixels**, CSS/asset strategy, evidence/confidence.
- `components`: component names, source bounds if observed, sizes, responsive behavior and states.
- `assets`: a record for each image, icon and source asset, with file path (or `null`), origin, usage, license status, transparency/crop.
- `uncertainties`: missing source details not objectively recoverable, and proposed alternatives.

### Dimensions

Represent bounds `[x, y, width, height]` relative to the **original screenshot**, not CSS viewport units. If DPR is known, derive CSS pixels separately. Record `viewport_css_width` only when known. Do not assume screenshot width always equals CSS viewport width.

### Evidence

- `observed`: directly supported by visible reference or source project file.
- `inferred`: plausible adaptation chosen by the implementer.
- `unknown`: cannot be reliably established.

`confidence` must be one of `high`, `medium`, `low`; high confidence isn't permission to call an inference observed.

### Font identification

Read project styles and font files first. If unknown, analyze appearance and list candidates with reasons; keep `font_family: null` until selected, or specify selected family as an approximation with `evidence: inferred`. Do not claim exact identification from a raster screenshot.

### Asset manifest

Write `artifacts/asset-manifest.json` with `id`, `type`, `source`, `output`, `usage`, `dimensions`, `license`, `status`, and `evidence`; distinguish `original-used`, `extracted`, `CSS-rebuilt`, `generated-with-authorization`, and `pending`.
