# Windows equivalent; uses the same versioned engine and JSON report schema.
# No execution-policy bypass, elevation, service or authentication changes.
param(
    [string]$Action = 'audit',
    [Parameter(Mandatory=$true)][string]$Workspace,
    [string]$State = "$env:LOCALAPPDATA\ProjectFlow\fleet-v1",
    [ValidateSet('codex','claude')][string]$Agent = 'codex',
    [switch]$TrustOwnedWorkspace,
    [string]$PromptFile
)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'ROOT_AGENT_DENIED: use a non-elevated terminal' }
$owner = (Get-Acl -LiteralPath $Workspace).Owner
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($owner -ne $user) { throw 'ACL_DENIED: workspace owner differs' }
if ($Action -eq 'apply') {
    New-Item -ItemType Directory -Force -Path $State | Out-Null
    $acl = Get-Acl -LiteralPath $State
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleAll($rule) }
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($user,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $State -AclObject $acl
}
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Split-Path $PSScriptRoot -Parent)
    $arguments = @('-m','app.services.fleet_permissions',$Action,'--workspace',$Workspace,'--state',$State,'--agent',$Agent)
    if ($TrustOwnedWorkspace) { $arguments += '--trust-owned-workspace' }
    if ($PromptFile) { $arguments += @('--prompt-file',$PromptFile) }
    & python @arguments
    $result = $LASTEXITCODE
} finally { $env:PYTHONPATH = $oldPythonPath }
exit $result
