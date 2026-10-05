# From the Windows PC: push, then deploy on the Mac over SSH (key, no password).
#   .\deploy\deploy.ps1                                   # the Mac at tatiana_sorokina@192.168.68.57
#   .\deploy\deploy.ps1 -MacHost tatiana_sorokina@MacBook-Pro-5.local
#   .\deploy\deploy.ps1 -MacHost tatiana_sorokina@192.168.68.60 -HostKeyAlias 192.168.68.57
# The Mac travels and DHCP moves addresses (WO28): find it first (MacBook-Pro-5.local, then 192.168.68.57, else ask the
# owner), and reach a new address with -HostKeyAlias 192.168.68.57 so its known host key still has to match.
# On the Mac (deploy/mac_deploy.sh, after `git pull`): private/ pull, deploy/mac_setup.sh (venv, folders, tests,
# launchd files), the WORKER restarted as the launchd service, then services status, `thrift status` and the last 30
# lines of the worker log. A failed setup or test run rolls the Mac back to the commit before the pull. The poster
# service is never started or stopped here (a work order that changes it says so; then it is stopped while idle
# before this and started after). Exits non-zero on any failure.
# private/ goes over the same SSH connection as a git bundle of the PC's private repo (the Mac's HTTPS credential for
# the private GitHub repo lives in its login keychain, which an SSH session can't open); the Mac fast-forwards to it.
# PowerShell 5.1: no && / || at the PowerShell level, and no double quotes inside the remote command (5.1 does not
# escape them for native commands); the single-quoted string reaches the Mac's bash as it is.
param([string]$MacHost = "tatiana_sorokina@192.168.68.57", [switch]$NoPush, [string]$HostKeyAlias = "")
$ErrorActionPreference = "Stop"
$alias = @()
if ($HostKeyAlias) { $alias = @("-o", "HostKeyAlias=$HostKeyAlias") }

if (-not $NoPush) {
    git push
    if ($LASTEXITCODE -ne 0) { Write-Error "git push failed - nothing deployed"; exit 1 }
}
if (Test-Path private/.git) {
    $bundle = Join-Path $env:TEMP "thrift-private.bundle"
    git -C private bundle create $bundle main
    if ($LASTEXITCODE -ne 0) { Write-Error "could not bundle private/ - nothing deployed"; exit 1 }
    scp -q -o BatchMode=yes @alias $bundle "${MacHost}:thrift-agent/.private.bundle"
    if ($LASTEXITCODE -ne 0) { Write-Error "could not copy private/ to $MacHost - nothing deployed"; exit 1 }
}
ssh -o BatchMode=yes -o ConnectTimeout=15 @alias $MacHost 'cd ~/thrift-agent && before=$(git rev-parse HEAD) && git pull --ff-only && bash deploy/mac_deploy.sh $before'
if ($LASTEXITCODE -ne 0) { Write-Error "deploy failed on $MacHost (exit $LASTEXITCODE, see the output above)"; exit 1 }
Write-Output "deployed to $MacHost"
