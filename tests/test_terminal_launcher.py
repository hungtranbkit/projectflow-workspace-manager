from pathlib import Path
from unittest.mock import Mock
import pytest
from app.config import Settings
from app.launchers import AGENT_LAUNCHERS
from app.services.git_workspace import GitWorkspaceService
from app.services.terminal_launcher import LauncherError, TerminalLauncherService

def service(tmp_path, *, which=None, environ=None, popen=None):
    settings=Settings(tmp_path,"127.0.0.1",8765,tmp_path/"db",30)
    return TerminalLauncherService(settings,GitWorkspaceService(tmp_path),which=which or (lambda x:f"/bin/{x}"),environ=environ if environ is not None else {"DISPLAY":":0"},popen=popen or Mock())

def test_agent_launcher_resolution_and_unknown(tmp_path):
    svc=service(tmp_path)
    assert svc.launcher_for("codex") == AGENT_LAUNCHERS["codex"]
    assert svc.launcher_for("claude").args == ("--dangerously-skip-permissions",)
    with pytest.raises(LauncherError,match="Chưa cấu hình"): svc.launcher_for("other")

def test_terminal_detection_order_and_desktop_requirement(tmp_path):
    found={"konsole":"/usr/bin/konsole","xterm":"/usr/bin/xterm"}
    assert service(tmp_path,which=found.get).detect_terminal().name == "konsole"
    with pytest.raises(LauncherError) as error: service(tmp_path,environ={}).detect_terminal()
    assert error.value.code == "DESKTOP_SESSION_UNAVAILABLE"

def test_cli_missing(tmp_path):
    svc=service(tmp_path,which=lambda _:None)
    with pytest.raises(LauncherError) as error: svc.launch_agent(tmp_path/".worktrees"/"missing","codex")
    assert error.value.code == "AGENT_CLI_NOT_FOUND"

def test_launch_uses_argv_without_shell_and_validated_worktree(git_repo):
    root,repo=git_repo; git=GitWorkspaceService(root); _,worktree,_=git.create_agent(repo,"codex","launcher")
    popen=Mock(); paths={"ptyxis":"/usr/bin/ptyxis","codex":"/usr/bin/codex"}
    svc=TerminalLauncherService(Settings(root,"127.0.0.1",8765,root/"db",30),git,which=paths.get,popen=popen,environ={"WAYLAND_DISPLAY":"wayland-0"})
    result=svc.launch_agent(worktree,"codex")
    assert result["terminal"]=="ptyxis"
    argv=popen.call_args.args[0]
    assert argv==["/usr/bin/ptyxis","--new-window","--working-directory",str(worktree),"--","/usr/bin/codex","--yolo"]
    assert popen.call_args.kwargs["cwd"]==worktree
    assert "shell" not in popen.call_args.kwargs
    with pytest.raises(Exception): svc.open_terminal(root/"outside")
