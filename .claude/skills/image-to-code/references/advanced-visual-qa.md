# Advanced visual QA / multi-agent acceptance

The image-to-code skill uses **agent-independent, deterministic QA tools**. A working CLI is not evidence that an agent actually performs the image-to-code workflow; `probe_agents.py` always leaves runtime invocation unverified without proof.

## Run on real, equal-sized screenshots

```bash
python -m pip install -r requirements-advanced.txt
python scripts/advanced_visual_qa.py --config examples/advanced-visual.example.json --out artifacts/visual/state-1
python scripts/validate_component_geometry.py --config examples/geometry.example.json --out artifacts/geometry.json
python scripts/build_state_dashboard.py --state-dir artifacts/visual/state-1 --out artifacts/visual/dashboard
python scripts/audit_typography_icons.py --config examples/font-icon.example.json --out artifacts/font-icon.json
python scripts/browser_font_audit.py --config examples/browser-font.example.json --out artifacts/font-browser.json
python scripts/browser_compat_qa.py --config examples/browser-compat-fixture.json --out artifacts/browser-compat.json
python scripts/probe_agents.py --workspace . --out artifacts/agent-probe.json
python scripts/negative_controls.py --out artifacts/negative-controls.json
python scripts/benchmark_evidence.py --manifest examples/benchmark-coverage.example.json --out artifacts/benchmark.json
```

All artwork and fonts are **read-only**. Original source file paths in config must reference authorized local files; no proprietary images, assets, screenshots or font binaries are embedded in this public repo.

### Key rules
1. Every mask requires a named reason, must total at most 8% of pixels, and **cannot overlap critical regions** such as subject face or main CTA.
2. Region SSIM excludes masked neighborhoods; SSIM from a region of few pixels can be unavailable. Regions must have valid in-bounds boxes.
3. Alignment hints are advisory only, never automatically shift/warp/re-render the image to inflate similarity.
4. Geometry tolerance defaults: critical 3px, high 5px, normal 9px, decorative 14px. A hidden/clipped element always fails even if its measured delta is zero.
5. FontFaceSet and CSS computed styles cannot definitively prove the original installed typeface. Compare source font metadata when available and licensed; do not distribute font files.
6. Icon geometry analysis expects an original SVG or authorized raster reference. If absent, report uncertainty, never fabricate vector source.
7. Browser tests include real Chromium touch input. WebKit absence is reported `SKIPPED`, not `PASS`. Linux Chromium mobile emulation is not iOS/Android hardware testing.
8. The interactive overlay is a self-contained static HTML file with embedded local image data and no network. It is not an AI-generated mockup.
9. CSS proposals are suggestions from measured differences; they are **not auto-applied**. Review parent layout/positioning and test every breakpoint.
10. CI artifacts should retain **only anonymized metrics and synthetic fixture screenshots** unless the project owner explicitly authorizes upload of proprietary artwork.

## Three-project P0 gate
Approved project references are required at **each** viewport: mobile, tablet and desktop. An inferred tablet design or stretched desktop screenshot cannot count. Use `benchmark_evidence.py` to report coverage and execute `negative_controls.py` for synthetic rejection sensitivity. Synthetic controls never imply a field false-positive rate. Autonomous image-to-code reconstruction needs separate real code-agent proof.