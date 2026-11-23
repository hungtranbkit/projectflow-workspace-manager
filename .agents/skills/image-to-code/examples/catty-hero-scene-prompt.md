# CATTY HERO — reference-guided regeneration prompt (example)

Use the **attached approved screenshot PLUS its cropped hero visual** as the primary visual references, and recreate the **same composition and same character identity** as a single cohesive cinematic scene. Do not create a new interpretation. This asset is the **scene-only master** for the later text-overlay stage.

## Character / identity — non-negotiable

A very cute **non-humanoid, quadrupedal kitten** with ultra-soft, very light cream-white voluminous fur, fine visible strands, large rounded baby-kitten head, large triangular furry ears, two very large glossy amber/dark-brown eyes with matching catchlights, defined black eyelashes, small pink nose, joyful open mouth with tiny teeth and visible tongue, and **black collar with a small metallic silver heart pendant**. Match the reference's exact face angle, eye spacing, muzzle shape and joyful expression. Preserve at least one naturally connected front paw and the lower visible torso, even where the original screenshot's typography occludes details. Do not crop the paw. The reference crop is for *guidance*, not an image layer to paste into the output.

## Background and native lighting

A continuous magical coral, warm pink and deep reddish cinematic environment with luminous flowing silk-like ribbons, crisp bright pink/coral light trails, tiny floating light particles, natural warm rim lighting along the kitten's fur and ears, clear specular reflections in both eyes and silver heart, deep burgundy shadows, and subtle volumetric bloom. The brightest regions and light ribbons are **part of the same rendered image**, not CSS overlay or a separate blur. Natural transitions with absolutely no rectangular crop edges, ghost text or smudged haze above or below the kitten.

## Composition

Vertical mobile hero framing, matched to the reference's angle and relative scale. Place the kitten in the upper/mid portion and keep a coherent luminous environment continuing below her, with a calm yet richly lit text-safe area corresponding to the source's headline and paragraph. Retain the portion of the visible front paw. The composition should allow a later HTML/CSS title and CTA without hiding facial features. Use the reference to match the visual hierarchy rather than adding new UI.

## Exclusions (must all hold)

**NO TEXT, LETTERS, LOGO, WATERMARK, BUTTON, NAV BAR, STATUS BAR, PHONE OR APP SCREEN** in the first-stage image. No cat girl / humanoid body, human hair, bows, clothing, hats, accessories other than the black collar and silver heart; no pink eyes, missing paws, random extra legs, altered expression or new animals. No cutout halo, blurred bars, duplicated reflections, screen artifacts, oversmoothing, or squared-off lighting. This is an image asset, not a screenshot with copied UI.

## Required two-pass output

1. Export `catty-hero-scene.webp` (or PNG) — **without any text** — and compare it to the reference for facial anatomy, paws, lighting and frame.
2. In the frontend, re-add the approved title/body/navigation/CTA using real text and icon components, matching the reference's font metrics, wrapping and coordinates; only generate a separate *baked-text poster* when a static image was explicitly requested.

If any fidelity review fails, regenerate with specific targeted corrections rather than calling the output 100% faithful.
