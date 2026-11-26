"""Bounded, deduplicated reruns of the pinned architecture tools (#91).

Requests go through the existing durable Harness gate (`HarnessStore`):
one child goal per (project, commit, toolset, input IR) -- an identical request
is never executed twice -- with the Harness WIP limit, a TTL and an event log.
A run only ever reads the configured repository's Git objects, renders inside a
network-less, read-only container with CPU/memory/PID/time caps, and imports its
output through the same validation path as a manual import. Nothing a browser
sends becomes a path, ref or command: the browser can only name a configured
project (and optionally a full commit SHA that must already exist locally).
"""

from __future__ import annotations

import fnmatch
import fcntl
import stat
import uuid
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from app.services.architecture_center import (
    MAX_AUX,
    PROJECT_RE,
    SHA1_RE,
    ArtifactRejected,
    canonical,
    read_regular_file,
    sha256,
)
from app.services.git_workspace import GitCommandError, GitSafetyError, ReadOnlySource

RUN_TTL = timedelta(minutes=45)
MAX_ATTEMPTS = 3
MAIN_GOAL = "architecture:"
IMAGE_RE = re.compile(r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
DEFAULT_LIMITS = dict(
    timeout_seconds=1200, memory="3g", cpus="2", pids=512, max_active=1, min_free_gib=5,
    max_source_files=4000, max_source_bytes=64 * 1024 * 1024, max_file_bytes=1024 * 1024, keep_run_dirs=3,
)


class RunRefused(ValueError):
    """Gate refusal with an operator-safe message."""


class LaunchRefused(RunRefused):
    """Definitive refusal before any executor could have been submitted."""


TERMINAL_RUNS = frozenset({"DONE_VERIFIED", "FAILED", "EXPIRED"})
OWNER_LABEL = "projectflow.architecture-owner"


def write_receipt(path: Path, value: dict) -> None:
    """Atomically persist private lifecycle evidence before the next side effect."""
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(canonical(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def read_receipt(path: Path) -> dict:
    value = json.loads(read_regular_file(path, MAX_AUX))
    if not isinstance(value, dict):
        raise RunRefused("Invalid lifecycle receipt")
    return value


@contextmanager
def execution_lock(d: Path):
    """One executor or reconciler per run; never follow a lock symlink."""
    if d.is_symlink() or not d.is_dir():
        raise RunRefused("Run directory is missing or unsafe")
    fd = os.open(d / "execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RunRefused("Unsafe run lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunRefused("Run executor or reconciler is still active") from exc
        yield
    finally:
        os.close(fd)


def bound_request(d: Path, task) -> dict:
    req = read_receipt(d / "request.json")
    owner = req.get("owner")
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise RunRefused("Missing durable run owner")
    commit = req.get("commit")
    if not isinstance(commit, str) or not SHA1_RE.fullmatch(commit):
        raise RunRefused("Missing pinned run source")
    expected = f"pf-arch-run-{task.id}-{owner[:12]}-{commit[:10]}"
    if req.get("unit") != expected or task.external_task_id != expected or task.main_goal != MAIN_GOAL + req.get("project", ""):
        raise RunRefused("Run ownership mismatch")
    return req


def assert_executor_active(d: Path) -> dict:
    store = harness(d.parent.parent)
    store.reconcile_due(at=datetime.now(timezone.utc))
    task = store.get(int(d.name))
    if task.state not in ("RUNNING_VERIFIED", "STALLED"):
        raise RunRefused("Run is not eligible to execute another tool")
    return bound_request(d, task)



def state_dir() -> Path:
    db = os.getenv("PROJECTFLOW_STEWARD_DB")
    return Path(db).parent if db else Path.home() / ".local/state/projectflow-steward"


def config_path() -> Path:
    return Path(os.getenv("PROJECTFLOW_ARCHITECTURE_CONFIG") or Path.home() / ".config/projectflow-steward/architecture.json")


def load_config(path: Path | None = None) -> dict:
    """Operator-owned private config (0600). Missing file = no project can be rerun."""
    path = Path(path or config_path())
    if not path.is_file():
        return {"projects": {}, "tools": {}, "limits": dict(DEFAULT_LIMITS)}
    st = path.stat()
    if st.st_mode & 0o077:
        raise RunRefused("Cấu hình kiến trúc phải có quyền 0600.")
    cfg = json.loads(read_regular_file(path, MAX_AUX))
    if not isinstance(cfg, dict) or set(cfg) - {"projects", "tools", "limits"}:
        raise RunRefused("Cấu hình kiến trúc không hợp lệ.")
    projects = cfg.get("projects", {})
    for pid, p in projects.items():
        if not PROJECT_RE.fullmatch(pid) or not isinstance(p, dict):
            raise RunRefused("Cấu hình dự án không hợp lệ.")
        need = {"repo", "ref", "include_prefixes", "include_suffixes"}
        allowed = need | {"exclude_globs", "archify_ir", "floorplan_architecture", "branch_label"}
        if need - set(p) or set(p) - allowed:
            raise RunRefused("Cấu hình dự án thiếu hoặc thừa trường.")
    tools = cfg.get("tools", {})
    for name in ("archify", "floorplan"):
        t = tools.get(name)
        if t is not None and not (isinstance(t, dict) and SHA1_RE.fullmatch(t.get("commit", "")) and t.get("path")):
            raise RunRefused("Công cụ phải được ghim bằng commit đầy đủ.")
    rt = tools.get("runtime")
    if rt is not None and not (isinstance(rt, dict) and IMAGE_RE.fullmatch(rt.get("image", ""))):
        raise RunRefused("Runtime phải được ghim bằng image digest.")
    cfg["limits"] = {**DEFAULT_LIMITS, **cfg.get("limits", {})}
    return cfg


def tracked_revision(cfg: dict, project: str) -> dict | None:
    p = cfg.get("projects", {}).get(project)
    if not p:
        return None
    try:
        src = ReadOnlySource(p["repo"])
        return {"ref": p.get("branch_label") or p["ref"], "commit": src.resolve(p["ref"]), "error": None, "source": src}
    except (GitSafetyError, GitCommandError, OSError) as exc:
        return {"ref": p.get("branch_label") or p["ref"], "commit": None, "error": "Không đọc được nguồn: " + str(exc)[:120]}


def with_distance(tracked: dict | None, commit: str) -> dict | None:
    if not tracked or not tracked.get("commit"):
        return tracked
    src = tracked.get("source")
    dist = None
    if src and tracked["commit"] != commit:
        try:
            dist = src.distance(commit, tracked["commit"]) if src.is_ancestor(commit, tracked["commit"]) else None
        except (GitSafetyError, GitCommandError):
            dist = None
    return {k: v for k, v in tracked.items() if k != "source"} | {"distance": dist}


def tool_identity(cfg: dict) -> dict:
    t = cfg.get("tools", {})
    return {k: {kk: t[k].get(kk) for kk in ("commit", "version", "image") if t[k].get(kk)} for k in ("archify", "floorplan", "runtime") if k in t}


def verify_tools(cfg: dict) -> None:
    """A tool runs only from a clean checkout at exactly its pinned commit."""
    for name in ("archify", "floorplan"):
        t = cfg.get("tools", {}).get(name)
        if not t:
            raise RunRefused(f"Chưa cấu hình công cụ {name}.")
        src = ReadOnlySource(t["path"])
        if src.resolve("HEAD") != t["commit"]:
            raise RunRefused(f"{name} không ở đúng commit đã ghim.")
        dirty = src._git("status", "--porcelain", "--untracked-files=normal").stdout.strip()
        if dirty:
            raise RunRefused(f"{name} có thay đổi chưa ghim.")
    if not cfg.get("tools", {}).get("runtime"):
        raise RunRefused("Chưa ghim runtime container.")


def harness(state: Path | None = None) -> HarnessStore:
    d = state or state_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return HarnessStore(d / "architecture-runs.sqlite3")


def run_dir(task_id: int, state: Path | None = None) -> Path:
    return (state or state_dir()) / "architecture-runs" / str(int(task_id))


def runs(project: str, state: Path | None = None) -> list[dict]:
    path = (state or state_dir()) / "architecture-runs.sqlite3"
    if not path.is_file():
        return []
    h = harness(state)
    h.reconcile_due(at=datetime.now(timezone.utc))
    out = []
    for t in reversed(h.list_tasks(main_goal=MAIN_GOAL + project)):
        req = run_dir(t.id, state) / "request.json"
        request = json.loads(req.read_text()) if req.is_file() else {}
        out.append(dict(id=t.id, state=t.state, cleanup=t.cleanup_state, created_at=t.created_at,
                        unit=t.external_task_id, commit=request.get("commit"), events=h.events(t.id)))
    return out


def _free_gib(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 2**30


def request_rerun(project: str, commit: str | None = None, archify_ir: Path | None = None, *,
                  cfg: dict | None = None, state: Path | None = None, launcher=None, now=None) -> dict:
    """Create (or find the existing) gated run for exactly one commit and toolset."""
    cfg = cfg or load_config()
    p = cfg.get("projects", {}).get(project)
    if not p:
        raise RunRefused("Dự án chưa được cấu hình để chạy lại.")
    if commit is not None and not SHA1_RE.fullmatch(commit):
        raise RunRefused("Commit phải là SHA đầy đủ 40 ký tự.")
    verify_tools(cfg)
    try:
        src = ReadOnlySource(p["repo"])
        sha = src.resolve(commit or p["ref"])
    except (GitSafetyError, GitCommandError) as exc:
        raise RunRefused("Không tìm thấy commit trong kho đã cấu hình.") from exc
    ir_path = Path(archify_ir or p.get("archify_ir") or "")
    ir = read_regular_file(ir_path, MAX_AUX) if str(ir_path) not in ("", ".") else b""
    fp_seed = Path(p["floorplan_architecture"]) if p.get("floorplan_architecture") else None
    seed = read_regular_file(fp_seed, MAX_AUX) if fp_seed else b""
    state = state or state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    limits = cfg["limits"]
    if _free_gib(state) < limits["min_free_gib"]:
        raise RunRefused("Node thiếu dung lượng đĩa; từ chối chạy.")
    toolset = sha256(canonical(tool_identity(cfg)).encode())[:16]
    child = f"rerun:{project}:{sha}:{toolset}:{sha256(ir)[:16]}:{sha256(seed)[:16]}"
    h = harness(state)
    at = now or datetime.now(timezone.utc)
    h.reconcile_due(at=at)
    # The production admission path can reconcile completed work from an earlier
    # request. Injected launchers are isolated test/adapter paths, never probe host tools.
    if launcher is None:
        for old in h.list_tasks():
            if (old.main_goal.startswith(MAIN_GOAL) and old.state in TERMINAL_RUNS
                    and old.cleanup_state == "CLEANUP_PENDING"):
                reconcile_run_cleanup(old.id, state)
    tries = [t for t in h.list_tasks() if t.child_goal == child or t.child_goal.startswith(child + "#")]
    for t in reversed(tries):
        if t.state not in ("FAILED", "EXPIRED"):
            return dict(task_id=t.id, state=t.state, commit=sha, duplicate=True,
                        message=f"Đã có lượt chạy #{t.id} cho đúng commit và bộ công cụ này ({t.state}); không chạy lặp.")
    if len(tries) >= MAX_ATTEMPTS:
        raise RunRefused(f"Đã thử {len(tries)} lần cho đúng commit và bộ công cụ này; cần đổi đầu vào hoặc xử lý thủ công.")
    if tries:
        child += f"#retry-{len(tries)}"  # a failed attempt is retried as a new, visible child goal
    active = [t for t in h.list_tasks() if t.main_goal.startswith(MAIN_GOAL)
              and (t.state not in ("DONE_VERIFIED", "FAILED", "EXPIRED") or t.cleanup_state == "CLEANUP_PENDING")]
    if len(active) >= limits["max_active"]:
        raise RunRefused(f"Đang có {len(active)} lượt chạy kiến trúc chưa xong; giới hạn {limits['max_active']}.")
    try:
        task = h.create(key=hashlib.sha256(child.encode()).hexdigest()[:40], main_goal=MAIN_GOAL + project,
                        child_goal=child, dod="both native outputs rendered from one pinned commit, validated and imported",
                        agent="architecture-runner", node=os.uname().nodename[:60] or "local", created_at=at, ttl=RUN_TTL,
                        scope_limit=(MAIN_GOAL, limits["max_active"]))
    except HarnessError as exc:
        raise RunRefused(str(exc)) from exc
    d = run_dir(task.id, state)
    d.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in (("archify-ir.json", ir), ("floorplan-architecture.json", seed)):
        fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
    owner = uuid.uuid4().hex
    unit = f"pf-arch-run-{task.id}-{owner[:12]}-{sha[:10]}"
    req = dict(project=project, commit=sha, toolset=toolset, archify_ir_sha256=sha256(ir), floorplan_seed_sha256=sha256(seed),
               requested_at=int(at.timestamp()), owner=owner, unit=unit)
    write_receipt(d / "request.json", req)
    write_receipt(d / "launch.json", {"owner": owner, "state": "REQUESTED"})
    # The receipt (unit name) is durable BEFORE the process can start, so the
    # runner can always record progress against a dispatched task.
    h.dispatch(task.id, external_task_id=unit, at=at)
    try:
        (launcher or launch_unit)(task.id, unit, cfg)
    except Exception as exc:
        at_fail = now or datetime.now(timezone.utc)
        definitive = isinstance(exc, LaunchRefused)
        write_receipt(d / "launch.json", {"owner": owner, "state": "REFUSED" if definitive else "AMBIGUOUS"})
        try:
            h.fail(task.id, at=at_fail, reason="launch failed: " + type(exc).__name__)
        except HarnessError:
            pass  # a late executor may already have produced a terminal result
        if definitive:
            # Only an explicitly pre-submission refusal proves no runtime ever existed.
            h.cleanup(task.id, at=at_fail, executor_stopped=True, no_active_refs=True,
                      dirty_worktree=False, evidence_preserved=True, deletion_verified=True)
        raise RunRefused("Không khởi chạy được lượt chạy; đã ghi nhận thất bại.") from exc
    write_receipt(d / "launch.json", {"owner": owner, "state": "ACCEPTED"})
    return dict(task_id=task.id, state="DISPATCHED", commit=sha, duplicate=False,
                message=f"Đã xếp lượt chạy #{task.id} cho commit {sha[:12]}.")


def launch_unit(task_id: int, unit: str, cfg: dict) -> None:
    """Start the runner as a transient user unit with a hard runtime cap."""
    lim = cfg["limits"]
    repo = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ("PROJECTFLOW_STEWARD_DB", "PROJECTFLOW_ARCHITECTURE_CONFIG") if k in os.environ}
    argv = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
            f"--property=RuntimeMaxSec={int(lim['timeout_seconds']) + 120}", "--property=KillMode=control-group",
            f"--property=MemoryMax={str(lim['memory']).upper()}", f"--working-directory={repo}",
            *[f"--setenv={k}={v}" for k, v in env.items()],
            os.path.realpath(sys.executable), "-m", "scripts.architecture_rerun", "run", "--task-id", str(int(task_id))]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, shell=False)
    except FileNotFoundError as exc:
        raise LaunchRefused("systemd-run executable unavailable") from exc
    if proc.returncode:
        raise RunRefused("systemd-run returned an unconfirmed submission result")


def select_sources(tree: list, p: dict, limits: dict) -> tuple[list, list]:
    """Tracked regular files under the allowlist; symlinks, submodules and oversize files are refused."""
    chosen, excluded = [], []
    for mode, obj, size, path in tree:
        if not path.startswith(tuple(p["include_prefixes"])) or not path.endswith(tuple(p["include_suffixes"])):
            continue
        parts = path.split("/")
        if any(x in ("", ".", "..") for x in parts) or any(fnmatch.fnmatch(path, g) for g in p.get("exclude_globs", [])):
            excluded.append(path)
            continue
        if mode not in ("100644", "100755") or not 0 <= size <= limits["max_file_bytes"]:
            excluded.append(path)
            continue
        chosen.append((obj, size, path))
    if len(chosen) > limits["max_source_files"] or sum(s for _, s, _ in chosen) > limits["max_source_bytes"]:
        raise RunRefused("Nguồn vượt giới hạn tệp/dung lượng.")
    return chosen, excluded


def materialize(src: ReadOnlySource, sha: str, p: dict, limits: dict, dest: Path) -> dict:
    """Write the allowlisted files of one commit into a fresh directory, then make it read-only."""
    chosen, excluded = select_sources(src.tree(sha), p, limits)
    data = src.blobs([o for o, _, _ in chosen])
    files = []
    dest.mkdir(mode=0o700)
    for obj, size, rel in chosen:
        target = dest / rel
        if dest.resolve() not in target.resolve().parents:
            raise RunRefused("Đường dẫn nguồn không an toàn.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as fh:
            fh.write(data[obj])
        files.append(dict(path=rel, sha256=sha256(data[obj]), bytes=size))
    manifest = dict(repository=p.get("label", ""), inputCommit=sha, method="git objects of one commit; allowlisted tracked regular files; no .git",
                    includedCount=len(files), excludedCount=len(excluded), excludedPaths=sorted(excluded), files=files)
    return manifest


def make_read_only(root: Path, writable_dirs: tuple[Path, ...] = (), writable_trees: tuple[Path, ...] = ()) -> None:
    """Files become 0444 and directories 0555, except the exact directories in
    writable_dirs (their contents still become read-only) and whole writable_trees."""
    dirs = {d.resolve() for d in writable_dirs}
    trees = {t.resolve() for t in writable_trees}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        d = Path(dirpath).resolve()
        if d in trees or trees & set(d.parents):
            continue
        for f in filenames:
            os.chmod(d / f, 0o444)
        if d not in dirs:
            os.chmod(d, 0o555)


def container(cfg: dict, mounts: list[tuple[Path, str, str]], argv: list[str], timeout: int,
              *, run: Path, step: str) -> dict:
    """Persist exact ownership before Docker; distinguish tool exit from unknown launch."""
    req = assert_executor_active(run)
    owner = req["owner"]
    if not re.fullmatch(r"[a-z0-9-]{1,40}", step):
        raise RunRefused("Invalid tool step")
    receipt = run / f"container-{step}.json"
    if receipt.exists():
        raise RunRefused("Tool step already has a receipt")
    name = f"pf-arch-{owner}-{step}"
    info = {"owner": owner, "name": name, "state": "CREATING"}
    write_receipt(receipt, info)
    lim = cfg["limits"]
    image = cfg["tools"]["runtime"]["image"]
    cmd = ["docker", "create", "--name", name, "--pull=never", "--network", "none",
           "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--pids-limit", str(lim["pids"]), "--memory", str(lim["memory"]),
           "--cpus", str(lim["cpus"]), "--user", f"{os.getuid()}:{os.getgid()}",
           "--tmpfs", "/tmp:rw,size=512m", "-e", "HOME=/tmp", "-e", "CI=1", "-e", "NO_COLOR=1",
           "-e", "ARCHIFY_NO_UPDATE_CHECK=1", "-e", "GIT_CONFIG_NOSYSTEM=1",
           "-e", "GIT_NO_LAZY_FETCH=1", "-e", "GIT_ALLOW_PROTOCOL=",
           "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*",
           "--label", "projectflow.architecture-run=1", "--label", f"{OWNER_LABEL}={owner}"]
    for host, inside, mode in mounts:
        cmd += ["-v", f"{host}:{inside}:{mode}"]
    cmd += ["--entrypoint", argv[0], image, *argv[1:]]
    started = time.time()
    try:
        created = subprocess.run(cmd, capture_output=True, timeout=min(timeout, 60), shell=False)
        cid = created.stdout.decode("ascii", "strict").strip()
        if created.returncode or not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise RunRefused("Docker create outcome unverified")
        info.update(id=cid, state="CREATED")
        write_receipt(receipt, info)
        assert_executor_active(run)
        info["state"] = "STARTING"
        write_receipt(receipt, info)
        proc = subprocess.run(["docker", "start", "-a", cid], capture_output=True, timeout=timeout, shell=False)
        inspected = subprocess.run(["docker", "inspect", "--type", "container", cid], capture_output=True,
                                   timeout=10, shell=False)
        if inspected.returncode:
            raise RunRefused("Docker terminal state unverified")
        row, = json.loads(inspected.stdout)
        st = row["State"]
        if (row["Id"] != cid or row["Name"] != "/" + name or row["Config"]["Labels"].get(OWNER_LABEL) != owner
                or st.get("Status") != "exited" or st.get("Running") is not False
                or st.get("Restarting") is not False or st.get("Paused") is not False
                or st.get("Pid") != 0 or type(st.get("ExitCode")) is not int):
            raise RunRefused("Docker terminal identity or state unverified")
        code = st["ExitCode"]
        info.update(state="EXITED", exit=code)
        write_receipt(receipt, info)
        # Non-forced removal of exactly the inspected stopped container. Failure
        # leaves its receipt and blocks capacity until the supervisor verifies it.
        removed = subprocess.run(["docker", "rm", cid], capture_output=True, timeout=10, shell=False)
        if removed.returncode:
            raise RunRefused("Stopped container removal not verified")
        info["state"] = "REMOVED"
        write_receipt(receipt, info)
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        text = re.sub(r"/home/[^/\\s]+/", "~/", text)
        return dict(exit=code, seconds=round(time.time() - started, 1), tail=text[-2000:])
    except Exception as exc:
        # A timeout or lost daemon reply is not proof of cancellation. No next
        # tool step, forced removal, or automatic retry on an ambiguous receipt.
        if info["state"] not in ("EXITED", "REMOVED"):
            info["state"] = "UNCERTAIN"
        write_receipt(receipt, info)
        raise RunRefused("Container execution or cleanup needs reconciliation") from exc


def runtime_absent(req: dict) -> bool:
    """Query the exact unit and owner-labelled containers, never infer from exit 124."""
    try:
        unit = subprocess.run(["systemctl", "--user", "show", req["unit"] + ".service",
                               "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                               "-p", "MainPID", "-p", "ControlPID", "-p", "Job"],
                              capture_output=True, text=True, timeout=10, shell=False)
        if unit.returncode:
            return False
        values = dict(x.split("=", 1) for x in unit.stdout.splitlines() if "=" in x)
        if (values.get("LoadState") not in ("loaded", "not-found")
                or values.get("ActiveState") not in ("inactive", "failed")
                or values.get("SubState") not in ("dead", "failed")
                or values.get("MainPID") != "0" or values.get("ControlPID") != "0"
                or values.get("Job") not in ("", "0")):
            return False
        found = subprocess.run(["docker", "ps", "-aq", "--no-trunc", "--filter",
                                f"label={OWNER_LABEL}={req['owner']}"],
                               capture_output=True, text=True, timeout=10, shell=False)
        return found.returncode == 0 and not found.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def reconcile_run_cleanup(task_id: int, state: Path | None = None) -> bool:
    """Separate supervisor: terminal + durable completion + exact runtime absence.
    Legacy/ambiguous/unknown receipts stay pending, preserve all evidence and capacity.
    """
    state = state or state_dir()
    h = harness(state)
    task = h.get(task_id)
    if not task.main_goal.startswith(MAIN_GOAL) or task.state not in TERMINAL_RUNS:
        return False
    if task.cleanup_state == "CLEANED_VERIFIED":
        return True
    d = run_dir(task_id, state)
    try:
        with execution_lock(d):
            task = h.get(task_id)
            if task.state not in TERMINAL_RUNS:
                return False
            if task.cleanup_state == "CLEANED_VERIFIED":
                return True
            req = bound_request(d, task)
            launch = read_receipt(d / "launch.json")
            done = read_receipt(d / "worker-finished.json")
            if (launch.get("owner") != req["owner"] or launch.get("state") != "ACCEPTED"
                    or done.get("owner") != req["owner"] or done.get("state") != task.state):
                return False
            for receipt in d.glob("container-*.json"):
                step = read_receipt(receipt)
                if step.get("owner") != req["owner"] or step.get("state") != "REMOVED":
                    return False
            if not runtime_absent(req):
                return False
            # Keep request, source manifest, IR/seed, logs, and every receipt.
            # Delete only derived bulky trees of this exact stopped run.
            for name in ("src", "pin", "archify"):
                child = d / name
                if child.is_symlink() or (child.exists() and not child.is_dir()):
                    return False
                if child.exists():
                    for directory, _, _ in os.walk(child, followlinks=False):
                        os.chmod(directory, 0o700, follow_symlinks=False)
                    shutil.rmtree(child)
            if any((d / name).exists() or (d / name).is_symlink() for name in ("src", "pin", "archify")):
                return False
            write_receipt(d / "cleanup.json", {"owner": req["owner"], "state": "VERIFIED"})
            h.cleanup(task_id, at=datetime.now(timezone.utc), executor_stopped=True, no_active_refs=True,
                      dirty_worktree=False, evidence_preserved=True, deletion_verified=True)
            return True
    except (OSError, ValueError, HarnessError):
        return False


def prune_run_dirs(state: Path, keep: int) -> None:
    """Compatibility entry point: age/count must never erase durable evidence.

    Reconciliation releases generated payloads, not run directories or receipts.
    Evidence archival/retention requires a separate reviewed export policy.
    """
    return None
