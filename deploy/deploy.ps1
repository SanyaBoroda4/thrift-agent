# From the Windows PC: push, then pull + test + restart on the Mac.
#   .\deploy\deploy.ps1 -MacHost thrift-mac.local
param([string]$MacHost = "thrift-mac.local", [switch]$NoRestart)
$ErrorActionPreference = "Stop"

git push
if ($LASTEXITCODE -ne 0) { throw "git push failed — nothing deployed" }

$restart = if ($NoRestart) { "" } else {
  'launchctl kickstart -k gui/$(id -u)/com.thriftagent.worker; launchctl kickstart -k gui/$(id -u)/com.thriftagent.poster;'
}
# private/ is its own repo on the Mac; pull it too when it is there (brand tiers, style examples, notes).
$pullPrivate = '( [ ! -d private/.git ] || git -C private pull --ff-only ) &&'
ssh $MacHost "cd ~/thrift-agent && git pull --ff-only && $pullPrivate .venv/bin/pip install -q -e . && .venv/bin/python -m pytest -q && $restart echo deployed"
if ($LASTEXITCODE -ne 0) { throw "deploy failed on $MacHost (see output above)" }
