"""Pinned no-network Docker Node/JDK21 acceptance for Zalo Hub shadow V2.

QA-ONLY: no agent writes, no network, no S7/ADB, no code commits and no
mutations to the host repository. All execution inputs come from immutable
Git HEAD, not from a dirty working tree or untracked agent-generated files.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Any

IMAGE_ID = "sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f"
RUNNER = "scripts/v2-source-integration-tests.sh"
JDK21 = Path("/home/dell/.local/opt/jdk21")
ORG_JSON = Path("/home/dell/.gradle/caches/modules-2/files-2.1/org.json/json")
PROOF = "HARNESS_ZGH_NODE_JAVA_QA_PASS"
MAX_SOURCE_BYTES = 80_000_000
MAX_TRACKED_FILES = 4000


def pinned_snapshot(repo: Path, head: str, folder: Path) -> tuple[str, int]:
    """Extract only regular committed Git blobs; no symlink/traversal/dirty input."""
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.fsmonitor=false",
         "archive", "--format=tar", head],
        capture_output=True, timeout=45)
    if result.returncode or len(result.stdout) > MAX_SOURCE_BYTES + 4_000_000:
        raise RuntimeError("immutable source archive missing or exceeds size cap")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    entries: list[tuple[str, str]] = []
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for item in archive:
            name = item.name.removeprefix("./")
            parts = Path(name).parts
            if (not name or name.startswith("/") or ".." in parts
                    or any(p in {"", ".git"} for p in parts)
                    or not (item.isdir() or item.isfile())
                    or len(parts) > 20):
                raise RuntimeError("unsafe tracked entry in Git archive")
            dest = folder.joinpath(*parts)
            if item.isdir():
                dest.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            total += item.size
            count += 1
            if total > MAX_SOURCE_BYTES or count > MAX_TRACKED_FILES:
                raise RuntimeError("tracked source snapshot exceeds limits")
            dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            source = archive.extractfile(item)
            if source is None:
                raise RuntimeError("missing tracked Git blob")
            digest = hashlib.sha256()
            with dest.open("xb") as sink:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    sink.write(chunk)
            dest.chmod(0o644)
            entries.append((name, digest.hexdigest()))
    # systemd's UMask=0077 can turn mkdir(mode=0755) into 0700.
    # Explicitly make ONLY the disposable test snapshot traversable to the
    # unprivileged Docker UID; the owner-only run root stays protected.
    for item in folder.rglob("*"):
        if item.is_dir():
            item.chmod(0o755)
    folder.chmod(0o755)
    manifest_bytes = json.dumps(sorted(entries), separators=(",", ":")).encode()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    (folder.parent / "qa-source-manifest.json").write_bytes(manifest_bytes)
    return manifest_hash, count


def sandbox_qa(
    spec: dict[str, Any], snapshot: Path, run_dir: Path,
    runner: Any = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Run exactly the pre-reviewed ZGH Node + Java tests in a pinned container."""
    if spec.get("expected") != RUNNER:
        raise RuntimeError("unreviewed QA acceptance command")
    if not JDK21.joinpath("bin/java").is_file() or not ORG_JSON.is_dir():
        raise RuntimeError("trusted local JDK21/org.json test cache absent")
    image = runner(["docker", "image", "inspect", IMAGE_ID],
                   capture_output=True, timeout=10)
    if image.returncode:
        raise RuntimeError("pinned local QA image missing")
    try:
        actual_image = json.loads(image.stdout)[0]["Id"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("QA image identity unreadable") from exc
    if actual_image != IMAGE_ID:
        raise RuntimeError("QA image identity mismatch")
    opened = datetime.fromisoformat(spec["created_at"])
    if opened.tzinfo is None:
        raise RuntimeError("QA deadline missing timezone")
    remaining = int((opened + timedelta(seconds=spec["ttl_seconds"]) -
                     datetime.now(timezone.utc)).total_seconds())
    if remaining < 8:
        raise RuntimeError("deadline expired before Java/Node acceptance")
    budget = min(120, remaining - 5)
    cmd = [
        "docker", "run", "--rm", "--pull=never", "--network=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=128", "--memory=1024m", "--cpus=2",
        "--user=65534:65534",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m",
        "--tmpfs=/work:rw,nosuid,nodev,size=180m,mode=1777",
        "--mount", f"type=bind,src={snapshot.resolve()},dst=/repo,readonly",
        "--mount", f"type=bind,src={JDK21},dst=/opt/jdk21,readonly",
        "--mount", f"type=bind,src={ORG_JSON},dst=/gradle/caches/modules-2/files-2.1/org.json/json,readonly",
        "--workdir=/work",
        "--env=JAVA_HOME=/opt/jdk21", "--env=GRADLE_USER_HOME=/gradle",
        "--env=HOME=/tmp", "--env=PATH=/opt/jdk21/bin:/usr/local/bin:/usr/bin:/bin",
        IMAGE_ID, "timeout", "--signal=TERM", "--kill-after=2s",
        f"{budget}s", "bash", "-c",
        "mkdir -p /work/src && cp -a /repo/. /work/src/ && "
        "chmod -R u+w /work/src && cd /work/src && "
        "bash scripts/v2-source-integration-tests.sh",
    ]
    return runner(cmd, capture_output=True, text=True,
                  timeout=min(135, budget + 12))


def run_verified_qa(
    spec: dict[str, Any], run_dir: Path,
) -> dict[str, Any]:
    repo = Path(spec["repo"]).resolve(strict=True)
    base = spec["base_sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", base):
        raise RuntimeError("untrusted git base SHA")
    if spec.get("executor") != "zgh-node-java-qa" or spec.get("expected") != RUNNER:
        raise RuntimeError("unsupported QA mode or script")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=10)
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10)
    if (head.returncode or head.stdout.strip() != base or status.returncode
            or status.stdout.strip()):
        raise RuntimeError("source repo is dirty or moved since task launch")
    snapshot = run_dir / "qa-source"
    manifest_sha, file_count = pinned_snapshot(repo, base, snapshot)
    script = snapshot / RUNNER
    if (not script.is_file() or script.is_symlink()
            or hashlib.sha256(script.read_bytes()).hexdigest() != spec["test_sha"]):
        raise RuntimeError("pinned QA acceptance script hash mismatch")
    result = sandbox_qa(spec, snapshot, run_dir)
    log = run_dir / "test-output.txt"
    log.write_text((result.stdout + result.stderr)[-25000:], encoding="utf-8")
    after_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
    after_status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                  capture_output=True, text=True, timeout=10)
    unchanged = (after_head.returncode == 0 and after_head.stdout.strip() == base
                 and after_status.returncode == 0 and not after_status.stdout.strip())
    passed = result.returncode == 0 and unchanged
    return {
        "test_exit": result.returncode,
        "scope_verified": unchanged,
        "qa_image_id": IMAGE_ID,
        "qa_source_head": base,
        "qa_script_sha": spec["test_sha"],
        "qa_manifest_sha": manifest_sha,
        "qa_file_count": file_count,
        "qa_proof": PROOF if passed else "QA_FAILED",
        "qa_pass": passed,
    }


def independently_verify(row: Any, report: dict[str, Any], run_dir: Path) -> bool:
    """Verify immutable source evidence at acceptance, independent of worker."""
    try:
        spec = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
        manifest_path = run_dir / "qa-source-manifest.json"
        source = run_dir / "qa-source"
        script = source / RUNNER
        stdout = run_dir / "test-output.txt"
        repo = Path(row["repo"]).resolve(strict=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        return bool(
            report.get("qa_pass") is True
            and report.get("test_exit") == 0
            and report.get("scope_verified") is True
            and report.get("qa_image_id") == IMAGE_ID
            and report.get("qa_source_head") == row["base_sha"]
            and report.get("qa_script_sha") == row["test_sha"]
            and spec.get("expected") == RUNNER
            and hashlib.sha256(RUNNER.encode()).hexdigest() == row["expected_sha"]
            and script.is_file() and not script.is_symlink()
            and hashlib.sha256(script.read_bytes()).hexdigest() == row["test_sha"]
            and manifest_path.is_file() and not manifest_path.is_symlink()
            and hashlib.sha256(manifest_path.read_bytes()).hexdigest() == report.get("qa_manifest_sha")
            and stdout.is_file() and not stdout.is_symlink()
            and stdout.stat().st_size > 0
            and head.returncode == 0 and head.stdout.strip() == row["base_sha"]
            and status.returncode == 0 and not status.stdout.strip()
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return False
