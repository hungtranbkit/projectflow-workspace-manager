"""Collect locally, spool and deliver evidence signed by this node's enrolled key."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.services.fleet_steward import Memory, NODES
from scripts.fleet_collect import collect, publish


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver(node, spool, key, endpoint):
    url = urllib.parse.urlsplit(endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Use the fixed HTTPS ingest URL")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
    )
    ack = spool / "delivered"
    ack.mkdir(mode=0o700, exist_ok=True)
    pending = sorted(spool.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for file in pending:
        if (ack / file.name).exists():
            continue
        if file.stat().st_size > 2_000_000:
            raise ValueError("Report exceeds ingress limit")
        body = file.read_bytes()
        signature = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(key),
                "-in",
                str(file),
            ],
            check=True,
            capture_output=True,
        ).stdout
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-ndjson",
                "X-PF-Node": node,
                "X-PF-Signature": base64.b64encode(signature).decode(),
            },
        )
        with opener.open(req, timeout=20) as response:
            receipt = json.load(response)
        if (
            receipt.get("node") != node
            or receipt.get("sha256") != hashlib.sha256(body).hexdigest()
        ):
            raise ValueError("Missing delivery receipt")
        (ack / file.name).touch(mode=0o600)
    for file in pending:
        marker = ack / file.name
        if marker.exists() and time.time() - file.stat().st_mtime > 7 * 86400:
            file.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--node", required=True, choices=NODES)
    run.add_argument("--root", required=True, action="append")
    run.add_argument("--spool", type=Path, required=True)
    run.add_argument("--key", type=Path, required=True)
    run.add_argument("--url", required=True)
    for name in ("init", "invite"):
        sub.add_parser(name).add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "init":
        Memory(args.db).init()
    elif args.command == "invite":
        print(Memory(args.db).issue())
    else:
        args.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            sum(f.stat().st_size for f in args.spool.glob("*.jsonl"))
            < 256 * 1024 * 1024
        ):
            publish(collect(args.node, args.root), args.spool)
        deliver(args.node, args.spool, args.key, args.url)


if __name__ == "__main__":
    main()
