#!/usr/bin/env python3
"""Audit real availability of coding agent CLIs. Never report an absent CLI as tested.
Can validate installation layouts in a temporary directory without invoking agents.
"""
from pathlib import Path
import argparse,json,shutil,subprocess,os
AGENTS={
 'claude-code':{'commands':['claude'],'skill_dirs':['.claude/skills/image-to-code']},
 'codex':{'commands':['codex'],'skill_dirs':['.agents/skills/image-to-code']},
 'opencode':{'commands':['opencode'],'skill_dirs':['.opencode/skills/image-to-code']},
 'hermes':{'commands':['hermes'],'skill_dirs':['.hermes/skills/image-to-code']},
 'codebuff':{'commands':['codebuff','freebuff'],'skill_dirs':['.agents/skills/image-to-code']},
 'cursor':{'commands':['cursor-agent','cursor'],'skill_dirs':['.cursor/skills/image-to-code']},
 'gemini-cli':{'commands':['gemini'],'skill_dirs':['.gemini/skills/image-to-code']}
}

def probe(root):
    report={}
    for name,conf in AGENTS.items():
        exe=next((shutil.which(cmd) for cmd in conf['commands'] if shutil.which(cmd)),None)
        dirs=[{'path':s,'has_skill':(root/s/'SKILL.md').is_file()} for s in conf['skill_dirs']]
        report[name]={'cli_available':bool(exe),'cli_path':exe,'install_layout':dirs,
                      'runtime_invocation':'UNVERIFIED' if exe else 'NOT_TESTED_CLI_UNAVAILABLE',
                      'notes':'No provider tokens or logins supplied; do not claim end-to-end agent execution.'}
    return report

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,default=Path.cwd());p.add_argument('--out',type=Path,required=True)
 args=p.parse_args();r=probe(args.workspace);args.out.parent.mkdir(parents=True,exist_ok=True)
 args.out.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))