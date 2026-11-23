# Layer decomposition: background to detail

Use pixels as evidence, not as permission to fill unseen areas.

## Layers and what to record

| Sequence | Layer | Information | Extraction preference |
| --- | --- | --- | --- |
| 0 | Full canvas | image dimensions, crop, viewport, DPR | retain reference unchanged |
| 1 | Background | color, gradients, texture, parallax, overlays, section boundaries | CSS for flat/gradient; image only for genuine art |
| 2 | Structural grids | max width, columns, gap, padding, align, breakpoints | HTML + CSS grid/flex |
| 3 | Large surfaces | hero, panels, navbar, card surfaces | CSS components |
| 4 | Text | actual copy, typescale, line height, weight, wrapping | accessible HTML |
| 5 | Icons | stroke, weight, caps, viewBox, frame, alignment | native asset or trace with verification |
| 6 | Images | crops, focal point, frame, masks, image overlays | local originals or unoccluded crops |
| 7 | Microdetail | shadows, blur, borders, separators, hover | CSS when possible |

Every layer/region: `id`, category, screenshot XYWH bounding box, relative bounding box, zIndex (where relevant), visibility, evidence, confidence, target implementation, file (if exported), and dependencies.

## Preserve source semantics

- A screenshot background **behind a card** contains unknown pixels. Cropping the visible area doesn't recover those pixels. Prefer CSS reconstruction if apparent uniform or gradient; otherwise mark missing background artwork.
- Font family cannot be proven from raster glyphs alone. Compare visual features with available project fonts; preserve exact supplied typography when known.
- Icons must have consistent viewBox, optical size, stroke thickness, stroke-linecap and spacing. If an icon can't be separated from the screenshot without colored edge fringing, rebuild as SVG only where shape is clear.
- Don't confuse a decorative, baked-in headline with actual HTML text. Separate as text if visually reproducible, accessible and intended as UI.
- No reconstruction of invisible portions of photographs or illustration without explicit generation authorization and a suitable tool.

## Alpha extraction

The included crop script copies the source rectangle without modifying pixels. It can optionally remove a **flat border-connected backdrop** with `remove_border_background: true` and a tolerance; confirm that its preview hasn't erased details. Complex backgrounds, hair, semitransparency and anti-aliased artwork require proper segmentation or the original source. Never present a flattened crop as a clean transparent original.

## Layer ordering report

Produce a table with each layer, image evidence, dimensions, planned implementation, and status (`extracted`, `CSS-rebuilt`, `original-used`, `pending`). Group repeated elements into a single component and list instances, rather than duplicating independent assets.
