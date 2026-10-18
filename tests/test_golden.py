"""WO45 §10: the golden shares — real shares of the owner's (private/tests/golden/<case>/shares/…, never in the public
repo) replayed through the whole intake with the model's recorded answers (answers.json), each compared with its
expected.yaml. They run where that folder is (the Mac's pre-deploy test run, the developer's PC with private/) and are
skipped in GitHub CI, which has no private repo. `thrift replay --golden --record` records a case's answers once;
a prompt change needs a new recording (the answers are keyed by the prompt)."""
import pytest

from thrift_agent import config, replay

CASES = [c for c in replay.cases() if (c / replay.ANSWERS).exists()]
# The answers were recorded by `thrift replay --golden --record` with the machine's own settings (its models, its
# private style examples in the copy prompt): the replay must read the same, not the suite's isolated defaults (live
# 2026-10-18: every case missed `write_listing (claude-sonnet-5)` under the isolation). Captured at import, before
# the autouse fixture takes them away; the secrets (.env) stay out — a replay never calls the model.
_REAL = {"OVERRIDE_FILES": config.OVERRIDE_FILES, "PRIVATE_DIR": config.PRIVATE_DIR}


@pytest.fixture
def real_settings(monkeypatch):
    for name, value in _REAL.items():
        monkeypatch.setattr(config, name, value)
    config.settings.cache_clear()
    yield config.settings()
    config.settings.cache_clear()


@pytest.mark.skipif(not CASES, reason="no recorded golden shares under private/tests/golden")
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_golden_share(case, tmp_path, real_settings):
    out = replay.run(real_settings, case, tmp_path / "replay", record=False)
    assert out["answers"]["misses"] == 0, "a replay never asks the model"
    assert out["mismatches"] == [], replay.summary(out)
