from thrift_agent.config import Settings, _merge, settings


def test_merge_is_deep_and_override_wins():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    assert _merge(base, {"a": {"y": 3}, "c": 4}) == {"a": {"x": 1, "y": 3}, "b": 1, "c": 4}
    assert base["a"]["y"] == 2                       # input untouched


def test_get_dotted():
    s = Settings({"marketplaces": {"poshmark": {"username": "u"}}})
    assert s.get("marketplaces.poshmark.username") == "u"
    assert s.get("marketplaces.depop.username", "none") == "none"


def test_ensure_dirs_leaves_private_alone(tmp_path):
    base = settings().data
    paths = {k: str(tmp_path / k) for k in base["paths"]}
    paths["harvest"] = str(tmp_path / "private" / "harvest")
    s = Settings({**base, "paths": paths})
    s.ensure_dirs()
    assert s.path("inbox").is_dir() and s.path("db").parent.is_dir()
    assert not (tmp_path / "private").exists()       # `git clone ... private` must still find an empty spot
