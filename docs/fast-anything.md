# fast-anything — Performance Optimization Pilot

## Current status

Evidence snapshot: `2026-11-24 11:35:34 UTC`. The user-level installation and synthetic tool workflow passed on the healthy Dell Linux x86_64 development node. A real Code Graph RAG workload was **not found or run**, so the synthetic result is not evidence that ProjectFlow or a Code Graph RAG application is faster. No production service or application source was modified.

The install is intentionally local to this user account. Use `scripts/fast-anything-pilot.sh` to install, check, or remove the pinned CLI. The installed MCP registrations are user-scoped and point at the absolute executable path; no repository-specific or production MCP config was changed.

## Verified release and package

- Upstream repository: <https://github.com/EL4CTEO/fast-anything>
- npm package: <https://www.npmjs.com/package/@el4cteo/fast-anything>
- Selected stable release: `0.8.6` (`latest` at verification time, 2026-11-24 UTC)
- Official GitHub release: <https://github.com/EL4CTEO/fast-anything/releases/tag/v0.8.6>
- Tag `v0.8.6` resolves to source commit `ba571858d222836843fcd0bccbd94a4290c36ac2`
- Main package integrity: `sha512-oayRBlpXFu7ReoIjPfj9YD2tVICMLtrBL7nwCxr+XGg1UfIOmmu6PD3O3uZ0EOJBgahf7NUmoY3rWkaCwyRuog==`
- Linux x64 package: `@el4cteo/fast-anything-linux-x64@0.8.6`; `os=linux`, `cpu=x64`, `libc=glibc`; integrity `sha512-4RuvY/iSgwiKNmF3esmOjn8ya/FR5j8q6vjNhR1NxHZ6pQURvqF3xgLfgkLLVGqmT2BZKizWZpeB/8d6S5zJzg==`
- License: MIT. Node engine: `>=18`.
- Package audit: no npm lifecycle scripts were declared. The npm wrapper selects the platform package and launches its binary. The Linux artifact is an x86-64 dynamically linked ELF (glibc, `libgcc_s`); SHA-256 `240504aef174edc52c653ab5d30594f94be0bd16be68f04616f7b675d2675288`.
- The published npm integrity pins the downloaded tarballs. npm wrote a local `package-lock.json` in the user install prefix with these package integrities; the relevant integrity values are recorded above. The lockfile remains with the user-local install rather than becoming a ProjectFlow runtime dependency. The binary was not independently rebuilt or signed as part of this pilot; treat npm integrity and the upstream tag as the recorded provenance evidence.

## Installation and agent registration

- Install prefix: `$HOME/.local/share/projectflow-tools/fast-anything`
- Installed command: `$HOME/.local/share/projectflow-tools/fast-anything/node_modules/.bin/fast-anything`
- Installed package size: 2.7 MiB.
- Versions at verification: Node `v26.7.0`, npm `11.19.0`, Python `3.14.4`, rustc `1.100.0`, Cargo `1.100.0`, Claude Code `2.1.331`, Codex CLI `0.171.0`.
- Rust was initially absent. The official rustup installer was reviewed and run with a minimal stable profile and `--no-modify-path`; no sudo or system package changes. Toolchain is user-owned, about 600 MiB in `$HOME/.rustup` plus 43 MiB in `$HOME/.cargo`. Cargo is added only to each MCP server's configured `PATH`, not to shell startup files.
- `fast-anything doctor`: PASS (`ready to scan`, `ready to port`); it also reports optional runtimes not installed (Ruby, Go, PHP, R, Bun, Deno).
- Claude Code: registered `fast-anything` at **user** scope; `claude mcp list` reports Connected.
- Codex: registered `fast-anything` in user config; `codex mcp get fast-anything` reports enabled stdio transport.
- Direct MCP stdio check: `initialize` PASS (protocol `2025-03-26`); `tools/list` PASS with all eight tools: `fast_scan`, `fast_port`, `fast_verify`, `fast_enable`, `fast_disable`, `fast_bench`, `fast_report`, `fast_status`. Direct `tools/call fast_status` also passed and showed the port disabled after rollback.
- Existing MCP entries were preserved; neither client had a `fast-anything` entry before this registration. Configs contain the absolute local executable path and Cargo PATH; they are user-local and are not committed.

## Synthetic smoke evidence (not application evidence)

The isolated workload came from upstream's `examples/fuzzy-dedupe` at tag `v0.8.6`, copied to a disposable `/tmp` directory. It is deterministic and contains no proprietary input. The port implementation was the upstream tag's reference Rust example, not an AI-generated or application change.

Commands were run from the disposable workload directory with the absolute CLI path and `$HOME/.cargo/bin` on PATH:

1. `fast-anything scan -- python3 app.py` — exit 0. Latest run: 41.00 s profiled; `fuzzy.find_duplicates` ranked first at 100.0% cumulative share, with `levenshtein` and `normalize` listed as helpers.
2. `fast-anything port fuzzy.find_duplicates` — exit 0; recorded 1 real call.
3. Copied the reference Rust implementation from the same upstream tag into the generated temporary crate, then `fast-anything verify fuzzy.find_duplicates` — exit 0; 33/33 calls matched (1 recorded, 7 edge, 25 derived). Reported per-call time: 11,874.2 ms Python vs 15.2 ms Rust (783×).
4. `fast-anything enable fuzzy.find_duplicates` — exit 0.
5. `fast-anything bench --runs 5` — exit 0; five interleaved runs per variant. Tool output: Python median 10.2606 s (standard deviation 0.7995 s) vs Rust median 0.0465 s (standard deviation 0.0057 s), 220.6×. CLI summary rounded this to 10.26 s vs 47 ms, 221×, `±799 ms`.
6. An additional five alternating Python/Rust runs recorded end-to-end time and peak RSS with `/usr/bin/time`: Python median 9.9757 s (range 8.9260–10.7063 s), median peak RSS 10,768 KiB (range 10,676–10,880 KiB); Rust median 0.0515 s (range 0.0379–0.0895 s), median peak RSS 11,604 KiB (range 11,512–11,736 KiB). The ratio of those medians is about 193.7×. All ten runs produced the same output SHA-256: `c198d28657c7892c1f23d81576969f09619d1522c2af59f5017e80d458c1ecc0`.
7. `fast-anything report` — exit 0; generated Markdown and SVG in the disposable `.fast` directory (not committed).
8. `fast-anything disable fuzzy.find_duplicates` — exit 0; restored `fuzzy.py` byte-for-byte to the upstream original (SHA-256 `4a668121ae0396069b953e718dfb9b51d6d272f3f576148fe4bfa3f7eed52031`). The post-rollback output hash matched the ten-run result above.

The Rust-enabled sample used about 836 KiB more median peak RSS than the Python variant. The smoke project's `.fast` data occupied 71 MiB at measurement time. System disk remained at 74% used with about 122 GiB free; no disk cleanup was needed. Generated `.fast/` content is excluded from Git. The pilot-owned synthetic `.fast/` directory was removed with `fast-anything clean --all` after evidence was recorded; the user npm cache was left untouched.

## Real Code Graph RAG workload

**BLOCKED / NOT RUN.** A read-only inspection found a local document RAG engine candidate, but its parser/retrieval source layout was not a Code Graph RAG workload. No dedicated Code Graph RAG repository or safe representative parsing/indexing/query command was identified on the node. No live service, external data, model call, or proprietary source was profiled or sent to an external service. The synthetic workload above proves only that the tool's scan, port, verify, enable, bench, report, MCP, and rollback paths work.

Next step: identify the actual Code Graph RAG repository and a safe offline workload. Then capture its unmodified baseline and profile; do not claim acceleration until a real workload passes output-equivalence checks and the same five-run benchmark threshold (at least 20% lower end-to-end median).

## Project checks and limitations

- `bash -n scripts/fast-anything-pilot.sh` — PASS.
- Installer safe-uninstall check with temporary HOME directories — PASS: marked install removed; repeating uninstall returned 0; unmarked directory refused with exit 1 and its file remained. The real install was not removed.
- `scripts/fast-anything-pilot.sh install 0.8.6` and `check 0.8.6` — PASS; exact package version and CLI `--version` matched.
- Project `scripts/preflight.sh` — PASS.
- Project `scripts/test.sh` — attempted, exit 4: this worktree had no project dependencies installed and pytest could not import `fastapi`. The suite's session fixture also performs real Docker cleanup over `wm-*` resources; it was not safe to run that suite on a shared node without isolating its Docker daemon. No project test dependency installation or Docker test run was performed.
- One earlier smoke invocation exceeded the command's initial 30-second capture window; the generated profile showed it completed. A subsequent tracked scan completed with exit 0 and supplied the result above.
- No GitHub Actions rerun, production change, main merge, or force-push was performed.
