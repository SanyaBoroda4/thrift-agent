"""WO57 §4 — an eBay listing's parcel. The buyer pays the shipping; the listing says how: `calculated` (eBay works the
charge out from the packed weight, the parcel's size and the buyer's ZIP code — USPS Ground Advantage) or `flat` (one
charge, the same for every buyer). Which of the two her own listings use is read once on the day the keys arrive
(`marketplaces.ebay.shipping.type`); the weights and sizes come from `data/ebay_shipping.json`, per item family,
rounded up. `buyer_pays` is also the shipping amount of the price formula (fees.quote). Pure functions."""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thrift_agent import families

DATA = Path(__file__).resolve().parents[2] / "data" / "ebay_shipping.json"
TYPES = ("calculated", "flat")
DEFAULT_HANDLING_DAYS = 3


class ShippingError(ValueError):
    """The shipping file is missing or doesn't hold what a listing needs."""


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Parcel(_Loose):
    weight_oz: int = Field(gt=0)
    size: str


class Box(_Loose):
    length: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Rate(_Loose):
    up_to_oz: int = Field(gt=0)
    buyer_pays: float = Field(gt=0)


class Table(_Loose):
    checked: date
    service: str
    service_code: str
    families: dict[str, Parcel]
    default: Parcel
    heavier: dict[str, Parcel] = Field(default_factory=dict)
    kids: dict[str, Parcel] = Field(default_factory=dict)
    sizes: dict[str, Box]
    rates: list[Rate] = Field(min_length=1)

    def problems(self) -> list[str]:
        out = [f"family {f!r} isn't one of ours" for f in self.families if f not in families.FAMILIES]
        used = [self.default, *self.families.values(), *self.heavier.values(), *self.kids.values()]
        out += [f"size {p.size!r} has no box" for p in used if p.size not in self.sizes]
        if [r.up_to_oz for r in self.rates] != sorted({r.up_to_oz for r in self.rates}):
            out.append("rates must rise by weight")
        return out


class Plan(BaseModel):
    """What one listing says about its shipping."""
    type: str                     # calculated | flat
    service: str                  # "USPS Ground Advantage"
    service_code: str             # eBay's code for it
    weight_oz: int                # packed, rounded up
    pounds: int                   # the same as eBay's forms take it: whole pounds …
    ounces: int                   # … and the ounces over
    length: int
    width: int
    height: int
    buyer_pays: float             # the flat charge; with calculated shipping, the estimate for the price formula
    handling_days: int
    family: str | None = None
    why: str = ""                 # "shoes, a boot: 4 lb" — for the item's check line and the dry run


_CACHE: dict[Path, tuple[float, Table]] = {}


def load(path: Path | None = None) -> Table:
    file = Path(path or DATA)
    try:
        mtime = file.stat().st_mtime
    except OSError:
        raise ShippingError(f"{file.name} is missing") from None
    hit = _CACHE.get(file)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        table = Table.model_validate(json.loads(file.read_text(encoding="utf-8")))
    except ValidationError as e:
        why = "; ".join(f"{'.'.join(map(str, er['loc'])) or 'file'}: {er['msg']}" for er in e.errors()[:3])
        raise ShippingError(f"{file.name} is not usable ({why[:300]})") from None
    except (json.JSONDecodeError, ValueError) as e:
        raise ShippingError(f"{file.name} is not usable ({type(e).__name__}: {str(e).splitlines()[0][:200]})") from None
    if problems := table.problems():
        raise ShippingError(f"{file.name} is not usable ({'; '.join(problems[:4])})")
    _CACHE[file] = (mtime, table)
    return table


def round_up(weight_oz: float) -> int:
    """A packed weight rounded UP the way the carrier bills it: to the next 4 ounces under a pound, to the next whole
    pound from there (15.1 oz is a pound; 2 lb 1 oz is 3 lb)."""
    w = max(1.0, float(weight_oz))
    if w <= 15.999:
        return int(math.ceil(w / 4.0) * 4)
    return int(math.ceil(w / 16.0) * 16)


def charge(table: Table, weight_oz: int) -> float:
    """What a buyer is typically charged at this weight (the heaviest row for anything above the table)."""
    for rate in table.rates:
        if weight_oz <= rate.up_to_oz:
            return rate.buyer_pays
    return table.rates[-1].buyer_pays


def parcel_for(family: str | None, package_class: str | None, department: str | None,
               table: Table | None = None) -> tuple[Parcel, str]:
    """The parcel of an item: its family's, a coat's or a boot's heavier one (`package_class` "heavy" —
    catalogs.common.package_class, the estimate behind Depop's and Vinted's package sizes), a kids' item's lighter one.
    An unknown family gets the default. Returns it with the words for the check line."""
    table = table or load()
    if (department or "") == "Kids":
        key = "shoes" if family == "shoes" else "outerwear" if family == "outerwear" or package_class == "medium" \
            else "clothing"
        if key in table.kids:
            return table.kids[key], f"kids {key}"
    if package_class == "heavy" and family in table.heavier:
        return table.heavier[family], f"{family}, heavy"
    if family in table.families:
        return table.families[family], str(family)
    return table.default, "no family read: the default parcel"


def plan(family: str | None, package_class: str | None = None, department: str | None = None, *,
         kind: str = "calculated", handling_days: int | None = None, flat: float | None = None,
         table: Table | None = None) -> Plan:
    """The shipping of one listing. `kind`: calculated | flat (her listings' own type); `flat`: a fixed charge that
    replaces the table's (the owner's setting); `handling_days`: hers, else 3 business days."""
    if kind not in TYPES:
        raise ShippingError(f"shipping type must be one of {', '.join(TYPES)}, not {kind!r}")
    table = table or load()
    parcel, why = parcel_for(family, package_class, department, table)
    weight = round_up(parcel.weight_oz)
    box = table.sizes[parcel.size]
    pays = float(flat) if (kind == "flat" and flat) else charge(table, weight)
    pounds, ounces = divmod(weight, 16)
    return Plan(type=kind, service=table.service, service_code=table.service_code, weight_oz=weight, pounds=pounds,
                ounces=ounces, length=box.length, width=box.width, height=box.height, buyer_pays=pays,
                handling_days=int(handling_days or DEFAULT_HANDLING_DAYS), family=family,
                why=f"{why}: {pounds} lb {ounces} oz" if pounds else f"{why}: {ounces} oz")
