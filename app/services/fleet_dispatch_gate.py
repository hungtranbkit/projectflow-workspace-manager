"""Bounded authorization handoff from an already granted MCP session.

This is not a runner or policy store. A unique private, short-lived receipt
allows the existing FleetProfile runner to start only after MCP's real grants
and binding have succeeded. Missing/uncertain handoff never dispatches work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time


class DispatchGateError(RuntimeError):
    pass


def _private_owned(path: Path, *, directory=False):
    if path.is_symlink():
        raise DispatchGateError('ACL_DENIED')
    stat = path.stat()
    if not (path.is_dir() if directory else path.is_file()):
        raise DispatchGateError('ACL_DENIED')
    if hasattr(os, 'getuid'):
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise DispatchGateError('ACL_DENIED')
        return
    # No Windows ownership guesses from username strings; current SID and
    # private ACL required. Pass literal path in environment, never interpolation.
    env = {**os.environ, 'PF_DISPATCH_GATE_PATH': str(path)}
    script = r"""$ErrorActionPreference='Stop';
$id=[Security.Principal.WindowsIdentity]::GetCurrent();
$acl=Get-Acl -LiteralPath $env:PF_DISPATCH_GATE_PATH;
if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $id.User.Value){exit 1};
foreach($rule in $acl.Access){
 if($rule.AccessControlType -eq 'Allow'){
  $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;
  if($sid -notin @($id.User.Value,'S-1-5-18','S-1-5-32-544')){exit 1}
 }
}; exit 0"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatchGateError('ACL_DENIED') from exc
    if result.returncode:
        raise DispatchGateError('ACL_DENIED')


def wait_for_dispatch_gate(path, request_key, workspace, timeout=10):
    if not request_key or not 0 < timeout <= 30:
        raise DispatchGateError('TRUST_REQUIRED')
    raw = Path(path).expanduser()
    if not raw.is_absolute() or raw.parent.is_symlink():
        raise DispatchGateError('ACL_DENIED')
    try:
        _private_owned(raw.parent, directory=True)
    except OSError as exc:
        raise DispatchGateError('ACL_DENIED') from exc
    deadline = time.monotonic() + timeout
    while True:
        if raw.exists() or raw.is_symlink():
            try:
                _private_owned(raw)
                if raw.stat().st_size > 8192:
                    raise DispatchGateError('TRUST_REQUIRED')
                receipt = json.loads(raw.read_text())
                now = time.time()
                if (receipt.get('approved') is not True or receipt.get('request_key') != request_key
                    or receipt.get('workspace') != str(Path(workspace).resolve())
                    or not isinstance(receipt.get('created_at'), (int, float))
                    or not isinstance(receipt.get('expires_at'), (int, float))
                    or not now - 30 <= receipt['created_at'] <= now + 1
                    or not now < receipt['expires_at'] <= receipt['created_at'] + 30):
                    raise DispatchGateError('TRUST_REQUIRED')
                return
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise DispatchGateError('TRUST_REQUIRED') from exc
        if time.monotonic() >= deadline:
            raise DispatchGateError('GRANT_HANDOFF_TIMEOUT')
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
