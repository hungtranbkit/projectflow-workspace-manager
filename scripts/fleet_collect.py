"""Collect operator-visible metadata on a Linux fleet node. No application contents."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from app.services.fleet_steward import NODES, SOURCES, validate
from app.services.git_workspace import GitWorkspaceService, GitCommandError

SAFE = re.compile(r"[A-Za-z0-9_.@:+/-]{1,420}")
APP = "com.mesflow.zalogrouphub"


def run(args, timeout=8):
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env={**os.environ, "SYSTEMD_PAGER": "cat", "LC_ALL": "C"},
        )
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def github_project(remote):
    match = re.fullmatch(
        r"(?:https://(?:[^/@]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        remote,
    )
    return match[1] if match else ""


def collect(node, roots):
    at = int(time.time())
    data = {}
    coverage = dict.fromkeys(SOURCES, "not-configured")
    checkouts = {}

    def add(src, name, state, project="", version="", location=""):
        if not name or not SAFE.fullmatch(name):
            coverage[src] = "partial"
            return
        data[src, name] = dict(
            source=src,
            name=name,
            project=project,
            state=state,
            version=version,
            location=location,
        )

    coverage["git"] = "ok"
    for root in roots:
        root = Path(root).resolve()
        if not root.is_dir():
            coverage["git"] = "partial"
            continue
        git = GitWorkspaceService(root, timeout=3)

        def lost(_error):
            coverage["git"] = "partial"

        for base, dirs, files in os.walk(root, onerror=lost):
            path = Path(base)
            if ".git" in dirs or ".git" in files:
                dirs.clear()
                if not SAFE.fullmatch(str(path)):
                    coverage["git"] = "partial"
                    continue
                try:
                    project = github_project(
                        git.git(
                            path, "config", "--get", "remote.origin.url", check=False
                        ).stdout.strip()
                    )
                    rev = git.git(
                        path, "rev-parse", "--verify", "HEAD", check=False
                    ).stdout.strip()
                except (GitCommandError, OSError, UnicodeError):
                    coverage["git"] = "partial"
                    continue
                checkouts[str(path)] = project
                add(
                    "git",
                    str(path),
                    "checkout",
                    project,
                    rev if re.fullmatch("[a-f0-9]{40,64}", rev) else "",
                    str(path),
                )
                continue
            dirs[:] = [
                d
                for d in sorted(dirs)
                if d
                not in {
                    "node_modules",
                    "vendor",
                    "build",
                    "dist",
                    "target",
                    "data",
                    "cache",
                    "backups",
                    ".venv",
                }
                and (not d.startswith(".") or d == ".worktrees")
                and len(path.relative_to(root).parts) < 3
            ]

    def project_at(path):
        if path:
            for p in (Path(path), *Path(path).parents):
                if str(p) in checkouts:
                    return checkouts[str(p)]
        return ""

    def project_for(name, path):
        result = project_at(path)
        if result:
            return result
        if name.startswith(("zgh-", "zalo-group-hub", "cloudflared-zgh-")):
            return "hungtranbkit/zalo-group-hub"
        if name == "workspace-manager.service" or name.startswith("projectflow-"):
            return "hungtranbkit/projectflow-workspace-manager"
        return ""

    for source, scope in [("systemd-user", ["--user"]), ("systemd-system", [])]:
        args = ["systemctl", *scope, "--no-pager"]
        rc, out = run(
            [
                *args,
                "list-units",
                "--type=service",
                "--state=running",
                "--no-legend",
                "--plain",
            ]
        )
        coverage[source] = "unavailable" if rc else "ok"
        units = [
            line.split()[0]
            for line in out.splitlines()
            if line.split() and SAFE.fullmatch(line.split()[0])
        ]
        for offset in range(0, len(units), 80):
            rc, details = run(
                [
                    *args,
                    "show",
                    "-p",
                    "Id",
                    "-p",
                    "WorkingDirectory",
                    "-p",
                    "ActiveState",
                    *units[offset : offset + 80],
                ]
            )
            if rc:
                coverage[source] = "partial"
            for block in details.split("\n\n"):
                fields = dict(
                    line.split("=", 1) for line in block.splitlines() if "=" in line
                )
                name = fields.get("Id", "")
                cwd = fields.get("WorkingDirectory", "")
                if not SAFE.fullmatch(cwd):
                    cwd = ""
                if name:
                    add(
                        source,
                        name,
                        fields.get("ActiveState", "unknown"),
                        project_for(name, cwd),
                        location=cwd,
                    )

    fmt = '{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.ID}}'
    rc, out = run(["docker", "ps", "--no-trunc", "--format", fmt])
    coverage["docker"] = "unavailable" if rc else "ok"
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) == 4:
            name, state, wd, cid = cols
            rc, image = run(["docker", "inspect", "--format", "{{.Image}}", cid])
            if rc:
                coverage["docker"] = "partial"
            add(
                "docker",
                name,
                state,
                project_for(name, wd),
                image if re.fullmatch(r"sha256:[a-f0-9]{64}", image) else "",
                wd if SAFE.fullmatch(wd) else "",
            )

    if node == "m910":
        targets = []
        rc, devices = run(["adb", "devices"])
        coverage["android"] = "unavailable" if rc else "ok"
        if not rc:
            for line in devices.splitlines()[1:]:
                fields = line.split()
                if len(fields) != 2 or fields[1] != "device":
                    coverage["android"] = "partial"
                    continue
                adb = ["adb", "-s", fields[0], "shell"]
                rc, model = run([*adb, "getprop", "ro.product.model"])
                if rc or not re.fullmatch(r"SM-G93[05][A-Z0-9]{1,4}", model):
                    continue
                targets.append((adb, model))
        models = [m for _, m in targets]
        coverage["phone-storage"] = "partial"
        coverage["control-path"] = "partial"
        for adb, model in targets:
            if models.count(model) != 1:
                coverage["android"] = "partial"
                continue
            rc, packages = run([*adb, "pm", "list", "packages", APP])
            if rc:
                coverage["android"] = "partial"
            for line in packages.splitlines():
                pkg = line.removeprefix("package:").strip()
                if not re.fullmatch(
                    re.escape(APP) + r"(?:\.[a-z0-9]+)?", pkg
                ) or pkg.endswith(".test"):
                    continue
                rc, details = run([*adb, "dumpsys", "package", pkg])
                version = re.search(
                    r"(?m)^\s*versionName=([a-zA-Z0-9_.+-]{1,150})\s*$", details
                )
                rc, pid = run([*adb, "pidof", pkg])
                state = (
                    "process-present"
                    if re.fullmatch(r"\d+(?: \d+)*", pid)
                    else "installed"
                )
                add(
                    "android",
                    model + "/" + pkg,
                    state,
                    "hungtranbkit/zalo-group-hub",
                    version[1] if version else "",
                    model,
                )
                rc, _ = run([*adb, "run-as", pkg, "pwd"])
                if rc:
                    continue
                for flag, path in [
                    ("-d", "files/customer-reply-photos"),
                    ("-f", "databases/customer-replies.db"),
                ]:
                    rc, _ = run([*adb, "run-as", pkg, "test", flag, path])
                    state = {0: "present", 1: "not-seen-at-path"}.get(rc, "unknown")
                    add(
                        "phone-storage",
                        model + "/" + pkg + "/" + path,
                        state,
                        "hungtranbkit/zalo-group-hub",
                        location=pkg + "/" + path,
                    )
            rc, network = run([*adb, "ip", "-4", "addr", "show", "wlan0"])
            addr = re.search(r"\binet ([0-9.]+)/", network)
            if (
                not rc
                and addr
                and ipaddress.IPv4Address(addr[1]).is_private
                and not ipaddress.IPv4Address(addr[1]).is_loopback
            ):
                conn = http.client.HTTPConnection(addr[1], 8788, timeout=3)
                try:
                    conn.request("GET", "/")
                    status = conn.getresponse().status
                    add(
                        "control-path",
                        model + "/http-8788",
                        "HTTP-" + str(status),
                        "hungtranbkit/zalo-group-hub",
                        location="M910-to-" + model,
                    )
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    conn.close()
            rc, listeners = run([*adb, "ss", "-ltn"])
            if not rc and re.search(r":8788\b", listeners):
                add(
                    "control-path",
                    model + "/lan-8788",
                    "listening",
                    "hungtranbkit/zalo-group-hub",
                    location=model,
                )
        unit = data.get(("systemd-user", "zgh-iphone-controller.service"))
        if unit:
            add(
                "control-path",
                "iphone-controller/M910",
                unit["state"],
                "hungtranbkit/zalo-group-hub",
                location="m910",
            )
    report = dict(node=node, at=at, sources=coverage, items=list(data.values()))
    validate(report)
    return report


def publish(report, spool):
    spool.mkdir(parents=True, mode=0o700, exist_ok=True)
    text = (
        json.dumps(
            {k: v for k, v in report.items() if k != "items"}, ensure_ascii=False
        )
        + "\n"
    )
    text += "".join(
        json.dumps(i, sort_keys=True, ensure_ascii=False) + "\n"
        for i in sorted(report["items"], key=lambda row: (row["source"], row["name"]))
    )
    name = hashlib.sha256(text.encode()).hexdigest() + ".jsonl"

    def atomic(path, content):
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf8", dir=spool, delete=False
        ) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(f.name, path)

    atomic(spool / name, text)
    atomic(spool / "latest.json", json.dumps(dict(file=name, lines=text.count("\n"))))
    return name


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", choices=NODES, required=True)
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--spool", type=Path, required=True)
    args = parser.parse_args()
    r = collect(args.node, args.root)
    print(r["node"], len(r["items"]), publish(r, args.spool))
