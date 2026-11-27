# Visual QA PASS

- Tool: UIFix 1.2.6 (97c4b9d83684c7d607cb8c4b146e3f8aa5356e53)
- Source commit: `b80a379098b28a09da47da8cd5829c94bf387b22`
- Reference SHA-256: `9f65d312989d0aae40de76358a6c49f2f383a3cc32fd13422c88f5cbac25c713`
- Route: `http://127.0.0.1:35265/same`
- Viewport: 320x240 at DPR 1
- Capture mode/output: viewport; 320x240
- Pixel mismatch: 0/76800 (0.000%); threshold 0.000%
- SSIM: 1.0000 (diagnostic only)
- Horizontal overflow: none
- JavaScript/page errors: 0
- External requests blocked: 0

## Replay

```sh
node tools/visual_qa/visual-qa.mjs --reference "/tmp/visual-qa-smoke-uGJiSY/reference.png" --url "http://127.0.0.1:35265/same" --viewport 320x240 --out "/tmp/visual-qa-smoke-uGJiSY/repaired" --threshold 0 --capture viewport
```

## Findings

- No page errors.
- No external requests.

A pixel threshold is only an automated diagnostic. Review content, typography, responsive behavior, overflow, accessibility and business actions before acceptance.
