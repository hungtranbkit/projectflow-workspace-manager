# Coding-agent compatibility and installation

The **same canonical skill bundle** is installed on each agent; no model-specific rewrite or stripped-down prompt is used. A coding agent must support `SKILL.md` discovery, have permission to read local files, edit code, and (for real pixel QA) run a browser. A text-only model without image input cannot inspect a screenshot directly; make image content accessible through a supported tool and mark limitations honestly.

## Verified discovery paths (October 2026)

| Agent | Project-local | User/global | Activation / notes |
| --- | --- | --- | --- |
| Claude Code | `.claude/skills/image-to-code/` | `~/.claude/skills/image-to-code/` | `/image-to-code` or describe a screenshot task |
| OpenCode | `.opencode/skills/image-to-code/` | `~/.config/opencode/skills/image-to-code/` | Native `skill` tool; discovers Claude and `.agents` compatible paths too |
| Freebuff / Codebuff | `.agents/skills/image-to-code/` | `~/.agents/skills/image-to-code/` | Native skill discovery; `/skill:image-to-code` where supported |
| Hermes Agent | `.hermes/skills/image-to-code/` | `~/.hermes/skills/image-to-code/` | `/image-to-code` or Hermes skills list; **project skills require `hermes skills trust`** |
| Codex | `.agents/skills/image-to-code/` | `~/.agents/skills/image-to-code/` | `$image-to-code` or ask Codex to use the skill |
| Cursor | `.cursor/skills/image-to-code/` | `~/.cursor/skills/image-to-code/` | `/image-to-code` or `@image-to-code` |
| Gemini CLI | `.gemini/skills/image-to-code/` | `~/.gemini/skills/image-to-code/` | `/skills list`, prompt to activate; project may require workspace trust |
| Other Agent Skills agents | `.agents/skills/image-to-code/` | `~/.agents/skills/image-to-code/` | Works only if the agent implements that discovery path; otherwise read `SKILL.md` manually |

OpenCode may respect `$XDG_CONFIG_HOME/opencode/skills` instead of `~/.config/opencode/skills`; the installer uses it if set. Windows OpenCode global installation can use `%APPDATA%/opencode/skills` when available.

For `--agent all`, there are **six unique destination directories**, not eight. Freebuff, Codex and the generic Agent Skills route share `.agents/skills` and are installed only once. This avoids unnecessary duplication and conflicting copies.

## Install (Python 3.10+; no pip dependencies)

From a local clone of this repository:

```bash
# One specific agent, in an existing project
python scripts/install.py --agent claude --scope project --project /path/to/my-app

# All supported agents, in an existing project
python scripts/install.py --agent all --scope project --project /path/to/my-app

# Global in current user's home; customize selected agents
python scripts/install.py --agent claude,opencode,freebuff,hermes,codex,cursor,gemini --scope global

# Preview without writing
python scripts/install.py --agent all --project /path/to/my-app --dry-run

# Validate installed files against the repository source
python scripts/install.py --agent all --project /path/to/my-app --verify

# Explicitly upgrade existing installs; each previous install gets a backup
python scripts/install.py --agent all --project /path/to/my-app --force
```

On Windows, use `py -3 scripts/install.py ...` if `python` is unavailable. Project paths can contain spaces when quoted. Installer operations are local filesystem operations only: no downloads, network requests, agent configuration edits, telemetry or automatic execution of code. It copies the `SKILL.md`, `LICENSE`, `AUTHORS.md`, and bundled `scripts/`, `references/`, `examples/` and `assets/`. An existing installation is **not overwritten** without `--force`; `--force` preserves a uniquely named backup. `--verify` uses SHA-256.

For a different agent with unsupported automatic discovery, put the full folder where that agent supports skills, or explicitly ask it to read the local `SKILL.md`, then work from the original skill directory so relative script paths resolve. **Do not claim native skill registration without documentation and validation.**

## Agent behavior contract

- Inventory the supplied screenshot, including unknown/occluded elements.
- Decompose **background → sections → components → typography → icons → illustrations/images → details**.
- Create design spec and asset manifest; implement in the target project's own framework; preserve existing approved assets.
- Render mobile/tablet/desktop, test behavior, visually compare a matching viewport, fix regressions.
- Present mobile + tablet in a single labeled, readable side-by-side board. For a desktop screenshot, show the desktop original + reconstructed desktop side-by-side. For non-desktop references, present a desktop preview separately.
- If an agent's UI cannot render inline image files, create the exact PNG boards and supply their paths **while explicitly stating the UI limitation**. Do not pretend that a bare link is an inline image.
- Scripts are optional support; they do not themselves generate a React page or reverse engineer hidden assets. **Native agent compatibility is not equivalent to end-to-end visual homology.**

## Documentation checked

- Claude Code: https://code.claude.com/docs/en/skills
- OpenCode: https://opencode.ai/docs/skills/
- Freebuff/Codebuff: https://github.com/CodebuffAI/freebuff/blob/main/common/src/templates/initial-agents-dir/skills/README.md and https://www.codebuff.com/docs/tips/skills
- Hermes: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- Codex: https://developers.openai.com/blog/skills-agents-sdk
- Cursor: https://cursor.com/help/customization/skills
- Gemini CLI: https://geminicli.com/docs/cli/using-agent-skills/
