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

    def __init__(self, db, launchers=None, which=shutil.which):
        self.db = db
        self.launchers = launchers if launchers is not None else AGENT_LAUNCHERS
        self.which = which
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

    def start(self, *, task_id: int | None, workspace_id: int, agent: str, worktree_path: str | Path, mode: str = "INTERACTIVE") -> int:
        launcher = self.launchers.get(agent.lower())
        if not launcher:
            raise SessionError("AGENT_UNSUPPORTED", f"Agent not allowed: {agent}")
        executable = self.which(launcher.executable)
        if not executable:
            raise SessionError("AGENT_CLI_NOT_FOUND", f"{launcher.label} CLI chưa được cài hoặc không có trong PATH")
        argv = [executable, *launcher.args]
        cwd = str(worktree_path)
        if not Path(cwd).is_dir():
            raise SessionError("WORKTREE_NOT_FOUND", "Worktree không còn tồn tại")

        sid = self.db.execute(
            "INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status,mode,last_activity_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, workspace_id, agent.lower(), launcher.label, cwd, "STARTING", mode, now()),
        )
        try:
            pid, master_fd = pty.fork()
        except OSError as exc:
            self.db.execute("UPDATE agent_sessions SET status='FAILED',exited_at=? WHERE id=?", (now(), sid))
            raise SessionError("PTY_SPAWN_FAILED", str(exc)) from exc
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, os.environ.copy())
            finally:
                os._exit(127)

        def on_exit(exit_code):
            self.db.execute("UPDATE agent_sessions SET status='EXITED',exited_at=?,exit_code=? WHERE id=?", (now(), exit_code, sid))
            self._live.pop(sid, None)

        def on_activity():
            self.db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE id=?", (now(), sid))

        session = LivePtySession(sid, master_fd, pid, on_exit, on_activity)
        session.mode = mode
        self._live[sid] = session
        self.db.execute("UPDATE agent_sessions SET status='RUNNING',pid=? WHERE id=?", (pid, sid))
        return sid

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
        session = self._live.get(sid)
        if not session:
            return
        with session._lock:
            tail = bytes(session._buffer)[-20000:]
        self.db.execute("UPDATE agent_sessions SET transcript_tail=? WHERE id=?", (tail.decode("utf-8", "replace"), sid))

    def live_tail(self, sid: int, n: int = 20000) -> str | None:
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
        self.db.execute(
            "UPDATE agent_sessions SET status='FAILED',exited_at=? WHERE status IN ('RUNNING','STARTING','WAITING_FOR_INPUT')",
            (now(),),
        )
