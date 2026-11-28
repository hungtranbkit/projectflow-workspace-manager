from __future__ import annotations
import fcntl
import hashlib
import os
import pty
import shutil
import signal
import struct
import termios
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.launchers import AGENT_LAUNCHERS
from app.services.secret_redaction import redact


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class LivePtySession:
    """One real PTY-backed process (pty.fork(), never subprocess stdout
    pipes -- interactive CLIs need a real controlling terminal). Owns the
    master fd, a background reader thread, a bounded in-memory ring
    buffer (for late WebSocket subscribers to catch up on), and the set
    of live subscriber callbacks. Everything here is in-process only --
    it does not outlive this Python process, which is exactly why
    AgentSessionManager.reconcile_on_startup() marks old rows honestly
    dead after a restart instead of pretending they're still RUNNING."""

    BUFFER_CAP = 200_000

    def __init__(self, session_id: int, master_fd: int, pid: int, on_exit, on_activity):
        self.id = session_id
        self.master_fd = master_fd
        self.pid = pid
        self.on_exit = on_exit
        self.on_activity = on_activity
        self.mode = "INTERACTIVE"
        self._subscribers: set = set()
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self.closed = False
        self.exit_code: int | None = None
        self._thread = threading.Thread(target=self._read_loop, daemon=True)

    def start_reader(self) -> None:
        self._thread.start()

    def _read_loop(self) -> None:
        while True:
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                self._finish()
                return
            with self._lock:
                self._buffer.extend(chunk)
                if len(self._buffer) > self.BUFFER_CAP:
                    del self._buffer[: len(self._buffer) - self.BUFFER_CAP]
                subs = list(self._subscribers)
            self.on_activity()
            for send in subs:
                try:
                    send(chunk)
                except Exception:
                    pass

    def _finish(self) -> None:
        try:
            _, status = os.waitpid(self.pid, os.WNOHANG)
            self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        except ChildProcessError:
            self.exit_code = None
        self.closed = True
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.on_exit(self.exit_code)

    def subscribe(self, send) -> bytes:
        """Register a live subscriber, return the current buffer tail so
        a newly-connected viewer sees recent scrollback immediately."""
        with self._lock:
            self._subscribers.add(send)
            return bytes(self._buffer)

    def unsubscribe(self, send) -> None:
        with self._lock:
            self._subscribers.discard(send)

    def write(self, data: bytes) -> None:
        if self.closed:
            raise SessionError("SESSION_CLOSED", "Session is no longer running")
        os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if self.closed:
            return
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def stop(self) -> None:
        if self.closed:
            return
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


class AgentSessionManager:
    """Spawns and tracks PTY-backed Agent Sessions. Command safety
    (section 14): the browser/caller supplies only `agent` (a name) and a
    caller-resolved, already-validated worktree path -- argv is ALWAYS
    derived from the trusted `launchers` registry (the same one the
    desktop launcher uses), never from client input. There is no code
    path that accepts a raw command or flags."""

    def __init__(self, db, launchers=None, which=shutil.which, final_output_root: str | Path | None = None):
        self.db = db
        self.launchers = launchers if launchers is not None else AGENT_LAUNCHERS
        self.which = which
        self.final_output_root = Path(final_output_root or (Path.home() / ".local" / "state" / "projectflow-workspace-manager" / "agent-final-messages")).resolve()
        self._live: dict[int, LivePtySession] = {}
        # Bounded CLI-readiness detection for deliver_prompt (section 3):
        # 'ready' means the PTY's output buffer has gone quiet for
        # `prompt_quiet_window` seconds -- a real interactive CLI stops
        # writing once it settles at its own idle prompt, and (observed
        # live against both Codex and Claude) only resumes continuous
        # output once it is actively processing a submitted prompt, never
        # while idle. Deliberately NOT parsing any agent-specific banner
        # text as a state machine -- overridable per-instance for tests.
        self.prompt_ready_timeout = 8.0
        self.prompt_quiet_window = 0.4

    def supports_final_output(self, agent: str) -> bool:
        launcher = self.launchers.get(agent.lower())
        return bool(launcher and launcher.completion_mode == "CODEX_EXEC")

    def is_process_live(self, session_id: int) -> bool:
        """Return true only for a tracked PTY with a live PID and recent
        output activity. A quiet/stale persisted RUNNING row is UNKNOWN to
        orchestration and must not trigger a duplicate executor."""
        session = self._live.get(int(session_id))
        if session is None or session.closed:
            return False
        try:
            os.kill(session.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        row=self.db.one("SELECT last_activity_at FROM agent_sessions WHERE id=?",(int(session_id),))
        if not row or not row.get("last_activity_at"):
            return False
        try:
            activity=datetime.fromisoformat(row["last_activity_at"].replace("Z","+00:00"))
            if activity.tzinfo is None:
                activity=activity.replace(tzinfo=timezone.utc)
        except (TypeError,ValueError):
            return False
        return (datetime.now(timezone.utc)-activity).total_seconds() <= 120

    def start(self, *, task_id: int | None, workspace_id: int, agent: str, worktree_path: str | Path, mode: str = "INTERACTIVE", initial_prompt: str | None = None, prompt_source: str | None = None, prompt_version: int | None = None) -> int:
        launcher = self.launchers.get(agent.lower())
        if not launcher:
            raise SessionError("AGENT_UNSUPPORTED", f"Agent not allowed: {agent}")
        executable = self.which(launcher.executable)
        if not executable:
            raise SessionError("AGENT_CLI_NOT_FOUND", f"{launcher.label} CLI chưa được cài hoặc không có trong PATH")
        use_final_output = initial_prompt is not None and launcher.completion_mode == "CODEX_EXEC"
        if initial_prompt is not None and not use_final_output:
            raise SessionError("EXECUTOR_TRANSPORT_UNSUPPORTED", "This trusted launcher does not support final-message capture")
        effective_mode = "VIEW_ONLY" if use_final_output else mode
        cwd = str(worktree_path)
        if not Path(cwd).is_dir():
            raise SessionError("WORKTREE_NOT_FOUND", "Worktree không còn tồn tại")

        if use_final_output:
            self.final_output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.final_output_root, 0o700)
            except OSError:
                pass

        sid = self.db.execute(
            "INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status,mode,last_activity_at,completion_transport,final_output_status,prompt_status,prompt_version,prompt_sha256,prompt_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, workspace_id, agent.lower(), launcher.label, cwd, "STARTING", effective_mode, now(),
             "CODEX_EXEC" if use_final_output else "PTY_INTERACTIVE", "PENDING" if use_final_output else "NOT_CONFIGURED",
             "PENDING" if use_final_output else "PENDING", prompt_version if use_final_output else None,
             hashlib.sha256(initial_prompt.encode("utf-8")).hexdigest() if use_final_output else None,
             prompt_source if use_final_output else None),
        )
        final_output_path = None
        argv = [executable, *launcher.args]
        prompt_pipe = None
        if use_final_output:
            final_output_path = (self.final_output_root / f"session-{sid}-final-message.txt").resolve()
            if final_output_path.exists() or final_output_path.is_symlink():
                self.db.execute("UPDATE agent_sessions SET status='FAILED',exited_at=?,final_output_status='PATH_COLLISION' WHERE id=?", (now(), sid))
                raise SessionError("FINAL_OUTPUT_PATH_COLLISION", "Final-message output path already exists")
            self.db.execute("UPDATE agent_sessions SET completion_transport='CODEX_EXEC',final_output_path=? WHERE id=?", (str(final_output_path), sid))
            argv = [executable, "exec", "--json", "--output-last-message", str(final_output_path), *launcher.args, "-"]
            prompt_pipe = os.pipe()
        try:
            pid, master_fd = pty.fork()
        except OSError as exc:
            if prompt_pipe:
                os.close(prompt_pipe[0]); os.close(prompt_pipe[1])
            self.db.execute("UPDATE agent_sessions SET status='FAILED',exited_at=? WHERE id=?", (now(), sid))
            raise SessionError("PTY_SPAWN_FAILED", str(exc)) from exc
        if pid == 0:
            try:
                if prompt_pipe:
                    os.close(prompt_pipe[1])
                    os.dup2(prompt_pipe[0], 0)
                    if prompt_pipe[0] != 0:
                        os.close(prompt_pipe[0])
                os.chdir(cwd)
                os.execvpe(argv[0], argv, os.environ.copy())
            finally:
                os._exit(127)
        if prompt_pipe:
            os.close(prompt_pipe[0])
            try:
                payload = memoryview(initial_prompt.encode("utf-8"))
                while payload:
                    written = os.write(prompt_pipe[1], payload)
                    payload = payload[written:]
            except (BrokenPipeError, OSError) as exc:
                try: os.close(prompt_pipe[1])
                except OSError: pass
                try: os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError: pass
                try: os.close(master_fd)
                except OSError: pass
                self.db.execute("UPDATE agent_sessions SET status='FAILED',exited_at=?,prompt_status='FAILED',final_output_status='INPUT_FAILED' WHERE id=?", (now(), sid))
                raise SessionError("CODEX_PROMPT_PIPE_FAILED", "Could not deliver the trusted initial prompt") from exc
            finally:
                try: os.close(prompt_pipe[1])
                except OSError: pass
            self.db.execute("UPDATE agent_sessions SET prompt_status='DELIVERED',delivered_at=? WHERE id=?", (now(), sid))

        def on_exit(exit_code):
            # Persist the final transcript on every real process exit, not
            # only on a browser's WS disconnect -- a session nobody was
            # watching (or that outlives the browser tab) must not lose
            # its completion report just because no one was connected
            # when it printed one.
            self.persist_tail(sid)
            if use_final_output:
                self.capture_final_output(sid, confirmed_exit=True)
            self.db.execute("UPDATE agent_sessions SET status='EXITED',exited_at=?,exit_code=? WHERE id=?", (now(), exit_code, sid))
            self._live.pop(sid, None)

        def on_activity():
            self.db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE id=?", (now(), sid))

        session = LivePtySession(sid, master_fd, pid, on_exit, on_activity)
        session.mode = effective_mode
        self._live[sid] = session
        self.db.execute("UPDATE agent_sessions SET status='RUNNING',pid=? WHERE id=?", (pid, sid))
        session.start_reader()
        return sid

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True

    def capture_final_output(self, sid: int, *, confirmed_exit: bool = False) -> bool:
        """Capture Codex's own --output-last-message artifact only after
        process exit. Never fall back to PTY text for CODEX_EXEC sessions,
        because the PTY may contain the echoed input prompt."""
        row = self.db.one("SELECT pid,completion_transport,final_output_path,final_output_status FROM agent_sessions WHERE id=?", (sid,))
        if not row or row["completion_transport"] != "CODEX_EXEC":
            return False
        if row["final_output_status"] in ("CAPTURED", "REDACTED", "TOO_LARGE", "INVALID_UTF8", "MISSING", "PATH_INVALID"):
            return row["final_output_status"] == "CAPTURED"
        if not confirmed_exit and self._pid_alive(row.get("pid")):
            return False
        path_value = row.get("final_output_path")
        if not path_value:
            self.db.execute("UPDATE agent_sessions SET final_output_status='MISSING' WHERE id=?", (sid,))
            return False
        path = Path(path_value)
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self.final_output_root.resolve())
            if path.is_symlink():
                raise ValueError("invalid output file")
        except (OSError, ValueError):
            self.db.execute("UPDATE agent_sessions SET final_output_status='PATH_INVALID' WHERE id=?", (sid,))
            return False
        if not resolved.exists():
            self.db.execute("UPDATE agent_sessions SET final_output_status='MISSING' WHERE id=?", (sid,))
            return False
        if not resolved.is_file():
            self.db.execute("UPDATE agent_sessions SET final_output_status='PATH_INVALID' WHERE id=?", (sid,))
            return False
        try:
            payload = resolved.read_bytes()
        except OSError:
            self.db.execute("UPDATE agent_sessions SET final_output_status='MISSING' WHERE id=?", (sid,))
            return False
        if not payload:
            status, text, digest = "MISSING", "", None
        elif len(payload) > 512_000:
            status, text, digest = "TOO_LARGE", "", hashlib.sha256(payload).hexdigest()
        else:
            digest = hashlib.sha256(payload).hexdigest()
            try:
                raw_text = payload.decode("utf-8")
            except UnicodeDecodeError:
                status, text = "INVALID_UTF8", ""
            else:
                text = redact(raw_text)
                status = "CAPTURED" if text == raw_text else "REDACTED"
        self.db.execute("UPDATE agent_sessions SET final_output_status=?,final_output_sha256=?,final_output_text=? WHERE id=?", (status, digest, text, sid))
        if status == "CAPTURED":
            owner = self.db.one("SELECT workspace_id FROM agent_sessions WHERE id=?", (sid,))
            if owner:
                self.db.event("agent", owner["workspace_id"], "EXECUTOR_FINAL_OUTPUT_CAPTURED", f"session={sid} sha256={digest}")
        try:
            resolved.unlink(missing_ok=True)
        except (UnboundLocalError, OSError):
            pass
        return status == "CAPTURED"

    def _wait_ready(self, session: LivePtySession) -> bool:
        """Bounded readiness wait (section 3): true only once the PTY's
        output has gone quiet for `prompt_quiet_window` seconds, within
        `prompt_ready_timeout` overall. Returns False (never blocks
        forever, never guesses) if the session closes or the CLI is
        still actively rendering when the deadline hits -- callers must
        treat False as a real, reportable delivery failure, not retry
        silently."""
        deadline = time.monotonic() + self.prompt_ready_timeout
        last_len = -1
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            if session.closed:
                return False
            with session._lock:
                cur_len = len(session._buffer)
            t = time.monotonic()
            if cur_len != last_len:
                last_len = cur_len
                last_change = t
            elif cur_len > 0 and (t - last_change) >= self.prompt_quiet_window:
                return True
            time.sleep(0.03)
        return False

    def deliver_prompt(self, sid: int, prompt: str, source: str, version) -> bool:
        """Sends the exact, already-generated, trusted Builder Prompt
        into a live session's stdin as one bracketed paste (so a
        multi-line prompt is never mistaken for several separate Enter
        presses by the agent's own TUI) followed by a single submit
        keystroke. This is the ONLY prompt text this method ever sends --
        never anything supplied by a browser request (section 2: 'do not
        accept arbitrary raw command execution from browser'). Persists
        prompt_status/prompt_version/prompt_sha256/prompt_source/
        delivered_at so 'Agent RUNNING' is never conflated with 'prompt
        actually delivered' (section 2)."""
        session = self._live.get(sid)
        if not session or not self._wait_ready(session):
            self.db.execute("UPDATE agent_sessions SET prompt_status='FAILED' WHERE id=?", (sid,))
            return False
        try:
            session.write(("\x1b[200~" + prompt + "\x1b[201~\r").encode("utf-8"))
        except SessionError:
            self.db.execute("UPDATE agent_sessions SET prompt_status='FAILED' WHERE id=?", (sid,))
            return False
        sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.db.execute(
            "UPDATE agent_sessions SET prompt_status='DELIVERED',prompt_version=?,prompt_sha256=?,prompt_source=?,delivered_at=? WHERE id=?",
            (version, sha256, source, now(), sid),
        )
        return True

    def get(self, sid: int) -> LivePtySession | None:
        return self._live.get(sid)

    def stop(self, sid: int) -> None:
        session = self._live.get(sid)
        if session:
            session.stop()

    def set_mode(self, sid: int, mode: str) -> None:
        session = self._live.get(sid)
        if session:
            session.mode = mode
        self.db.execute("UPDATE agent_sessions SET mode=? WHERE id=?", (mode, sid))

    def persist_tail(self, sid: int) -> None:
        """Persists the FULL live buffer (already capped at
        LivePtySession.BUFFER_CAP), not some smaller re-truncation of it.

        A real bug, found via live production verification: this used to
        downsample to a fixed 20_000-byte tail on every WS disconnect,
        independent of the live buffer's own (larger) 200_000-byte cap.
        A real Codex TUI redraws its whole screen (cursor moves, spinner
        frames, style resets) for every printed line, so a 20_000-byte
        window can hold only a few thousand characters of actual content
        -- nowhere near enough to still contain a real completion
        report's WORK_STATUS marker by the time WHAT_CHANGED appears.
        Persisting the same window the live session already keeps means
        a session that exits or gets disconnected loses no more than an
        already-live session's own view would have."""
        session = self._live.get(sid)
        if not session:
            return
        with session._lock:
            tail = bytes(session._buffer)[-session.BUFFER_CAP:]
        # B0.7: pattern-based redaction (app/services/secret_redaction.py)
        # on every persisted transcript -- a real Builder session's own
        # PTY output can print a credential an agent's tool/environment
        # emitted (never routed through SecretsService at all), and a
        # transcript becomes real multi-tenant-visible surface once
        # B0.2's organizations exist. Applied unconditionally (cheap,
        # pure-text, no DB/org lookup needed) regardless of AUTH_MODE --
        # this is a strict improvement over today's exact behavior, not
        # a new gate, so it carries no AUTH_MODE=none precedent concern.
        text = redact(tail.decode("utf-8", "replace"))
        self.db.execute("UPDATE agent_sessions SET transcript_tail=? WHERE id=?", (text, sid))

    def persist_all_live_tails(self) -> int:
        """Flush each owned PTY ring before graceful app shutdown so a
        subsequent startup reconciliation can retain real completion output."""
        session_ids = list(self._live)
        for sid in session_ids:
            self.persist_tail(sid)
        return len(session_ids)

    def live_tail(self, sid: int, n: int = LivePtySession.BUFFER_CAP) -> str | None:
        """The session's current transcript, read straight from the live
        in-process PTY buffer when the session is still running --
        transcript_tail on the DB row is only ever refreshed on WS
        disconnect (persist_tail), so a session nobody has opened the web
        terminal for yet would otherwise look like it has said nothing at
        all. Falls back to the last-persisted DB tail once the process
        (and its in-memory buffer) is gone. None only when there is
        genuinely no transcript anywhere."""
        session = self._live.get(sid)
        if session:
            with session._lock:
                return bytes(session._buffer)[-n:].decode("utf-8", "replace")
        row = self.db.one("SELECT transcript_tail FROM agent_sessions WHERE id=?", (sid,))
        return row["transcript_tail"] if row and row.get("transcript_tail") else None

    def reconcile_on_startup(self) -> None:
        """A server restart honestly loses every in-process PTY -- any row
        still RUNNING/STARTING/WAITING_FOR_INPUT belonged to a previous
        process and never has a live session in this one. Mark it FAILED
        rather than let the UI claim a session is running when it is not
        (section 20: detect the loss, never falsely report RUNNING)."""
        # Codex exec writes its final assistant message to a durable file
        # before process exit. If the app stopped after that child exited
        # but before its callback persisted the file, recover it now. A
        # still-live/unknown PID is never accepted as an exited executor.
        pending = self.db.all("SELECT id FROM agent_sessions WHERE status IN ('RUNNING','STARTING','WAITING_FOR_INPUT') AND completion_transport='CODEX_EXEC' AND final_output_status='PENDING'")
        for row in pending:
            self.capture_final_output(row["id"])
        self.db.execute(
            "UPDATE agent_sessions SET status='FAILED',exited_at=? WHERE status IN ('RUNNING','STARTING','WAITING_FOR_INPUT')",
            (now(),),
        )
