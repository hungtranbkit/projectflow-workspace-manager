#!/usr/bin/env python3
"""Reproducible isolated CLI install/layout preparation; never authenticates an agent.

No shell interpolation or global installs. Official non-npm installers are documented
rather than piped into sh. `--install` is opt-in. `--check` safely checks preinstalled
binaries without contacting a provider or sending workspace content to it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

CATALOG = {
    "claude-code": {"package": "@anthropic-ai/claude-code", "binary": "claude", "skill_dir": ".claude/skills", "docs": "https://code.claude.com/docs/en/setup"},
    "codex": {"package": "@openai/codex", "binary": "codex", "skill_dir": ".agents/skills", "docs": "https://github.com/openai/codex"},
    "opencode": {"package": "@opencode/cli", "binary": "opencode", "skill_dir": ".opencode/skills", "docs": "https://opencode.ai/v2/docs"},
    "freebuff": {"package": "freebuff", "binary": "freebuff", "skill_dir": ".agents/skills", "docs": "https://github.com/CodebuffAI/freebuff"},
    "codebuff": {"package": "codebuff", "binary": "codebuff", "skill_dir": ".agents/skills", "docs": "https://www.codebuff.com/docs/help/quick-start"},
    "gemini-cli": {"package": "@google/gemini-cli", "binary": "gemini", "skill_dir": ".gemini/skills", "docs": "https://geminicli.com/docs/get-started/installation/"},
    "hermes": {"package": None, "binary": "hermes", "skill_dir": ".hermes/skills", "docs": "https://hermes-agent.nousresearch.com/docs/getting-started/installation"},
    "cursor": {"package": None, "binary": "agent", "skill_dir": ".cursor/skills", "docs": "https://cursor.com/docs/cli/installation"},
}


def run_limited(argv, timeout=75, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
        # Never store command stderr verbatim: it can contain environment-dependent secrets.
        return {"exit_code": p.returncode, "stdout_excerpt": p.stdout.strip()[:300], "stderr_present": bool(p.stderr.strip())}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "error": "timeout"}
    except OSError as e:
        return {"exit_code": None, "error": e.__class__.__name__}


def make_project(skill_root, sandbox, ids):
    if not (skill_root / 'SKILL.md').is_file():
        raise ValueError('Missing SKILL.md in skill root')
    sandbox.mkdir(parents=True, exist_ok=True)
    installed = []
    for name in ids:
        item = CATALOG[name]
        folder = sandbox / 'project' / item['skill_dir'] / 'image-to-code'
        folder.mkdir(parents=True, exist_ok=True)
        for file in ['SKILL.md', 'LICENSE']:
            if (skill_root / file).is_file():
                shutil.copy2(skill_root / file, folder / file)
        for subdir in ['references', 'scripts', 'examples']:
            src = skill_root / subdir
            if src.exists():
                shutil.copytree(src, folder / subdir, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        installed.append({"agent": name, "skill_path": str(folder.relative_to(sandbox)), "skill_discoverable": (folder/'SKILL.md').is_file()})
    return installed


def prepare(skill_root: Path, dest: Path, agents, install=False, check=False, allow_install_scripts=False):
    results = []
    placements = make_project(skill_root, dest, agents)
    for name in agents:
        conf = CATALOG[name]
        status = {'agent': name, 'package': conf['package'], 'docs': conf['docs'], 'skill_dir': conf['skill_dir'], 'installation': 'NOT_ATTEMPTED', 'invocation': 'NOT_AUTHENTICATED', 'cli_version': None}
        exe = shutil.which(conf['binary'])
        if install:
            if not conf['package']:
                status['installation'] = 'MANUAL_OFFICIAL_INSTALL_REQUIRED'
            else:
                prefix = dest / 'runtimes' / name
                prefix.mkdir(parents=True, exist_ok=True)
                cmd = ['npm', 'install', '--prefix', str(prefix), '--no-audit', '--no-fund', *([] if allow_install_scripts else ['--ignore-scripts']), conf['package']]
                # Native binary install depends on postinstall, so retry must be an explicit
                # manual action rather than executing third-party scripts implicitly.
                result = run_limited(cmd, timeout=180)
                status['installation'] = 'INSTALLED_PACKAGE_ONLY' if result.get('exit_code') == 0 else 'INSTALL_FAILED'
                status['install_check'] = result
                if result.get('exit_code') == 0:
                    binary = prefix / 'node_modules' / '.bin' / conf['binary']
                    if binary.exists():
                        exe = str(binary)
        if check and exe:
            version = run_limited([str(exe), '--version'], timeout=20)
            status['cli_version'] = version['stdout_excerpt'] if version.get('exit_code') == 0 else None
            status['binary_executable'] = version.get('exit_code') == 0
        else:
            status['binary_executable'] = False
        if status['installation'] == 'NOT_ATTEMPTED' and exe:
            status['installation'] = 'PREINSTALLED_UNVERIFIED' if not check else ('PREINSTALLED' if status['binary_executable'] else 'PREINSTALLED_BROKEN')
        if not exe and status['installation'] == 'NOT_ATTEMPTED':
            status['installation'] = 'NOT_INSTALLED'
        results.append(status)
    report = {'schema_version':1,'kind':'cli-preflight', 'generated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'agents':results,'placements':placements,'auth_note':'No code agent interaction or image-to-code completion was executed.'}
    (dest/'cli-preflight.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--skill-root',type=Path,default=Path.cwd())
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--agents',nargs='+',choices=sorted(CATALOG),default=list(CATALOG))
    p.add_argument('--check',action='store_true',help='Run --version if a CLI is already installed')
    p.add_argument('--install',action='store_true',help='Attempt isolated npm package installs; requires network')
    p.add_argument('--allow-install-scripts',action='store_true',help='Explicitly allow third-party npm postinstall for native binaries on isolated runners')
    a=p.parse_args()
    report=prepare(a.skill_root.resolve(),a.out.resolve(),a.agents,a.install,a.check,a.allow_install_scripts)
    print(json.dumps({'agents':[{'name':x['agent'],'status':x['installation'],'version':x['cli_version']} for x in report['agents']], 'report':str(a.out/'cli-preflight.json')},indent=2))

if __name__=='__main__':main()