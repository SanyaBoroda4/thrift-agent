from thrift_agent.config import Settings, _merge, settings


def test_merge_is_deep_and_override_wins():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    assert _merge(base, {"a": {"y": 3}, "c": 4}) == {"a": {"x": 1, "y": 3}, "b": 1, "c": 4}
    assert base["a"]["y"] == 2                       # input untouched


def test_merge_none_override_of_mapping_is_ignored():
    # settings.local.yaml with `paths:` uncommented but every child still commented out parses as {"paths": None}
    base = {"paths": {"inbox": "./in", "db": "./var/db"}, "x": 1}
    merged = _merge(base, {"paths": None, "x": None})
    assert merged["paths"] == base["paths"]
    assert merged["x"] is None                        # scalars can still be nulled on purpose
    assert Settings(merged).path("inbox").name == "in"   # no TypeError


def test_get_dotted():
    s = Settings({"marketplaces": {"poshmark": {"username": "u"}}})
    assert s.get("marketplaces.poshmark.username") == "u"
    assert s.get("marketplaces.depop.username", "none") == "none"


def test_flag_set_matches_iphone_and_icloud_spellings(tmp_path):
    ctl = tmp_path / "Posh"
    s = Settings({"paths": {"control": str(ctl)}})
    assert s.flag_set("PAUSE") is False               # control dir not created yet: not paused, not an error
    ctl.mkdir()
    assert s.flag_set("PAUSE") is False
    for name in ("PAUSE", "PAUSE.txt", ".PAUSE.txt.icloud"):   # ours, iOS Files/Shortcuts, not-yet-downloaded
        f = ctl / name
        f.touch()
        assert s.flag_set("PAUSE") is True, name
        f.unlink()
    (ctl / "PAUSED").touch()
    (ctl / "HOLD_UNSHIPPED.txt").touch()
    assert s.flag_set("PAUSE") is False
    assert s.flag_set("HOLD_UNSHIPPED") is True
    assert s.flag("PAUSE") == ctl / "PAUSE"            # writing still targets the plain name


def test_ensure_dirs_leaves_private_alone(tmp_path):
    base = settings().data
    paths = {k: str(tmp_path / k) for k in base["paths"]}
    paths["harvest"] = str(tmp_path / "private" / "harvest")
    s = Settings({**base, "paths": paths})
    s.ensure_dirs()
    assert s.path("inbox").is_dir() and s.path("db").parent.is_dir()
    assert not (tmp_path / "private").exists()       # `git clone ... private` must still find an empty spot
