# Terminal MCP runtime association

ProjectFlow records the tuple `task/workspace/backend/node/native_session/
durable_task` in `terminal_runtime_associations`. It is an idempotent mapping,
not another task queue. Terminal MCP remains the canonical owner of durable
delivery and its idempotency receipt; ProjectFlow remains the owner of repo,
branch and worktree validation and exact-tested-SHA gates.

Allowed backends are `tmux` and `herdr-core`. A workspace must belong to the
given task, and an existing association cannot silently change native session
or durable receipt. New rows begin `unknown/unobserved`; `blocked` or `done`
may be stored only through an explicit source-backed observation.

Open Codex/Open Claude launch integration must call Terminal MCP once, then
record its returned receipt through `TerminalRuntimeAssociations.record()`.
It must not launch a second agent or enqueue a second task on retry. Rollback
stops creating new associations; existing rows are audit metadata and do not
control or terminate any process.
