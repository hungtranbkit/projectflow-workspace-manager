"""Task-bound adoption of FleetProfile by the existing Builder launch path.

No alternative policy database: enrollment extends the existing private trust
receipt. A request may select capabilities, never a cwd/executable/command.
"""
from __future__ import annotations
import json
import os
import uuid
import re
import sys
from pathlib import Path

from app.services.fleet_permissions import FleetProfile, FleetError, PROFILE, digest, private_write


class FleetDispatch:
    def __init__(self, settings):
        self.settings = settings
        self.state = settings.state_dir / 'fleet-v1'

    def profile(self, workspace):
        profile = FleetProfile(workspace['worktree_path'], self.state)
        if profile.path.exists():
            profile.repo = json.loads(profile.path.read_text())['repo']
        return profile

    def enrollment(self, workspace):
        profile = self.profile(workspace)
        if not profile.path.exists():
            return None
        profile.check()
        task = json.loads(profile.path.read_text()).get('task')
        if not task or task.get('workspace_id') != workspace['id'] or task.get('task_id') != workspace.get('task_id'):
            raise FleetError('TRUST_REQUIRED: task binding changed')
        return task

    def enroll(self, workspace, body):
        if body.get('trust_owned_workspace') is not True or not workspace.get('task_id'):
            raise FleetError('TRUST_REQUIRED: explicit task owner enrollment required')
        if set(body) - {'trust_owned_workspace','repo','scope','runtime','image','request_key'}:
            raise FleetError('TRUST_REQUIRED: unsupported enrollment fields')
        scope, runtime = body.get('scope','local'), body.get('runtime','native')
        if scope not in ('local','delivery') or runtime not in ('native','docker'):
            raise FleetError('TRUST_REQUIRED: unsupported task requirements')
        request_key=body.get('request_key',str(workspace['task_id']))
        if not isinstance(request_key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_key):
            raise FleetError('TRUST_REQUIRED: invalid task request key')
        image = body.get('image')
        if runtime == 'docker' and not re.fullmatch(r'sha256:[0-9a-f]{64}',image or ''):
            raise FleetError('TRUST_REQUIRED: existing image digest required')
        profile = FleetProfile(workspace['worktree_path'],self.state,body.get('repo','hungtranbkit/projectflow-workspace-manager'))
        task = {'workspace_id':workspace['id'],'task_id':workspace['task_id'],
                'scope':scope,'runtime':runtime,'image':image,'readiness_version':2,'request_key':request_key}
        return profile.apply(True,task=task)

    def prepare(self, workspace, prompt):
        task = self.enrollment(workspace)
        if task is None:
            return None
        profile = self.profile(workspace)
        gate = profile.preflight(workspace['agent'],task['runtime'],task['image'],task['scope'])
        if gate['code'] != 'PASS':
            raise FleetError('BLOCKED: '+json.dumps(gate['failures'],sort_keys=True))
        if task['runtime'] == 'docker':
            prompt += '\n\nRUNTIME: The owned worktree above is mounted at /workspace in this isolated runtime. Use /workspace as the working directory. Host absolute paths are context only. Git delivery occurs separately on the host; preserve the requested file/build/test scope.\n'
        # Commits and regenerated prompts must never silently authorize a rerun.
        # A new attempt requires the owner's explicit new enrollment key.
        key = digest(json.dumps([workspace['id'],workspace['task_id'],task.get('request_key',str(workspace['task_id']))],sort_keys=True).encode())
        prompt_path = self.state / (key+'.prompt.txt')
        temporary=prompt_path.with_name(prompt_path.name+'.'+uuid.uuid4().hex)
        private_write(temporary,prompt)
        try:
            try:
                os.link(temporary,prompt_path)
            except FileExistsError:
                if prompt_path.is_symlink() or prompt_path.read_text()!=prompt:
                    raise FleetError('TRUST_REQUIRED: request prompt changed; owner must enroll a new request key')
        finally:
            temporary.unlink(missing_ok=True)
        argv = [sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/fleet-permissions.py'),
                'launch','--workspace',str(profile.workspace),'--state',str(self.state),'--repo',profile.repo,
                '--agent',workspace['agent'],'--prompt-file',str(prompt_path),'--scope',task['scope'],
                '--runtime',task['runtime'],'--request-key',key,'--timeout','600']
        if task['image']:
            argv += ['--image',task['image']]
        return {'argv':argv,'request_key':key,'profile':PROFILE,'prompt':prompt,'receipt':str(profile.path.with_name(profile.path.stem+'.request-'+key+'.json'))}
