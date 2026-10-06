"""The marketplaces' listing-form catalogs (WO30): read-only reference data in data/ — Depop's and Vinted's, read from
their logged-in create forms and web APIs on 2026-10-06, next to Poshmark's (data/poshmark_catalog.json). Every dropdown
value the cross-lister fills is one of these: the agent never types a free-text guess into a dropdown.

`depop()` / `vinted()` load a file once per process (again when it changes on disk: `thrift catalogs refresh` rewrites
them) and validate it. `check()` is the startup check: a file that is missing or doesn't hold what the mapping needs
turns that marketplace's cross-listing off with the reason — Poshmark is never affected."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
FILES = {"depop": "depop_catalog.json", "vinted": "vinted_catalog.json"}
CROSS = ("depop", "vinted")                 # the marketplaces an item is cross-listed on after Poshmark (WO30)


class CatalogError(ValueError):
    """A catalog file that is missing or doesn't hold what the mapping needs."""


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- Vinted

class Option(_Loose):
    id: int
    title: str
    group: str | None = None


class Library(_Loose):
    field_title: str
    options: list[Option]


class VField(_Loose):
    required: bool = False
    lib: str | None = None
    max: int | None = None


class VCategory(_Loose):
    path: str
    fields: dict[str, VField]


class VColor(_Loose):
    id: int
    title: str


class VPackage(_Loose):
    id: int
    code: str


class Vinted(_Loose):
    form: dict[str, Any]
    condition_map_from_agent: dict[str, Any]
    package_sizes: list[VPackage]
    colors: list[VColor]
    option_libraries: dict[str, Library]
    categories: dict[str, VCategory]

    @model_validator(mode="after")
    def _consistent(self):
        problems = []
        for cid, c in self.categories.items():
            if not cid.isdigit():
                problems.append(f"category id {cid!r} is not a number")
            for name, f in c.fields.items():
                if f.lib and f.lib not in self.option_libraries:
                    problems.append(f"{c.path}: {name} uses a library the file doesn't have ({f.lib})")
        conditions = {o.id for o in self.library("condition_0")}
        for grade, cid in self.condition_map_from_agent.items():
            if isinstance(cid, int) and cid not in conditions:
                problems.append(f"condition {grade} -> {cid} is not a condition_0 option")
        if not self.colors or not self.package_sizes:
            problems.append("no colors or package sizes")
        if problems:
            raise ValueError("; ".join(problems[:8]))
        return self

    def library(self, name: str) -> list[Option]:
        lib = self.option_libraries.get(name)
        return list(lib.options) if lib else []

    def leaf(self, path: str) -> int | None:
        """The id of the leaf category at `path` ("Women > Clothing > Skirts"), or None."""
        return self._paths().get(path)

    def _paths(self) -> dict[str, int]:
        cached = self.__dict__.get("_by_path")
        if cached is None:
            cached = {c.path: int(cid) for cid, c in self.categories.items()}
            self.__dict__["_by_path"] = cached
        return cached

    def category(self, cid: int) -> VCategory:
        return self.categories[str(cid)]

    def department_leaves(self, department: str) -> dict[int, str]:
        """{id: path} of every leaf under Women, Men or Kids."""
        return {int(cid): c.path for cid, c in self.categories.items() if c.path.split(" > ")[0] == department}

    def color(self, title: str) -> int | None:
        return next((c.id for c in self.colors if c.title.lower() == title.lower()), None)

    def option(self, lib: str, title: str) -> int | None:
        return next((o.id for o in self.library(lib) if o.title.lower() == title.lower()), None)


# ---------------------------------------------------------------- Depop

class SizeSet(_Loose):
    path: str
    sizes: list[str]


class DCategory(_Loose):
    attributes: list[str] | None = None


class Depop(_Loose):
    form: dict[str, Any]
    condition: list[str]
    condition_map_from_agent: dict[str, str]
    color: dict[str, Any]
    source: dict[str, Any]
    age: dict[str, Any]
    package_sizes: list[dict[str, Any]]
    attribute_limits: dict[str, int]
    attribute_values: dict[str, list[str]]
    size_sets_US: dict[str, SizeSet]
    product_types_by_department: dict[str, list[str]]
    ui_name_to_slug: dict[str, str]
    categories: dict[str, DCategory]

    @model_validator(mode="after")
    def _consistent(self):
        problems = []
        for path, c in self.categories.items():
            if len(path.split(" > ")) != 3:
                problems.append(f"category {path!r} is not 'Department > Group > Category'")
            for a in c.attributes or []:
                if a not in self.attribute_values:
                    problems.append(f"{path}: attribute {a} has no values")
        for dept, types in self.product_types_by_department.items():
            for t in types:
                slug, _, sid = t.partition("=")
                if "/" not in slug or (sid != "-" and sid not in self.size_sets_US):
                    problems.append(f"{dept}: product type {t!r}")
        for grade, value in self.condition_map_from_agent.items():
            if grade != "never use" and value not in self.condition:
                problems.append(f"condition {grade} -> {value!r} is not a Depop condition")
        for field in ("color", "source", "age"):
            values = getattr(self, field).get("values") or []
            if not values or (getattr(self, field).get("default") not in (None, *values)):
                problems.append(f"{field}: no values or a default outside them")
        if not self.form.get("comboboxes"):
            problems.append("form.comboboxes missing")
        if problems:
            raise ValueError("; ".join(problems[:8]))
        return self

    def colors(self) -> list[str]:
        return list(self.color["values"])

    def size_set(self, sid: str | None) -> list[str]:
        return list(self.size_sets_US[sid].sizes) if sid and sid in self.size_sets_US else []

    def product_types(self, department: str) -> dict[str, str]:
        """{"bottoms/trousers": "22", "accessories/bag": "-", …} for Women / Men / Kids."""
        key = DEPOP_DEPARTMENT.get(department, department)
        return dict(t.split("=", 1) for t in self.product_types_by_department.get(key, []))

    def product_type(self, path: str) -> str | None:
        """The product type slug ("bottoms/trousers") behind a category path ("Women > Bottoms > Pants")."""
        dept, group, name = path.split(" > ")
        types = self.product_types(dept)
        gslug = DEPOP_GROUP.get(group)
        if gslug is None or not types:
            return None
        if (forced := DEPOP_SLUG_OVERRIDES.get((group, name))) and forced in types:
            return forced
        ui = self.ui_name_to_slug.get(f"{name} (coats and jackets)" if (group, name) == ("Coats and jackets", "Vests")
                                      else name)
        if ui and f"{gslug}/{ui}" in types:
            return f"{gslug}/{ui}"
        mine = [t for t in types if t.startswith(gslug + "/")]
        want = _slug_key("other " + gslug.replace("-", " ") if name == "Other" else name)
        exact = [t for t in mine if _slug_key(t.split("/", 1)[1].replace("-", " ")) == want]
        if len(exact) == 1:
            return exact[0]
        loose = [t for t in mine if (k := _slug_key(t.split("/", 1)[1].replace("-", " "))) and
                 (k.startswith(want) or want.startswith(k))]
        return loose[0] if len(loose) == 1 else None

    def size_set_id(self, path: str) -> str | None:
        """The US size set of a category ("22"), or None when it has no size field."""
        t = self.product_type(path)
        sid = self.product_types(path.split(" > ")[0]).get(t) if t else None
        return None if sid in (None, "-") else sid

    def attributes(self, path: str) -> list[str]:
        c = self.categories.get(path)
        return list(c.attributes or []) if c else []

    def department_paths(self, department: str) -> list[str]:
        return [p for p in self.categories if p.split(" > ")[0] == department]


DEPOP_DEPARTMENT = {"Women": "womenswear", "Men": "menswear", "Kids": "kidswear"}
DEPOP_GROUP = {"Tops": "tops", "Bottoms": "bottoms", "Dresses": "dresses", "Coats and jackets": "coats-jackets",
               "Jumpsuits and rompers": "jumpsuit-and-playsuit", "Suits": "suits", "Footwear": "footwear",
               "Accessories": "accessories", "Sleepwear": "nightwear", "Underwear": "underwear",
               "Swimwear": "swim-beach-wear", "Costume": "fancy-dress", "Onesies and sleepers": "sleepsuits-and-bodysuits",
               "Clothing bundles": "bundles"}
# The menu names whose product type slug isn't a spelling of the name (the catalog's ui_name_to_slug covers the rest).
DEPOP_SLUG_OVERRIDES = {
    ("Suits", "Vests"): "suits/waistcoats-vests",
    ("Accessories", "Scarves and wraps"): "accessories/scarf-wraps",
    ("Accessories", "Wallets and cardholders"): "accessories/wallet-purses",
    ("Accessories", "Jewelry"): "accessories/jewellery",
    ("Accessories", "Hats and caps"): "accessories/hat",
    ("Accessories", "Bags"): "accessories/bag",
    ("Accessories", "Belts"): "accessories/belt",
    ("Accessories", "Watches"): "accessories/watch",
    ("Swimwear", "Bikini and tankini sets"): "swim-beach-wear/bikinis-and-tankini-sets",
    ("Swimwear", "Swim briefs and shorts"): "swim-beach-wear/swim-briefs-shorts",
    ("Costume", "Costume"): "fancy-dress/fancy-dress",
    ("Onesies and sleepers", "Onesies and sleepers"): "sleepsuits-and-bodysuits/sleepsuits-babygrows",
    ("Clothing bundles", "Clothing bundles"): "bundles/bundles",
    ("Footwear", "Flip flops"): "footwear/flipflops",
}


def _slug_key(s: str) -> str:
    s = re.sub(r"\band\b", " ", s.lower().replace("&", " and "))
    return re.sub(r"[^a-z]", "", s).rstrip("s")


# ---------------------------------------------------------------- loading

_CACHE: dict[str, tuple[float, Path, Any]] = {}
_MODELS = {"depop": Depop, "vinted": Vinted}


def path(mp: str) -> Path:
    return DATA_DIR / FILES[mp]


def load(mp: str, file: Path | None = None) -> Depop | Vinted:
    """The validated catalog of `mp`, cached until the file changes. Raises CatalogError."""
    file = file or path(mp)
    try:
        mtime = file.stat().st_mtime
    except OSError:
        raise CatalogError(f"{mp}: {file.name} is missing") from None
    hit = _CACHE.get(mp)
    if hit and hit[0] == mtime and hit[1] == file:
        return hit[2]
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        cat = _MODELS[mp].model_validate(data)
    except ValidationError as e:
        why = "; ".join(f"{'.'.join(map(str, er['loc'])) or 'file'}: {er['msg']}" for er in e.errors()[:3])
        raise CatalogError(f"{mp}: {file.name} is not usable ({why[:400]})") from None
    except (json.JSONDecodeError, ValueError) as e:
        raise CatalogError(f"{mp}: {file.name} is not usable ({type(e).__name__}: {str(e).splitlines()[0][:300]})") \
            from None
    _CACHE[mp] = (mtime, file, cat)
    return cat


def depop_catalog() -> Depop:
    return load("depop")


def vinted_catalog() -> Vinted:
    return load("vinted")


def check() -> dict[str, str | None]:
    """The startup check: {marketplace: None (fine) | the problem} for Depop and Vinted."""
    out: dict[str, str | None] = {}
    for mp in CROSS:
        try:
            load(mp)
            out[mp] = None
        except CatalogError as e:
            out[mp] = str(e)
    return out
