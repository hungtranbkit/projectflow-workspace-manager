# Visual QA FAIL

- Tool: UIFix 1.2.6 (97c4b9d83684c7d607cb8c4b146e3f8aa5356e53)
- Source commit: `331f9963a51d07ff59c0989214ba877cc5da0163`
- Reference SHA-256: `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`
- Route: `http://127.0.0.1:45174/app/store-minh-anh/orders/new`
- Viewport: 1586x992 at DPR 1
- Capture mode/output: viewport; 1586x992
- Pixel mismatch: 310775/1573312 (19.753%); threshold 8.000%
- SSIM: 0.9022 (diagnostic only)
- Horizontal overflow: none
- JavaScript/page errors: 0
- External requests blocked: 0

## Replay

```sh
node tools/visual_qa/visual-qa.mjs --reference "/home/dell/workspace/.worktrees/novaretail-visual-reference-fixture-001/docs/design/web-journey/06-order.png" --url "http://127.0.0.1:45174/app/store-minh-anh/orders/new" --viewport 1586x992 --out "/home/dell/workspace/.worktrees/novaretail-visual-reference-fixture-001/prototypes/baoyu/NWR-VISUAL-REFERENCE-FIXTURE-001/react-pilot/uifix-after" --threshold 0.08 --capture viewport
```

## Findings

- No page errors.
- No external requests.

A pixel threshold is only an automated diagnostic. Review content, typography, responsive behavior, overflow, accessibility and business actions before acceptance.
