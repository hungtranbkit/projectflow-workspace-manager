# Requirements Quality Checklist — Draft

- [x] Source feature analysis distinguishes verified CrommaCode capabilities from proposed ProjectFlow additions.
- [x] Observable user scenarios include independent acceptance cases.
- [x] Separate dispatch, execution, and delivery/acceptance dimensions.
- [x] Preserve existing Spec Layer, Harness, one-worktree-per-agent, and exact-tested-commit rules.
- [x] Exclude public raw logs, passwords, prompts, paths and write actions.
- [x] Mark recovery, hook mutation and public deployment as later approval-gated phases.
- [x] Define measurable event replay, duplicate launch, freshness, dashboard and privacy criteria.
- [ ] Confirm CLI hook compatibility from installed versions on accessible node.
- [ ] Review exact event-store owner and available API surface against current code.
- [ ] Decide on retention, default stall threshold, p95 event delay baseline, and pilot nodes.
- [ ] Run real Spec Kit CLI initialization and command/skill generation in an isolated review branch; compare changes to existing .agents/skills and .claude/skills, no forced overwrite without diff review.
- [ ] Approve unique canonical FeatureSpec IDs and review YAML baseline change before creating behavior-changing tasks.

**Note:** These are authored Spec Kit-compatible SDD artifacts; this checklist does NOT claim that the official Specify CLI has run or that /speckit skills have been installed. The local runner could not reach PyPI; formal CLI adoption remains a reviewed task, not a silently completed installation.
