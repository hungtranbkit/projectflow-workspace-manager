# Visual QA FAIL

- Tool: UIFix 1.2.6 (97c4b9d83684c7d607cb8c4b146e3f8aa5356e53)
- Source commit: `b80a379098b28a09da47da8cd5829c94bf387b22`
- Reference SHA-256: `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`
- Route: `http://127.0.0.1:8765/`
- Viewport: 1586x992 at DPR 1
- Capture mode/output: viewport; 1586x992
- Pixel mismatch: 177088/1573312 (11.256%); threshold 8.000%
- SSIM: 0.9501 (diagnostic only)
- Horizontal overflow: none
- JavaScript/page errors: 0
- External requests blocked: 0

## Replay

```sh
node tools/visual_qa/visual-qa.mjs --reference "/home/dell/workspace/.worktrees/novaretail-visual-reference-fixture-001/docs/design/web-journey/06-order.png" --url "http://127.0.0.1:8765/" --viewport 1586x992 --out "/home/dell/workspace/task-artifacts/issue-73-visual-qa/novaretail-pilot/after" --threshold 0.08 --capture viewport
```

## Findings

- No page errors.
- No external requests.

A pixel threshold is only an automated diagnostic. Review content, typography, responsive behavior, overflow, accessibility and business actions before acceptance.
