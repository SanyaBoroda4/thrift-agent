"""Settings = config/settings.yaml deep-merged with config/settings.local.yaml."""
from __future__ import annotations

import os
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
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
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
        """Control files: PAUSE, HOLD_UNSHIPPED."""
        return self.path("control") / name

    def ensure_dirs(self) -> None:
        for name in ("inbox", "work", "archive", "failed", "chrome_profile", "control", "harvest"):
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


def load_yaml(name: str) -> dict:
    """private/<name> → config/<name> → config/<stem>.example.yaml"""
    stem = Path(name).stem
    for p in (PRIVATE_DIR / name, CONFIG_DIR / name, CONFIG_DIR / f"{stem}.example.yaml"):
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8"))
    raise FileNotFoundError(name)


def style_dir() -> Path:
    private = PRIVATE_DIR / "style_examples"
    return private if private.exists() else ROOT / "data" / "style_examples"
