"""WO45 §10: the golden shares — real shares of the owner's (private/tests/golden/<case>/shares/…, never in the public
repo) replayed through the whole intake with the model's recorded answers (answers.json), each compared with its
expected.yaml. They run where that folder is (the Mac's pre-deploy test run, the developer's PC with private/) and are
skipped in GitHub CI, which has no private repo. `thrift replay --golden --record` records a case's answers once;
a prompt change needs a new recording (the answers are keyed by the prompt)."""
import pytest

from thrift_agent import replay
from thrift_agent.config import settings

CASES = [c for c in replay.cases() if (c / replay.ANSWERS).exists()]


@pytest.mark.skipif(not CASES, reason="no recorded golden shares under private/tests/golden")
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_golden_share(case, tmp_path):
    out = replay.run(settings(), case, tmp_path / "replay", record=False)
    assert out["answers"]["misses"] == 0, "a replay never asks the model"
    assert out["mismatches"] == [], replay.summary(out)
