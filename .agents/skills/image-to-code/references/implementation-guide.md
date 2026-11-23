# Source implementation playbook

1. Identify the project's router, page, CSS strategy, existing components, fonts, image loader, and static asset convention. Make no framework migration without explicit request.
2. Create semantic tokens in the existing token mechanism, using explicit recorded values and fallback for unknown. Avoid magic numbers scattered across components.
3. Implement a structurally accurate DOM using sections, navigation, headings and lists where appropriate; ensure focus order follows visible reading order.
4. Rebuild CSS background and section frame before adding content. Then implement component geometry, typography, icons and pictures, and microeffects. Check each stage in the browser before moving to the next.
5. Use provided original assets when available. Don't fetch copyrighted photos, Google Fonts or icon sets automatically when external downloads are prohibited or the license is unclear.
6. For raster images use `<picture>` / `img` or the project's approved image component with explicit dimensions, lazy-loading as appropriate, alt text and consistent object-position. Use SVG for simple icons but avoid blindly applying a generic icon library where source icons differ.
7. Match textual content as supplied; where unreadable, mark a placeholder instead of fabricating real brand copy.
8. Make responsive behaviors explicit, not achieved by arbitrary shrinking. Use `clamp()` thoughtfully; avoid absolute coordinates for core content.
9. Keep edits scoped to the requested page and assets; never overwrite brand identity or unrelated modules.
10. Run format/lint/typecheck/build when available. Test interactive states in the browser and inspect console/network errors.

## Example component tree (adapt to the source)

```text
Page
 ├─ BackgroundLayer
 ├─ SiteHeader
 │   ├─ Brand
 │   ├─ DesktopNav
 │   └─ MobileNav
 ├─ HeroSection
 │   ├─ Eyebrow, Title, Description
 │   ├─ CTAActions
 │   └─ HeroArtwork
 ├─ FeatureGrid
 │   └─ FeatureCard[]
 └─ SiteFooter
```

This is not an instruction to invent these sections if absent from the image.

## CSS tokens example (illustrative only; not extracted)

```css
:root {
  --content-max: 72rem;
  --page-gutter: clamp(1rem, 3vw, 3rem);
  --text-heading: clamp(2rem, 4vw, 4.5rem);
  --radius-card: 1rem;
}
.container { width: min(calc(100% - 2 * var(--page-gutter)), var(--content-max)); margin-inline: auto; }
```

Do not copy these numeric defaults into a real design spec unless source evidence supports them.
