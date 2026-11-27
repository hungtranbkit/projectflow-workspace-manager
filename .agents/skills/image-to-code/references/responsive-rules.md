# Responsive reconstruction protocol

## Observed vs inferred

Never claim that a screenshot at 1440px establishes the precise 390px layout. For viewports not shown, infer a coherent responsive adaptation and test the resulting behavior. Record `evidence: inferred`, even if it looks plausible.

## Three required view families

| Family | Inspect | Design decisions |
| --- | --- | --- |
| Mobile | 360, 390, 430px | stacked content, tap-friendly actions, collapsed nav when required, hero text visibility, no scroll-x |
| Tablet | 768, 820, 1024px | intermediate grids, adaptive nav, 1–2 columns, media crop, controlled max-width |
| Desktop | 1280, 1440, 1920px | source grid composition, responsive gutters, max-width, full-width background continuity |

Representative capture sizes: mobile 390×844, tablet 820×1180, desktop 1440×900. Add exact reference viewport sizes whenever known.

## Component-by-component questions

For each component record what happens on mobile/tablet/desktop:

- **Header:** sticky/fixed/static; break from inline navigation to menu; menu keyboard and touch interaction; logo sizing and visible utilities.
- **Hero:** max-width; text order; fixed/relative artwork; text legibility and safe area; object-position or alternate art direction.
- **Cards/grid:** column counts, `minmax`, gap, row heights, independent card content, reading order.
- **Typography:** font size/line height changes; `clamp`, max measure, heading wrapping, hyphenation avoidance.
- **Images:** aspect-ratio, crop/contain, focal point, backgrounds, overlay stacking and potential content shift.
- **Buttons/controls:** tap hit areas, focus states, full-width or inline on mobile, group wrapping, disabled/loading.
- **Section spacing:** fluid padding/vertical rhythm, avoidance of large empty zones and clipped content.
- **Modals/menus:** screen fit, close behavior, Escape and focus containment if used.

Prefer intrinsic layout: `width: min(100% - 2rem, 72rem)`, `grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr))`, `font-size: clamp(...)`. Use breakpoints only where structural transformations are necessary. Ensure images remain proportional and don't create overflow.

Check 200% browser zoom, keyboard focus, reduced motion, and small-height mobile. If a background must extend to the bottom of a long page, attach it to the correct section or page element, not just a viewport-height pseudo-element.
