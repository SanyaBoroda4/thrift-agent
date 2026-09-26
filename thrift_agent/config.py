"""Settings = config/settings.yaml deep-merged with config/settings.local.yaml."""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PRIVATE_DIR = ROOT / "private"      # separate private repo, git-ignored here


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is None and isinstance(out.get(k), dict):
            continue            # an uncommented `paths:` whose children are still commented out is not an override
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, name: str) -> Path:
        raw = self.data["paths"][name]
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else (ROOT / p).resolve()

    @property
    def is_prod(self) -> bool:
        return self.data.get("machine_role") == "prod"

    def flag(self, name: str) -> Path:
        """Control files: PAUSE, HOLD_UNSHIPPED. This is the path *we* write; test presence with flag_set()."""
        return self.path("control") / name

    def flag_set(self, name: str) -> bool:
        """Is the control flag present, in any of the spellings it arrives in?

        Prod points `control` at the iCloud Posh folder so the seller can pause from the iPhone: iOS Files and
        Shortcuts save `PAUSE.txt`, and until the Mac has downloaded it the entry shows as `.PAUSE.txt.icloud`."""
        d = self.path("control")
        if not d.is_dir():
            return False
        return any(p.name.lstrip(".").split(".")[0] == name for p in d.iterdir())

    def ensure_dirs(self) -> None:
        # Not "harvest": it lives under private/, which must stay absent until the private repo is cloned there
        # (git clone refuses a non-empty target). harvest() creates it when it runs.
        for name in ("inbox", "work", "archive", "failed", "chrome_profile", "control"):
            self.path(name).mkdir(parents=True, exist_ok=True)
        self.path("db").parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def settings() -> Settings:
    load_dotenv(ROOT / ".env")
    data = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))
    for over in (PRIVATE_DIR / "settings.yaml", CONFIG_DIR / "settings.local.yaml"):
        if over.exists():
            data = _merge(data, yaml.safe_load(over.read_text(encoding="utf-8")) or {})
    return Settings(data)


_BRAND_SECTIONS = ("brands", "aliases")


def _warn_yaml_bool(file: str, section: str, what: str, value: bool) -> None:
    example = '{...}' if section == "brands" else "<brand>"
    print(f'{file}: {what} under {section} was parsed as YAML boolean {value} — quote it, e.g. '
          f'"{"on" if value else "off"}": {example}', file=sys.stderr)


def _normalise_brand_sections(data: Any, file: str) -> Any:
    """Lowercase the keys of `brands` / `aliases` (and alias values) so they match the model's normalised brand.

    YAML 1.1 reads an unquoted `on`, `off`, `yes` or `no` as a boolean, so a brand called On (the running-shoe maker)
    silently becomes the key True. The key is still coerced to a string, but we warn so the user quotes it."""
    if not isinstance(data, dict):
        return data
    for section in _BRAND_SECTIONS:
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        out: dict[str, Any] = {}
        for k, v in sec.items():
            if isinstance(k, bool):
                _warn_yaml_bool(file, section, "a key", k)
            if section == "aliases" and v is not None:
                if isinstance(v, bool):
                    _warn_yaml_bool(file, section, "an alias value", v)
                v = str(v).lower()
            out[str(k).lower()] = v
        data[section] = out
    return data


def load_yaml(name: str) -> dict:
    """private/<name> → config/<name> → config/<stem>.example.yaml"""
    stem = Path(name).stem
    for p in (PRIVATE_DIR / name, CONFIG_DIR / name, CONFIG_DIR / f"{stem}.example.yaml"):
        if p.exists():
            return _normalise_brand_sections(yaml.safe_load(p.read_text(encoding="utf-8")), p.name)
    raise FileNotFoundError(name)


def style_dir() -> Path:
    private = PRIVATE_DIR / "style_examples"
    return private if private.exists() else ROOT / "data" / "style_examples"
