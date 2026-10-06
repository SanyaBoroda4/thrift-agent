"""WO32: the extension's content scripts in jsdom (tests/js/content.test.mjs) — run by Node's test runner. Skipped where
there is no Node.js or no `npm ci --prefix tests/js` (the Mac has neither; CI and the PC run them)."""
import shutil
import subprocess

import pytest

from thrift_agent.config import ROOT

JS = ROOT / "tests" / "js"


def test_the_content_scripts_in_jsdom():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no Node.js here (the Mac has none): CI and the dev PC run the content-script tests")
    if not (JS / "node_modules" / "jsdom").is_dir():
        pytest.skip("jsdom isn't installed: npm ci --prefix tests/js")
    r = subprocess.run([node, "--test", str(JS / "content.test.mjs")], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", timeout=600)
    assert r.returncode == 0, (r.stdout[-6000:] + r.stderr[-2000:])
