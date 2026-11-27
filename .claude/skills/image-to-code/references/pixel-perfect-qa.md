# Pixel-perfect QA — mandatory calibrated feedback loop

## Design for evidence, not for a flattering score

1. **Reference provenance:** record exact source image, whether the screenshot includes a device frame, crop / viewport / DPR; do not call inferred dimensions measured. Save original read-only. Do not retouch reference or generate a new reference with an image model and call it the same screenshot.
2. **Asset-first hard gate:** recover actual icons, fonts and expressions from approved sources where possible. Keep the background lighting/particles with the original artistic source instead of creating blur patches that conceal missing subject anatomy. Preserve original character's face, paws and contours. No hidden part is recoverable exactly from a flattened screenshot; label approximations.
3. **States and geometry:** enumerate every visible UI state and interaction in the source (not just the landing page). Each required state gets its own browser screenshot, layout assertions and the exact strings from the mockup. No fallback art silently masking missing assets.
4. **Viewport and aspect ratio:** compare only matching viewport and DPR; if the source includes frames or is a triptych, explicitly define crops and declare normalization. Don't silently scale/warp either image. Tablet and desktop inferred from a mobile-only reference have **responsiveness QA**, not claimed matching fidelity.
5. **Pixel calibration:** run `scripts/calibrate_pixel_perfect.py` with each stage reference and Chromium screenshot. Inspect `compare-state-N.png`, `heatmap-state-N.png`, SSIM and RGB MAE. Optionally include prior baseline screenshots to quantify whether a change improved or regressed. No SSIM-to-0..10 conversion.
6. **Browser hard gate:** `scripts/validate_visual_states.mjs` must prove that the frame, face, paws, main CTAs, bottom bar, microphone and other critical nodes fit the viewport, including within overflow-clipping ancestors. This stops false positives where text is present but the phone UI is cut off.
7. **Interaction QA:** test navigation, dialogs, modal close, state transitions, keyboard, touch where relevant, labels, failed images, overflow, JS errors. A demo must reproduce fixed mockup copy verbatim, not hallucinate a conversation or assume a real AI service.
8. **Review:** categorize deltas by severity: wrong asset/expression/occluded anatomy (P0), typography and icons (P1), border/shadow and details (P2). Correct biggest first and repeat capture + calibration. Keep evidence of original vs new.
9. **Presentation contract:** mobile + tablet side by side, and for desktop reference images, desktop source vs rebuilt desktop side by side; show directly in chat at readable size. For multi-step mobile states, show one original/render pair per step. Do not fill the chat with a 10-panel illegible collage.
10. **Acceptance:** no score ≥9.5 without fully visible UI state screenshots, working interactive code, verified assets, typography/icon audit and zero P0 blockers. Never promise recoverability of hidden content. Keep separate technical test pass counts, visual estimate, diagnostic SSIM/MAE and unknowns.

## Usage (reference triptych, mobile viewport)

```bash
python scripts/calibrate_pixel_perfect.py \
  --reference approved-triptych.png --columns 3 \
  --render artifacts/mobile-stage-0.png \
  --render artifacts/mobile-stage-1.png \
  --render artifacts/mobile-stage-2.png \
  --normalize-reference --out artifacts/pixel-qa
node scripts/validate_visual_states.mjs examples/visual-states.example.json
```

Normalization changes only a derived reference used for measurement and must be disclosed. A more rigorous case uses individually captured source screens matching the viewport exactly, without `--normalize-reference`.

SSIM is optional and requires `pip install -r requirements-pixel-perfect.txt`; Pillow MAE works with `requirements.txt` alone.
