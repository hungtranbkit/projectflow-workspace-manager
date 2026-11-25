"""Pinned official GitHub CLI user install; never installs or copies auth."""
from __future__ import annotations
import io
import json
import os
import platform
import shlex
import shutil
import tarfile
import urllib.request
from pathlib import Path

from app.services.fleet_permissions import FleetError, digest, private_write

VERSION = "2.105.0"
ARCHIVE_SHA256 = "79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f"
URL = f"https://github.com/cli/cli/releases/download/v{VERSION}/gh_{VERSION}_linux_amd64.tar.gz"


def install_archive(data, expected_sha, home):
    if digest(data) != expected_sha:
        raise FleetError("CHECKSUM_MISMATCH")
    home = Path(home)
    binary = home / f".local/share/projectflow/gh/{VERSION}/gh"
    link = home / ".local/bin/gh"
    receipt = home / ".local/state/projectflow/gh-install-v1.json"
    if link.exists() or link.is_symlink():
        if (link.is_symlink() and link.resolve() == binary and receipt.exists()
                and digest(binary.read_bytes()) == json.loads(receipt.read_text())["binary_sha256"]):
            return {"code": "PASS", "changed": False, "version": VERSION}
        raise FleetError("PROFILE_CONFLICT: preserve existing gh")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember(f"gh_{VERSION}_linux_amd64/bin/gh")
        if not member.isfile() or member.size > 100*1024*1024:
            raise FleetError("INVALID_ARCHIVE")
        content = archive.extractfile(member).read()
    binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if binary.exists() or binary.is_symlink():
        if (binary.is_symlink() or not receipt.exists()
                or digest(binary.read_bytes()) != digest(content)
                or json.loads(receipt.read_text()).get("binary_sha256") != digest(content)):
            raise FleetError("PROFILE_CONFLICT: preserve existing binary")
    # Persist intent first so interrupted installation is recoverable without
    # taking ownership of an unrelated pre-existing binary.
    private_write(receipt, json.dumps({"version": VERSION, "archive_sha256": expected_sha,
                                     "binary_sha256": digest(content), "before": None}))
    if not binary.exists():
        fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            binary.unlink(missing_ok=True)
            raise
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(binary)
    return {"code": "PASS", "changed": True, "version": VERSION, "archive_sha256": expected_sha}


def install(home=Path.home()):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise FleetError("ROOT_AGENT_DENIED")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise FleetError("CLI_UNSUPPORTED: official Linux amd64 package only")
    with urllib.request.urlopen(URL, timeout=30) as response:
        data = response.read(100*1024*1024+1)
    if len(data) > 100*1024*1024:
        raise FleetError("INVALID_ARCHIVE")
    return install_archive(data, ARCHIVE_SHA256, home)


def rollback(home=Path.home()):
    home = Path(home)
    binary = home / f".local/share/projectflow/gh/{VERSION}/gh"
    link = home / ".local/bin/gh"
    receipt = home / ".local/state/projectflow/gh-install-v1.json"
    if not receipt.exists():
        return {"code": "PASS", "changed": False}
    saved = json.loads(receipt.read_text())
    if ((link.exists() or link.is_symlink()) and (not link.is_symlink() or link.resolve() != binary)
            or binary.is_symlink()
            or (binary.exists() and digest(binary.read_bytes()) != saved["binary_sha256"])):
        raise FleetError("ROLLBACK_CONFLICT")
    link.unlink(missing_ok=True)
    binary.unlink(missing_ok=True)
    receipt.unlink()
    return {"code": "PASS", "changed": True}


def _helper_values(profile):
    result=profile.git.git(profile.workspace,'config','--local','--get-all','credential.https://github.com.helper',check=False)
    if result.returncode not in (0,1):
        raise FleetError('ACL_DENIED: cannot read repository helper')
    return result.stdout.splitlines()


def _set_helper_values(profile, values):
    key='credential.https://github.com.helper'
    result=profile.git.git(profile.workspace,'config','--local','--unset-all',key,check=False)
    if result.returncode not in (0,5):
        raise FleetError('ACL_DENIED: cannot update repository helper')
    for value in values:
        profile.git.git(profile.workspace,'config','--local','--add',key,value)


def repair_helper(profile):
    """Configure only this repository, after actual authenticated identity proof.

    Never overwrites auth files or global configuration. The backup can contain
    the previous helper definition, so remains private and is never reported.
    """
    checks=profile.audit()['checks']
    for key in ('gh_identity','github_push','repository'):
        if checks[key]['code'] != 'PASS':
            raise FleetError(checks[key]['code'])
    from app.services.git_workspace import noninteractive_git_env
    executable=shutil.which('gh',path=noninteractive_git_env().get('PATH'))
    if not executable:
        raise FleetError('CLI_UNSUPPORTED')
    binary=Path(executable).resolve()
    if not binary.is_file() or binary.stat().st_mode & 0o022:
        raise FleetError('ACL_DENIED: helper binary writable by other users')
    # Git's documented shell helper needs shell quoting even though every Git
    # operation itself is argv-only. Executable is a verified absolute path.
    after=['','!'+shlex.quote(str(binary))+' auth git-credential']
    receipt=profile.path.with_suffix('.git-helper.json')
    before=_helper_values(profile)
    if receipt.exists():
        saved=json.loads(receipt.read_text())
        if saved['after'] != after or before != after:
            raise FleetError('PROFILE_CONFLICT: preserve helper edits')
        return {'code':'PASS','changed':False}
    if before==after:
        return {'code':'PASS','changed':False}
    private_write(receipt,json.dumps({'before':before,'after':after,'workspace':str(profile.workspace)}))
    try:
        _set_helper_values(profile,after)
    except Exception:
        _set_helper_values(profile,before)
        receipt.unlink(missing_ok=True)
        raise
    return {'code':'PASS','changed':True,'scope':'repository','helper':'gh auth git-credential'}


def rollback_helper(profile):
    receipt=profile.path.with_suffix('.git-helper.json')
    if not receipt.exists():
        return {'code':'PASS','changed':False}
    saved=json.loads(receipt.read_text())
    if saved['workspace'] != str(profile.workspace) or _helper_values(profile) != saved['after']:
        raise FleetError('ROLLBACK_CONFLICT')
    _set_helper_values(profile,saved['before'])
    receipt.unlink()
    return {'code':'PASS','changed':True}
