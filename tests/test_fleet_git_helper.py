import json
from app.services import fleet_gh
from app.services.fleet_permissions import Probe,FleetProfile
from app.services.git_workspace import GitWorkspaceService
import pytest


def test_auth_failure_never_changes_repo_config(git_repo,tmp_path,monkeypatch):
    root,repo=git_repo
    profile=FleetProfile(repo,tmp_path/'state')
    monkeypatch.setattr(profile,'audit',lambda:{'checks':{'gh_identity':{'code':'AUTH_REQUIRED'},'github_push':{'code':'PASS'},'repository':{'code':'PASS'}}})
    before=(repo/'.git/config').read_bytes()
    with pytest.raises(Exception,match='AUTH_REQUIRED'):
        fleet_gh.repair_helper(profile)
    assert (repo/'.git/config').read_bytes()==before


def test_helper_repair_repeat_and_conflict_aware_rollback(git_repo,tmp_path,monkeypatch):
    root,repo=git_repo; profile=FleetProfile(repo,tmp_path/'state')
    monkeypatch.setattr(profile,'audit',lambda:{'checks':{k:{'code':'PASS'} for k in ('gh_identity','github_push','repository')}})
    gh=tmp_path/'gh';gh.write_text('#!/bin/sh\nexit 0\n');gh.chmod(0o700)
    monkeypatch.setattr(fleet_gh.shutil,'which',lambda *a,**k:str(gh))
    key='credential.https://github.com.helper'
    profile.git.git(repo,'config','--local','--add',key,'original-helper')
    assert fleet_gh.repair_helper(profile)['changed']
    assert not fleet_gh.repair_helper(profile)['changed']
    assert fleet_gh.rollback_helper(profile)['changed']
    assert profile.git.git(repo,'config','--local','--get-all',key).stdout.splitlines()==['original-helper']
    fleet_gh.repair_helper(profile)
    profile.git.git(repo,'config','--local','--add',key,'owner-later-edit')
    with pytest.raises(Exception,match='ROLLBACK_CONFLICT'):fleet_gh.rollback_helper(profile)
