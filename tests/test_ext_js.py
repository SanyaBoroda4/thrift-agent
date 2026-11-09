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


def test_the_background_worker_in_node():
    """WO54 §4: ext/background.js with a stand-in chrome.* (tests/js/background.test.mjs) — it connects by itself in a
    Chrome started with no window, opens its windows unfocused, keeps a job's tab active in its own window, and its
    tidy closes every page no running job holds. Node's own vm: no jsdom needed."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("no Node.js here (the Mac has none): CI and the dev PC run the background-worker tests")
    r = subprocess.run([node, "--test", str(JS / "background.test.mjs")], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    assert r.returncode == 0, (r.stdout[-6000:] + r.stderr[-2000:])
