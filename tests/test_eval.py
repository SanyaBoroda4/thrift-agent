import json

import thrift_agent.eval as ev


def _fixtures(tmp_path, names):
    fixtures = tmp_path / "eval" / "fixtures"
    for n in names:
        (fixtures / n / "photos").mkdir(parents=True)
        (fixtures / n / "expected.yaml").write_text("items: []\n", encoding="utf-8")
    (fixtures / "no_expected").mkdir()          # a dir without expected.yaml is not a case
    return fixtures


def test_run_all_records_errors_and_writes_results(tmp_path, monkeypatch, capsys):
    fixtures = _fixtures(tmp_path, ["a", "b"])

    def fake_run_case(s, case):
        if case.name == "b":
            raise RuntimeError("boom")
        return {"case": case.name, "segmentation_ok": True, "groups": {}, "flags": [], "fields": {}}

    monkeypatch.setattr(ev, "run_case", fake_run_case)
    results = ev.run_all(None, fixtures)

    assert [r["case"] for r in results] == ["a", "b"]
    assert "error" not in results[0] and results[1] == {"case": "b", "error": "RuntimeError: boom"}
    files = list((tmp_path / "eval").glob("results_*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text(encoding="utf-8")) == results
    out = capsys.readouterr().out
    assert "'case': 'a'" in out and "RuntimeError: boom" in out


def test_summarize_skips_error_rows():
    ok = {"case": "a", "segmentation_ok": True,
          "fields": {0: {"brand": {"want": "x", "got": "x", "ok": True},
                         "size_us": {"want": "7", "got": "8", "ok": False}}}}
    err = {"case": "b", "error": "RuntimeError: boom"}
    assert ev.summarize([ok, err]) == {"segmentation": "1/1", "brand": "1/1", "size_us": "0/1", "errors": 1}
    assert ev.summarize([ok]) == {"segmentation": "1/1", "brand": "1/1", "size_us": "0/1"}
