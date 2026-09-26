import pytest
import yaml

from thrift_agent import config
from thrift_agent.config import CONFIG_DIR, Settings, _merge, settings


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


# --- shipped settings files ---------------------------------------------------------------------------------


def _shipped(name: str) -> dict:
    # The raw file, not settings(): the dev machine's settings.local.yaml would otherwise leak into the assertions.
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def test_example_local_settings_keep_archive_inside_icloud():
    s = Settings(_shipped("settings.local.example.yaml"))
    assert s.is_prod
    assert s.path("archive").parent == s.path("inbox").parent    # Posh/archive next to Posh/inbox: never leaves iCloud
    assert s.path("archive").name == "archive"


def test_shipped_defaults_have_duplicate_and_depop_settings():
    s = Settings(_shipped("settings.yaml"))
    assert isinstance(s.get("duplicates.lookback_days"), int) and s.get("duplicates.lookback_days") > 0
    assert isinstance(s.get("duplicates.max_distance"), int) and 0 < s.get("duplicates.max_distance") <= 64
    for mp in ("poshmark", "depop"):
        assert s.get(f"marketplaces.{mp}.username") == ""       # present but empty: posters() fails fast if enabled


# --- load_yaml ---------------------------------------------------------------------------------------------


@pytest.fixture
def yaml_dirs(tmp_path, monkeypatch):
    private, cfg = tmp_path / "private", tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "PRIVATE_DIR", private)
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    return private, cfg


def test_load_yaml_prefers_private_then_config_then_example(yaml_dirs):
    private, cfg = yaml_dirs
    (cfg / "brand_tiers.example.yaml").write_text("brands: {Example: {target: 1}}\n", encoding="utf-8")
    assert config.load_yaml("brand_tiers.yaml")["brands"] == {"example": {"target": 1}}
    (cfg / "brand_tiers.yaml").write_text("brands: {Config: {target: 2}}\n", encoding="utf-8")
    assert list(config.load_yaml("brand_tiers.yaml")["brands"]) == ["config"]
    private.mkdir()
    (private / "brand_tiers.yaml").write_text("brands: {Private: {target: 3}}\n", encoding="utf-8")
    assert list(config.load_yaml("brand_tiers.yaml")["brands"]) == ["private"]
    with pytest.raises(FileNotFoundError):
        config.load_yaml("nope.yaml")


def test_load_yaml_lowercases_brand_and_alias_keys(yaml_dirs, capsys):
    _, cfg = yaml_dirs
    (cfg / "brand_tiers.yaml").write_text(
        "brands:\n  Tory Burch: {target: 70}\n  UGG: {target: 65}\n"
        "aliases:\n  Ugg Australia: UGG\n"
        "category_defaults:\n  Shoes: 45\n", encoding="utf-8")
    tiers = config.load_yaml("brand_tiers.yaml")
    assert tiers["brands"] == {"tory burch": {"target": 70}, "ugg": {"target": 65}}
    assert tiers["aliases"] == {"ugg australia": "ugg"}
    assert tiers["category_defaults"] == {"Shoes": 45}          # categories keep the model's spelling
    assert capsys.readouterr().err == ""


def test_load_yaml_warns_when_a_brand_key_is_a_yaml_boolean(yaml_dirs, capsys):
    # `on:` unquoted is YAML 1.1 for True — exactly what a seller listing the running-shoe brand On would type.
    _, cfg = yaml_dirs
    (cfg / "brand_tiers.yaml").write_text(
        "brands:\n  on: {target: 60}\n  Nike: {target: 40}\naliases:\n  on running: on\n", encoding="utf-8")
    tiers = config.load_yaml("brand_tiers.yaml")
    assert tiers["brands"] == {"true": {"target": 60}, "nike": {"target": 40}}    # coerced to str, not lost
    assert tiers["aliases"] == {"on running": "true"}
    err = capsys.readouterr().err
    assert "brand_tiers.yaml: a key under brands was parsed as YAML boolean True" in err
    assert 'quote it, e.g. "on": {...}' in err
    assert "an alias value under aliases was parsed as YAML boolean True" in err


def test_load_yaml_handles_empty_file_and_empty_sections(yaml_dirs):
    _, cfg = yaml_dirs
    (cfg / "brand_tiers.yaml").write_text("", encoding="utf-8")
    assert config.load_yaml("brand_tiers.yaml") is None
    (cfg / "brand_tiers.yaml").write_text("brands:\naliases:\n", encoding="utf-8")
    assert config.load_yaml("brand_tiers.yaml") == {"brands": None, "aliases": None}
