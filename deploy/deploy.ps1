# From the Windows PC: push, then pull + test + restart on the Mac.
#   .\deploy\deploy.ps1 -MacHost thrift-mac.local
# The remote command is one bash && chain, so a failing step stops everything after it. A red test suite rolls
# the checkout back to the pre-pull commit (ORIG_HEAD) so the next launchd restart never runs untested code.
# PowerShell 5.1: no && / || at the PowerShell level; they only exist inside the ssh'd bash string.
param([string]$MacHost = "thrift-mac.local", [switch]$NoRestart)
$ErrorActionPreference = "Stop"

git push
if ($LASTEXITCODE -ne 0) { throw "git push failed - nothing deployed" }

# Restart through deploy/services.sh: kickstart on a label that was never bootstrapped fails, and the script says so.
$restart = if ($NoRestart) { "" } else { 'bash deploy/services.sh restart &&' }
# private/ is its own repo on the Mac; pull it too when it is there (brand tiers, style examples, notes).
$pullPrivate = '( [ ! -d private/.git ] || git -C private pull --ff-only ) &&'
# Braces keep the rollback scoped to the test step: bash gives && and || equal precedence, so an unbraced
# `... && pytest || rollback` would also roll back after a failed pull. `exit 1` ends the remote shell.
# No double quotes inside the remote command: PowerShell 5.1 does not escape them for native commands.
$runTests = '{ .venv/bin/python -m pytest -q || { echo tests failed, rolling back to the previous commit; git reset --hard ORIG_HEAD && .venv/bin/pip install -q -e . ; exit 1; }; }'
ssh $MacHost "cd ~/thrift-agent && git pull --ff-only && $pullPrivate .venv/bin/pip install -q -e . && $runTests && $restart echo deployed"
if ($LASTEXITCODE -ne 0) { throw "deploy failed on $MacHost (see output above)" }
