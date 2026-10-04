# From the Windows PC: push, then deploy on the Mac over SSH (key, no password).
#   .\deploy\deploy.ps1                                   # the Mac at tatiana_sorokina@192.168.68.57
#   .\deploy\deploy.ps1 -MacHost tatiana_sorokina@MacBook-Pro-5.local
# On the Mac (deploy/mac_deploy.sh, after `git pull`): private/ pull, deploy/mac_setup.sh (venv, folders, tests,
# launchd files), the WORKER restarted as the launchd service, then services status, `thrift status` and the last 30
# lines of the worker log. A failed setup or test run rolls the Mac back to the commit before the pull. The poster
# service is never started (it stays off until the owner turns publishing on). Exits non-zero on any failure.
# PowerShell 5.1: no && / || at the PowerShell level, and no double quotes inside the remote command (5.1 does not
# escape them for native commands); the single-quoted string reaches the Mac's bash as it is.
param([string]$MacHost = "tatiana_sorokina@192.168.68.57", [switch]$NoPush)
$ErrorActionPreference = "Stop"

if (-not $NoPush) {
    git push
    if ($LASTEXITCODE -ne 0) { Write-Error "git push failed - nothing deployed"; exit 1 }
}
ssh -o BatchMode=yes -o ConnectTimeout=15 $MacHost 'cd ~/thrift-agent && before=$(git rev-parse HEAD) && git pull --ff-only && bash deploy/mac_deploy.sh $before'
if ($LASTEXITCODE -ne 0) { Write-Error "deploy failed on $MacHost (exit $LASTEXITCODE, see the output above)"; exit 1 }
Write-Output "deployed to $MacHost"
