"""The Gmail Apps Script (gmail/Code.gs) under Node's own test runner, Apps Script's services mocked
(tests/js/gmail.test.mjs: node:test and node:vm only, no npm dependencies). Skipped where there is no Node.js (the Mac
has none; CI and the dev PC run it)."""
import shutil
import subprocess

import pytest

from thrift_agent.config import ROOT


def test_the_gmail_script_under_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no Node.js here (the Mac has none): CI and the dev PC run the Gmail script's tests")
    r = subprocess.run([node, "--test", "tests/js/gmail.test.mjs"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    assert r.returncode == 0, (r.stdout[-6000:] + r.stderr[-2000:])
