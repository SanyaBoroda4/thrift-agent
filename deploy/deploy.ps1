# From the Windows PC: push, then pull + restart on the Mac.
#   .\deploy\deploy.ps1 -MacHost thrift-mac.local
param([string]$MacHost = "thrift-mac.local", [switch]$NoRestart)
$ErrorActionPreference = "Stop"

git push
$restart = if ($NoRestart) { "" } else {
  'launchctl kickstart -k gui/$(id -u)/com.thriftagent.worker; launchctl kickstart -k gui/$(id -u)/com.thriftagent.poster;'
}
ssh $MacHost "cd ~/thrift-agent && git pull --ff-only && .venv/bin/pip install -q -e . && .venv/bin/python -m pytest -q && $restart echo deployed"
