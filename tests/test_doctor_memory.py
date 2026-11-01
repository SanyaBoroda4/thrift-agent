"""WO51: `thrift doctor` shows the Mac's memory in one line — free %, the kernel's pressure level, the swap (the 8 GB Mac
lives near its swap's end). Read with sysctl on the Mac only; tests pass the readings and never read the machine."""
import subprocess

from thrift_agent import doctor
from thrift_agent.config import Settings
from thrift_agent.db import DB

LIVE_1101 = {"kern.memorystatus_level": "29", "kern.memorystatus_vm_pressure_level": "2",
             "vm.swapusage": "total = 9216.00M  used = 8427.50M  free = 788.50M  (encrypted)"}   # the Mac at 14:15 EST


def test_the_memory_line_reads_the_three_sysctls():
    assert doctor.memory_line(LIVE_1101.get) == "Memory: 29% free · pressure warning · swap 8.2 of 9.0 GB used ⚠️"
    calm = {"kern.memorystatus_level": "61", "kern.memorystatus_vm_pressure_level": "1",
            "vm.swapusage": "total = 2048.00M  used = 512.25M  free = 1535.75M  (encrypted)"}
    assert doctor.memory_line(calm.get) == "Memory: 61% free · pressure normal · swap 0.5 of 2.0 GB used"
    low = {**calm, "kern.memorystatus_level": "7"}
    assert doctor.memory_line(low.get).endswith("⚠️")
    assert doctor.memory_line(lambda name: None) == "Memory: unreadable now"
    assert doctor.memory_line({"vm.swapusage": "total = 0.00M  used = 0.00M  free = 0.00M"}.get) == "Memory: unreadable now"


def _settings(tmp_path, role):
    return Settings({"machine_role": role, "paths": {"db": str(tmp_path / "s.db"), "inbox": str(tmp_path / "inbox"),
                                                     "work": str(tmp_path / "work")}})


class NoApi:
    def get(self, path):
        return {"shares": []}


def test_the_doctor_reads_memory_only_on_the_mac(tmp_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("the test machine's memory was read")
    monkeypatch.setattr(subprocess, "run", never)
    s = _settings(tmp_path, "dev")
    db = DB(s.path("db"))
    lines = doctor.report(s, db, api=NoApi())
    assert not any(line.startswith("Memory:") for line in lines)
    lines = doctor.report(s, db, api=NoApi(), memory=lambda: doctor.memory_line(LIVE_1101.get))
    assert lines[1] == "Memory: 29% free · pressure warning · swap 8.2 of 9.0 GB used ⚠️"
    lines = doctor.report(s, db, api=NoApi(), memory=lambda: 1 / 0)
    assert lines[1] == "Memory: unreadable now (ZeroDivisionError)"
    monkeypatch.setattr(doctor.sys, "platform", "darwin")              # the Mac: sysctl through memory_line
    monkeypatch.setattr(doctor, "_sysctl", LIVE_1101.get)
    lines = doctor.report(_settings(tmp_path, "prod"), db, api=NoApi())
    assert lines[1].startswith("Memory: 29% free")
