# Visual QA FAIL

- Tool: UIFix 1.2.6 (97c4b9d83684c7d607cb8c4b146e3f8aa5356e53)
- Source commit: `8913bda32a647bfb399f13497e273cbab424307f`
- Reference SHA-256: `c263f6b9a8326d18111c583bf3450c446507261d1164456eb941850599d5e0cf`
- Route: `http://127.0.0.1:45181/app/store-minh-anh/pos`
- Viewport: 1586x992 at DPR 1
- Capture mode/output: viewport; 1586x992
- Pixel mismatch: 419773/1573312 (26.681%); threshold 8.000%
- SSIM: 0.9015 (diagnostic only)
- Horizontal overflow: none
- JavaScript/page errors: 0
- External requests blocked: 0

## Replay

```sh
node tools/visual_qa/visual-qa.mjs --reference "reference.png" --url "http://127.0.0.1:45181/app/store-minh-anh/pos" --viewport 1586x992 --out "$TASK_REPO/.artifacts/visual-qa/projectflow-stage" --threshold 0.08 --capture viewport
```

## Findings

- No page errors.
- No external requests.

A pixel threshold is only an automated diagnostic. Review content, typography, responsive behavior, overflow, accessibility and business actions before acceptance.
