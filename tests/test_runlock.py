"""WO21: one worker per machine, and the deploy scripts the Mac runs."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from thrift_agent import runlock

ROOT = Path(__file__).resolve().parents[1]


def test_a_second_holder_is_refused_with_who_holds_it(tmp_path):
    lock = tmp_path / "var" / "worker.lock"
    first = runlock.hold(lock)
    with pytest.raises(runlock.AlreadyRunning, match=rf"another worker is already running \(pid {os.getpid()}, since "):
        runlock.hold(lock)
    with pytest.raises(runlock.AlreadyRunning, match="services.sh stop worker.*Control\\+C"):
        runlock.hold(lock)
    first.close()                                          # the process ends: the OS drops the lock
    runlock.hold(lock).close()


def test_the_worker_refuses_to_start_next_to_another(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from thrift_agent import cli
    from thrift_agent.config import Settings, settings
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "var" / "state.db")
    monkeypatch.setattr(cli, "settings", lambda: Settings(data))
    monkeypatch.setattr(cli, "_worker_iteration", lambda *a: pytest.fail("a second worker must not run"))
    held = runlock.hold(tmp_path / "var" / cli.WORKER_LOCK)
    try:
        r = CliRunner().invoke(cli.app, ["run"])
    finally:
        held.close()
    out = " ".join(r.output.split())
    assert r.exit_code == 1 and "not started: another worker is already running" in out, r.output


def test_telegram_test_sends_the_given_text(monkeypatch):
    from typer.testing import CliRunner

    from thrift_agent import cli
    sent = []

    class Bot:
        chat_id = "-100"

        def send_message(self, text):
            sent.append(text)
            return 7
    monkeypatch.setattr(cli.approve, "bot_for", lambda s: Bot())
    r = CliRunner().invoke(cli.app, ["telegram", "test", "--text", "worker started over SSH"])
    assert r.exit_code == 0 and sent == ["worker started over SSH"], r.output


POSIX_BASH = os.name != "nt" and shutil.which("bash")


@pytest.mark.skipif(not POSIX_BASH, reason="the deploy scripts run on the Mac: checked on the Linux/macOS runners")
@pytest.mark.parametrize("script", ["services.sh", "mac_deploy.sh", "mac_setup.sh"])
def test_the_deploy_scripts_parse(script):
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / script)], check=True)


@pytest.mark.skipif(not POSIX_BASH, reason="the deploy scripts run on the Mac: checked on the Linux/macOS runners")
@pytest.mark.parametrize("args", [[], ["start"], ["start", "everything"], ["restart", "all"], ["logs", "nonsense"],
                                  ["poster"]])
def test_services_sh_wants_a_service_to_act_on(args):
    """Never a bare start/restart that would also bring up the poster: the two keys decide that (WO21)."""
    r = subprocess.run(["bash", str(ROOT / "deploy" / "services.sh"), *args], capture_output=True, text=True)
    assert r.returncode == 2 and "usage:" in r.stderr
