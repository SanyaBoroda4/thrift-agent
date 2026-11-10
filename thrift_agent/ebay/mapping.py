"""WO57 §4 — an item as an eBay listing: its category, condition and item specifics, every value one of eBay's own.

Nothing here is typed from a guess. The category comes from eBay's own suggestions for the item (Taxonomy API), picked
inside the item's family and department; the condition from the conditions that category takes; each item specific
("aspect": Brand, Size, Size Type, Department, Type, Color, Style …) from the item's facts, spelled as eBay's value
list spells it. A required aspect is always filled — the facts' value, else a neutral value of eBay's own ("Unknown",
"Other"), each such choice said in the item's check line; an optional aspect the facts don't give is left out. No brand
is "Unbranded". Sizes are eBay's standard values (the Size list of the category, for its Size Type); the label's own
size goes into the description when it differs. Never Fair. The price is the fee formula's (`fees.quote`), never typed.

Pure functions over eBay's recorded answers (`Category`) and the item (`ItemView`): no network, no clock."""
from __future__ import annotations

import html
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thrift_agent import brands, families
from thrift_agent.catalogs import common
from thrift_agent.catalogs.common import ItemView, MappingError
from thrift_agent.ebay import fees, shipping
from thrift_agent.post import fallback

TITLE_MAX = 80
PHOTOS_MAX = 24
MULTI_MAX = 3                # values of one multi-valued aspect ("Features", "Material")
UNBRANDED = "Unbranded"
NEUTRAL = ("Unknown", "Not Specified", "Other", "N/A", "Does not apply", "Does Not Apply", "See Description")
NO_SIZE = "no size on the item"          # crosslist.NO_SIZE: the one reason every site skips for (her size settles it)
# our condition grade → eBay's condition ids, the first the category takes (never 3010, Pre-owned – Fair: invariant 2)
CONDITIONS = {"NWT": ("1000",), "NWOT": ("1500", "1000"), "like_new": ("2990", "3000"), "excellent": ("2990", "3000"),
              "good": ("3000", "2990"), "fair": ("3000", "2990")}
NEVER_CONDITION = ("3010", "7000")       # Pre-owned – Fair; For parts or not working
CONDITION_NAMES = {"1000": "New with tags", "1500": "New without tags", "1750": "New with defects",
                   "2990": "Pre-owned - Excellent", "3000": "Pre-owned - Good", "3010": "Pre-owned - Fair"}


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- eBay's answers, as the mapping reads them

class Suggestion(_Loose):
    """One category eBay suggests for a query (Taxonomy getCategorySuggestions)."""
    id: str
    name: str
    path: list[str]              # names, root → leaf: ["Clothing, Shoes & Accessories", "Women", …, "Dresses"]
    ids: list[str]               # ids, root → leaf (the leaf's own last)

    @property
    def text(self) -> str:
        return " > ".join(self.path)


class Aspect(_Loose):
    """One item specific of a category (Taxonomy getItemAspectsForCategory)."""
    name: str
    required: bool = False
    usage: str = "OPTIONAL"      # RECOMMENDED | OPTIONAL
    mode: str = "FREE_TEXT"      # FREE_TEXT | SELECTION_ONLY
    multi: bool = False
    max_length: int | None = None
    values: list[str] = Field(default_factory=list)
    # value → {another aspect's name: the values of it this value is for} ("XS" → {"Size Type": ["Regular"]})
    constraints: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    def allowed(self, chosen: dict[str, list[str]]) -> list[str]:
        """The values that go with what is chosen already (Size by its Size Type); all of them when none is tied."""
        out = []
        for v in self.values:
            ties = self.constraints.get(v) or {}
            if all(not chosen.get(other) or set(chosen[other]) & set(ok) for other, ok in ties.items()):
                out.append(v)
        return out or list(self.values)


class Category(_Loose):
    """A leaf category with what a listing in it needs."""
    id: str
    name: str
    path: list[str]
    ids: list[str]
    aspects: list[Aspect] = Field(default_factory=list)
    conditions: list[tuple[str, str]] = Field(default_factory=list)     # (id, eBay's name), as the category takes them

    @property
    def text(self) -> str:
        return " > ".join(self.path)


def parse_suggestions(answer: dict) -> list[Suggestion]:
    out = []
    for s in (answer or {}).get("categorySuggestions") or []:
        cat = s.get("category") or {}
        if not cat.get("categoryId"):
            continue
        up = sorted(s.get("categoryTreeNodeAncestors") or [], key=lambda a: a.get("categoryTreeNodeLevel") or 0)
        out.append(Suggestion(id=str(cat["categoryId"]), name=str(cat.get("categoryName") or ""),
                              path=[str(a.get("categoryName") or "") for a in up] + [str(cat.get("categoryName") or "")],
                              ids=[str(a.get("categoryId")) for a in up] + [str(cat["categoryId"])]))
    return out


def parse_aspects(answer: dict) -> list[Aspect]:
    out = []
    for a in (answer or {}).get("aspects") or []:
        name = str(a.get("localizedAspectName") or "").strip()
        if not name:
            continue
        c = a.get("aspectConstraint") or {}
        values, ties = [], {}
        for v in a.get("aspectValues") or []:
            text = str(v.get("localizedValue") or "").strip()
            if not text:
                continue
            values.append(text)
            for vc in v.get("valueConstraints") or []:
                other = str(vc.get("applicableForLocalizedAspectName") or "").strip()
                if other:
                    ties.setdefault(text, {})[other] = [str(x) for x in vc.get("applicableForLocalizedAspectValues") or []]
        out.append(Aspect(name=name, required=bool(c.get("aspectRequired")), usage=str(c.get("aspectUsage") or "OPTIONAL"),
                          mode=str(c.get("aspectMode") or "FREE_TEXT"),
                          multi=str(c.get("itemToAspectCardinality") or "SINGLE") == "MULTI",
                          max_length=c.get("aspectMaxLength"), values=values, constraints=ties))
    return out


def parse_conditions(answer: dict) -> list[tuple[str, str]]:
    """(condition id, eBay's name) for a category (Metadata getItemConditionPolicies)."""
    for policy in (answer or {}).get("itemConditionPolicies") or []:
        found = [(str(c.get("conditionId")), str(c.get("conditionDescription") or CONDITION_NAMES.get(
            str(c.get("conditionId")), ""))) for c in policy.get("itemConditions") or [] if c.get("conditionId")]
        if found:
            return found
    return []


# ---------------------------------------------------------------- words

def _key(text) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\band\b", "&", str(text or "").lower()))


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _tokens(text) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'", "").replace("’", ""))]


BREAK = "|"                  # between two phrases of the item's words: a value never matches across it
# what our words mean in eBay's ("mini" is its Short, "maxi" its Long, a tee its T-Shirt): added after the words
ALSO = {"mini": "short", "maxi": "long", "tee": "t shirt", "tshirt": "t shirt", "bootie": "ankle boot",
        "trainer": "sneaker", "hoodie": "hooded", "sleeveles": "sleeveless", "crewneck": "crew neck",
        "vneck": "v neck", "jogger": "joggers", "legging": "leggings", "zipper": "zip", "zipped": "zip",
        "lined": "lined", "floral": "floral", "plaid": "plaid", "stripe": "striped", "striped": "striped",
        "pullover": "pullover", "moto": "moto", "kneelength": "knee length"}


def item_words(view: ItemView) -> list[str]:
    """The item's own words, the surest first: its type, Poshmark's subcategory and category, the title, the colour
    words, the features the photos show — each a phrase of its own — then eBay's words for ours (ALSO)."""
    f, r = view.facts, view.render
    parts = [f.item_type, r.subcategory or "", r.category, r.title, f.color_name or "", *(f.features or [])]
    out: list[str] = []
    for part in parts:
        out += [*_tokens(part), BREAK]
    for word in dict.fromkeys(w for w in out if w in ALSO):
        out += [*_tokens(ALSO[word]), BREAK]
    return out


def _phrase_at(words: list[str], phrase: list[str]) -> int | None:
    """Where `phrase` stands in `words` as whole words in a row, else None."""
    n = len(phrase)
    if not n:
        return None
    for i in range(len(words) - n + 1):
        if words[i:i + n] == phrase:
            return i
    return None


def from_words(values: list[str], words: list[str], limit: int = 1) -> list[str]:
    """eBay's values the item's words say: a value is taken when all its words stand in the item's words in a row
    ("Ankle Boot" in "suede ankle boots"); the longest first, then the one the words say earliest."""
    hits = []
    for order, value in enumerate(values):
        phrases = [_tokens(value)]
        parts = [p.strip() for p in str(value).split("/")]
        if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z][A-Za-z -]{2,}", p) for p in parts):
            phrases = [_tokens(p) for p in parts]          # "Coat/Jacket", "Party/Cocktail": either names it
        found = [(len(p), at) for p in phrases if p and (at := _phrase_at(words, p)) is not None]
        if found:
            size, at = max(found, key=lambda x: (x[0], -x[1]))
            hits.append((-size, at, order, value))
    return [h[3] for h in sorted(hits)[:limit]]


def exact(values: list[str], *names: str | None) -> str | None:
    """eBay's own spelling of the first of `names` its list holds (case, spaces and punctuation aside)."""
    keyed = {_key(v): v for v in values if _key(v)}
    for n in names:
        if n and _key(n) in keyed:
            return keyed[_key(n)]
    return None


# ---------------------------------------------------------------- the category

DEPARTMENT_WORDS = {"Women": ("women",), "Men": ("men",), "Kids": ("kids", "girls", "boys", "baby", "toddler")}
PET_ROOT = "Pet Supplies"
FASHION_ROOT = "Clothing, Shoes & Accessories"


def category_family(path: list[str]) -> str | None:
    """The family of an eBay category path, as families reads the other sites' — and a pet's own things."""
    if any(_key(p) == _key(PET_ROOT) for p in path):
        return "pet"
    # the tree's root names three families at once ("Clothing, Shoes & Accessories"): it says nothing about the item
    return families.family_of_category("ebay", [p for p in path if _key(p) != _key(FASHION_ROOT)])


def in_department(path: list[str], department: str, kids_gender: str | None = None) -> bool:
    """Does an eBay category path belong to the item's department (Women / Men / Kids)? Home and Unisex go anywhere."""
    words = DEPARTMENT_WORDS.get(department)
    if not words:
        return True
    heads = {_tokens(p)[0] for p in path if _tokens(p)}
    mine = {_stem(w) for w in words}
    other = {_stem(w) for dept, ws in DEPARTMENT_WORDS.items() if dept != department for w in ws}
    if heads & mine:
        return True
    return not (heads & other) and department != "Kids"


def category_queries(view: ItemView) -> list[str]:
    """What eBay's category suggester is asked, in order: whose it is and what it is ("women's wrap midi dress"), then
    the title with whose it is."""
    f, r = view.facts, view.render
    if families.is_pet_item(f.item_type):
        whose = ""
    elif r.department == "Kids":
        whose = {"girls": "girls", "boys": "boys"}.get(str(r.kids_gender or f.kids_gender or ""), "kids")
    else:
        whose = {"Women": "women's", "Men": "men's"}.get(r.department, "")
    title = re.sub(r"\bsize\s+\S+\s*$", "", r.title, flags=re.I).strip()
    # last, whose it is and Poshmark's own category ("girls Shoes"): a plain query that names the department, for an
    # item whose own words eBay files under another one (a toddler's Mary Janes suggested only as women's flats)
    plain = f"{whose} {r.category}".strip() if whose and r.category else ""
    return list(dict.fromkeys(q for q in (f"{whose} {f.item_type}".strip(), f"{whose} {title}".strip(), plain) if q))


def choose_category(view: ItemView, suggestions: list[Suggestion]) -> tuple[Suggestion, str | None]:
    """The suggested category that is the item's own: its family AND department, in eBay's order of relevance; else its
    family; else its department; else eBay's first — the closest valid one, never refused (the note says which)."""
    if not suggestions:
        raise MappingError("eBay suggested no category")
    mine = families.family_of_type(view.facts.item_type)
    dept, gender = view.render.department, view.render.kids_gender or view.facts.kids_gender

    def fam_ok(s: Suggestion) -> bool:
        theirs = category_family(s.path)
        return mine is None or (theirs is not None and families.same_family(mine, theirs))

    def dept_ok(s: Suggestion) -> bool:
        return mine == "pet" or in_department(s.path, dept, gender)

    def strict(s: Suggestion) -> bool:
        return mine is not None and category_family(s.path) == mine

    for test, note in ((lambda s: strict(s) and dept_ok(s), None), (lambda s: fam_ok(s) and dept_ok(s), None),
                       (fam_ok, "eBay suggested none in the item's department: the closest of its family taken"),
                       (dept_ok, "eBay suggested none of the item's family: the closest of its department taken")):
        for s in suggestions:
            if test(s):
                return s, (f"category {s.text} — {note}" if note else None)
    s = suggestions[0]
    return s, f"category {s.text} — eBay's first suggestion (none of the item's family or department)"


def pick_category(view: ItemView, answers: list[list[Suggestion]]) -> tuple[Suggestion, str | None]:
    """The category from eBay's answers to `category_queries`, in order: the first answer that holds one of the item's
    own (no note) wins; else the best of all of them together, with its note."""
    for suggestions in answers:
        if suggestions:
            chosen, note = choose_category(view, suggestions)
            if note is None:
                return chosen, None
    merged = list({s.id: s for suggestions in answers for s in suggestions}.values())
    return choose_category(view, merged)


SIZE_LIST = "isn't on eBay's size list"
OTHER_CATEGORIES = 3


def ranked_categories(view: ItemView, answers: list[list[Suggestion]]) -> list[tuple[Suggestion, str | None]]:
    """The category picked, then (at most three) other suggestions that are the item's own too — its family AND
    department: eBay keeps toddler sizes under "Baby & Toddler Clothing" and a girl's 10 under "Girls' Clothing (Sizes 4
    & Up)", and only the category's own size list says which one the item's size stands in."""
    first, note = pick_category(view, answers)
    out = [(first, note)]
    for s in {s.id: s for suggestions in answers for s in suggestions}.values():
        if len(out) > OTHER_CATEGORIES:
            break
        if s.id != first.id and choose_category(view, [s])[1] is None:
            out.append((s, None))
    return out


def size_refused(e: Exception) -> bool:
    return SIZE_LIST in str(e)


def size_as_written(f) -> bool:
    """The listing's size is the item's own (no nearest / neutral value taken): no other category need be tried."""
    return not any(str(g).startswith("size ") for g in f.guesses)


# ---------------------------------------------------------------- the condition

def condition_for(grade: str, offered: list[tuple[str, str]]) -> tuple[str, str, str | None]:
    """(eBay's condition id, its name, a note when it isn't the plain one) for our grade, among the conditions the
    category takes: NWT → New with tags; new without tags → New without tags; like new and excellent → Pre-owned –
    Excellent; good → Pre-owned – Good (a fair reading too: never Fair). A category without that one takes the nearest
    it has — never Fair, never "for parts"."""
    names = dict(offered)
    wanted = CONDITIONS.get(grade, CONDITIONS["good"])
    if not names:                                        # nothing recorded for the category: eBay's standard ids
        cid = wanted[0]
        return cid, CONDITION_NAMES.get(cid, cid), None
    for n, cid in enumerate(wanted):
        if cid in names:
            return cid, names[cid] or CONDITION_NAMES.get(cid, cid), None if n == 0 else \
                f"condition: this category has no {CONDITION_NAMES.get(wanted[0], wanted[0])} — " \
                f"{names[cid] or CONDITION_NAMES.get(cid, cid)} taken"
    new = grade in ("NWT", "NWOT")
    pool = [(cid, name) for cid, name in offered if cid not in NEVER_CONDITION and cid.isdigit()
            and (int(cid) < 2000) == new]
    if not pool:
        # never a new item listed as used, never a used one as new: a category that takes only new items (eBay's
        # underwear rule) has no place for a pre-owned one — this site is left out for the item, with the reason
        raise MappingError(f"eBay takes only {'used' if new else 'new'} items in this category "
                           f"({', '.join(n or c for c, n in offered)}): a {'new' if new else 'pre-owned'} one can't "
                           f"be listed there")
    cid, name = min(pool, key=lambda c: abs(int(c[0]) - int(wanted[0])))
    return cid, name or CONDITION_NAMES.get(cid, cid), f"condition: the nearest this category takes — {name or cid}"


# ---------------------------------------------------------------- sizes

_MONTHS = re.compile(r"^\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*(?:months?|mos?|m)\s*$", re.I)


def size_candidates(value: str | None, marked: bool = False) -> list[str]:
    """Our size (Poshmark's menu value, else the US size) in the spellings eBay's lists may use, the plainest first:
    "Waist 32" → 32; "7.5 (Toddler Girl)" → 7.5; "12 Months" → 12 Months / 12M / 12 mo; "XXL" → XXL / 2XL; "One Size" →
    One Size / OS; "34E (DD)" → 34DD / 34E; "8P" → 8P / P8 / 8 (its Size Type says Petite). `marked`: only the
    spellings that keep a fit mark ("14W", never the bare 14)."""
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return []
    out = [raw]
    bare = re.sub(r"\s*\([^)]*\)\s*", " ", raw).strip()
    out.append(bare)
    if m := re.fullmatch(r"(?:waist|neck|size|us)\s+(\S+)", bare, re.I):
        bare = m.group(1)
        out.append(bare)
    if m := re.fullmatch(r"(\d{2}[A-K]{1,2})\s*\(([A-K]{1,3})\)", raw, re.I):       # 34E (DD)
        out += [re.sub(r"[A-K]+$", m.group(2).upper(), m.group(1), flags=re.I), m.group(1)]
    if m := _MONTHS.match(bare):
        a, b = m.group(1), m.group(2)
        span = f"{a}-{b}" if b else a
        out += [f"{span} Months", f"{span}M", f"{span} mo", f"{span} Mo.", f"{span} Month" if span == "1" else ""]
    if re.fullmatch(r"(?:nb|newborn)", bare, re.I):
        out += ["Newborn", "NB"]
    if re.fullmatch(r"(?:one\s*size|os|o/s)", bare, re.I):
        out += ["One Size", "OS", "One Size Fits All", "One Size Fits Most", "OSFA"]
    up = bare.upper()
    spell = {"XXL": "2XL", "2XL": "XXL", "XXXL": "3XL", "3XL": "XXXL", "XXXXL": "4XL", "4XL": "XXXXL", "XXS": "2XS",
             "2XS": "XXS"}
    if up in spell:
        out.append(spell[up])
    weak = []                # the size without its fit mark: right only under the Size Type the mark names
    if m := re.fullmatch(r"(\d{1,2}(?:\.5)?)\s*([PWT])", up):                        # 8P, 14W: the number is the size
        out += [f"{m.group(1)}{m.group(2)}", f"{m.group(2)}{m.group(1)}"]
        weak.append(m.group(1))
    if m := re.fullmatch(r"P?(XXS|XS|S|M|L|XL)P?", up):                               # PM / MP: petite letters
        if up != m.group(1):
            weak.append(m.group(1))
    if m := re.fullmatch(r"(\d{1,2}(?:\.5)?)\s*[CY]", up):                            # 11.5C, 4Y: the bare number
        out.append(m.group(1))
    strong = list(dict.fromkeys(o for o in out if o))
    return strong if marked else list(dict.fromkeys([*strong, *weak]))


NEUTRAL_SIZES = ("One Size", "One Size Fits All", "One Size Fits Most", "Other")


def size_value(want: str | None, offered: list[str], item_type: str | None) -> tuple[str | None, str | None]:
    """(eBay's size value, a note when it isn't the size as written). eBay's size list is CLOSED (since 2026-08-26 a
    size that isn't one of the category's standard values blocks the listing, whatever the metadata says about free
    text): the exact value in one of its spellings; else, in the other sites' order, the bigger of a dual label, the
    nearest of the same system one step at most, One Size for a one-size item; else the list's neutral choice (the
    label's own size then stands in the description). (None, None) when nothing of the list is reasonable. A category
    with no size list (not apparel or footwear) takes the size as written."""
    cands = size_candidates(want)
    if not cands:
        return None, None
    if not offered:
        return cands[min(1, len(cands) - 1)], None
    if (hit := exact(offered, *cands)) is not None:
        return hit, None
    for c in cands:
        if (found := fallback.menu_size(c, offered, item_type)) is not None:
            value, how = found
            return value, None if how == "exact" else f"size {want}: eBay's list has '{value}' ({how})"
    if (hit := exact(offered, *NEUTRAL_SIZES)) is not None:
        return hit, f"size {want} has no place on eBay's size list for this category — '{hit}' taken, the label's " \
                    f"size is in the description"
    return None, None


# ---------------------------------------------------------------- one aspect from the facts

EBAY_COLORS = {"Red": ("Red",), "Pink": ("Pink",), "Orange": ("Orange",), "Yellow": ("Yellow",), "Green": ("Green",),
               "Blue": ("Blue",), "Purple": ("Purple",), "Gold": ("Gold",), "Silver": ("Silver",), "Black": ("Black",),
               "Gray": ("Gray", "Grey"), "White": ("White",), "Cream": ("Ivory", "Cream", "Beige", "White"),
               "Brown": ("Brown",), "Tan": ("Tan", "Beige", "Brown")}
SIZE_TYPES = {"petite": "Petite", "plus": "Plus", "maternity": "Maternity", "juniors": "Juniors", "tall": "Tall",
              "big & tall": "Big & Tall", "big and tall": "Big & Tall"}
MATERIAL_ASPECTS = ("material", "outer shell material", "upper material", "exterior material", "lining material",
                    "fabric type", "insole material", "outsole material")
COLOR_ASPECTS = ("color", "exterior color", "main color", "colour")
SIZE_ASPECTS = ("size", "us shoe size", "dog size", "bottoms size", "waist size")


class Found(BaseModel):
    """What one aspect gets: its values, and the note when a choice was made for it."""
    values: list[str] = Field(default_factory=list)
    note: str | None = None


def _brand(view: ItemView, a: Aspect, aliases=None) -> Found:
    names = brands.candidates(view.render.brand, view.render.brand_label, aliases)
    if not names:
        return Found(values=[exact(a.values, UNBRANDED) or UNBRANDED])
    choice, how, came = brands.choose(names, a.values)
    if choice is not None:
        note = None if how == "exact" and choice.lower() == names[0].lower() else f"brand on eBay: '{choice}' (from '{came}')"
        return Found(values=[choice], note=note)
    if a.mode == "SELECTION_ONLY" and a.values:
        return Found(values=[exact(a.values, UNBRANDED) or UNBRANDED],
                     note=f"brand: eBay's list for this category has no '{names[0]}'{brands.offered(a.values)}")
    return Found(values=[names[0]])                      # eBay takes the brand as text: listed under its own name


def _department(view: ItemView, a: Aspect) -> Found:
    r, f = view.render, view.facts
    gender = str(r.kids_gender or f.kids_gender or "")
    if r.department == "Kids":
        baby = (r.size_tab or "") == "Baby" or bool(_MONTHS.match(str(r.size_value or r.size or "")))
        want = (["Baby"] if baby else []) + {"girls": ["Girls"], "boys": ["Boys"]}.get(gender, []) + \
            ["Unisex Kids", "Girls", "Boys", "Kids"]
    elif r.department == "Men":
        want = ["Men", "Unisex Adults"]
    elif r.department == "Women":
        want = ["Women", "Unisex Adults"]
    else:
        want = ["Unisex Adults", "Women"]
    hit = exact(a.values, *want)
    return Found(values=[hit or want[0]] if (hit or a.mode != "SELECTION_ONLY") else [])


def _size_type(view: ItemView, a: Aspect) -> Found:
    tab = (view.render.size_tab or "").strip().lower()
    want = [SIZE_TYPES[tab]] if tab in SIZE_TYPES else []
    hit = exact(a.values, *want, "Regular")
    return Found(values=[hit] if hit else ([want[0]] if want and a.mode != "SELECTION_ONLY" else []))


def _size(view: ItemView, a: Aspect, chosen: dict, closed: bool = True) -> Found:
    """The size among the values that go with the Size Type chosen — or, when the exact size stands only under
    another Size Type ("14W" under Plus), that pair: eBay wants a valid Size Type + Size combination. `closed`: the
    category is apparel or footwear, where eBay's size list is closed; elsewhere (a dog's coat under Pet Supplies) a
    size eBay's list doesn't hold goes as written where eBay takes text."""
    want = view.size_in().value
    cands = size_candidates(want, marked=True)
    offered = a.allowed(chosen)
    if cands and exact(offered, *cands) is None:
        for other in sorted({o for ties in a.constraints.values() for o in ties}):
            for kind in dict.fromkeys(k for ties in a.constraints.values() for k in ties.get(other, [])):
                if kind in (chosen.get(other) or []):
                    continue
                if (hit := exact(a.allowed({**chosen, other: [kind]}), *cands)) is not None and other in chosen:
                    was = chosen[other][0]
                    chosen[other] = [kind]
                    return Found(values=[hit], note=f"size {want}: eBay lists it under {other} '{kind}', not '{was}'")
    value, note = size_value(want, offered, view.facts.item_type)
    if value is None and not closed and a.mode != "SELECTION_ONLY" and (plain := size_candidates(want)):
        value = plain[0]
    return Found(values=[value] if value else [], note=note)


def _color(view: ItemView, a: Aspect) -> Found:
    colors = list(view.render.colors or [])
    words = str(view.facts.color_name or "")
    if len(colors) > 2 or re.search(r"\bmulti(?:colou?r(?:ed)?)?\b|\brainbow\b", words, re.I):
        hit = exact(a.values, "Multicolor", "Multi-Color", "Multi")
        return Found(values=[hit or "Multicolor"])
    out = []
    for c in colors[: MULTI_MAX if a.multi else 1]:
        hit = exact(a.values, *EBAY_COLORS.get(c, (c,)))
        if hit or a.mode != "SELECTION_ONLY":
            out.append(hit or EBAY_COLORS.get(c, (c,))[0])
    return Found(values=list(dict.fromkeys(out)))


def _material(view: ItemView, a: Aspect) -> Found:
    """From a read label only (invariant 1) — else denim on an item that visibly is denim (jeans, a denim jacket), else
    a material the approved TITLE itself names (the verifier let it stand: the listing says nothing new)."""
    from thrift_agent.brain import verify
    fibers = common._fibers(view)                        # the label's fibers, the largest share first
    if not fibers and verify.denim_item(view.facts):
        fibers = ["denim"]
    if not fibers:
        named = from_words(a.values, [*_tokens(view.render.title), BREAK], MULTI_MAX if a.multi else 1)
        return Found(values=named)
    out = []
    for fiber in fibers:
        hit = (from_words(a.values, _tokens(fiber)) or [None])[0] or exact(a.values, fiber, fiber.title())
        if hit is None and a.mode != "SELECTION_ONLY":
            hit = fiber.title()
        if hit and hit not in out:
            out.append(hit)
    return Found(values=out[: MULTI_MAX if a.multi else 1])


def _premium(view: ItemView, field: str) -> str | None:
    p = view.facts.premium
    ev = getattr(p, field, None) if p is not None else None
    return (ev.value or None) if ev is not None else None


def resolve(view: ItemView, a: Aspect, chosen: dict, aliases=None, closed: bool = True) -> Found:
    """One aspect from the item's facts — its own reader by name, else eBay's values the item's own words say."""
    name = a.name.strip().lower()
    if name == "brand":
        return _brand(view, a, aliases)
    if name == "department":
        return _department(view, a)
    if name == "size type":
        return _size_type(view, a)
    if name in SIZE_ASPECTS:
        return _size(view, a, chosen, closed)
    if name in COLOR_ASPECTS:
        return _color(view, a)
    if name in MATERIAL_ASPECTS:
        return _material(view, a)
    if name == "country/region of manufacture":
        made = _premium(view, "made_in")
        hit = exact(a.values, made) if made else None
        return Found(values=[hit or made] if made and (hit or a.mode != "SELECTION_ONLY") else [])
    if name == "vintage":
        return Found(values=[exact(a.values, "Yes") or "Yes"] if _premium(view, "vintage") else [])
    if name == "model":
        style = view.facts.style_name.value
        return Found(values=[style] if style and a.mode != "SELECTION_ONLY" else [])
    if name == "product line":
        line = _premium(view, "line")
        return Found(values=[exact(a.values, line) or line] if line and (exact(a.values, line) or
                                                                         a.mode != "SELECTION_ONLY") else [])
    if name in ("eu shoe size", "uk shoe size", "mpn", "upc", "california prop 65 warning", "personalize",
                "handmade", "signed", "unit quantity", "unit type", "item length", "item width", "item height"):
        return Found()                                    # never read from the photos: left out
    return Found(values=from_words(a.allowed(chosen), item_words(view), MULTI_MAX if a.multi else 1))


def _fallback(view: ItemView, a: Aspect, chosen: dict) -> Found:
    """A REQUIRED aspect the facts don't give: never invented — a neutral value of eBay's own list ("Unknown",
    "Other"), else, where eBay takes text, what the item plainly is (its Type or Style) or "Unknown"; a list with no
    neutral value gives the one sharing most words with the item, said in the check line."""
    name = a.name.strip().lower()
    offered = a.allowed(chosen)
    if len(offered) == 1:
        return Found(values=[offered[0]])                 # eBay's list has one value: the category's own
    if name in ("type", "style") and a.mode != "SELECTION_ONLY":
        sub = (view.render.subcategory or "").strip()
        text = sub if sub and sub.lower() not in ("none", "other") else view.facts.item_type.strip().title()
        return Found(values=[text[: a.max_length or 65]],
                     note=f"{a.name}: eBay's list has no match — '{text[: a.max_length or 65]}' written")
    if (hit := exact(offered, *NEUTRAL)) is not None:
        return Found(values=[hit], note=f"{a.name}: not on the photos — eBay requires it, '{hit}' taken")
    if a.mode != "SELECTION_ONLY":
        return Found(values=["Unknown"], note=f"{a.name}: not on the photos — eBay requires it, 'Unknown' written")
    if not offered:
        return Found()
    words = set(item_words(view))
    best = max(offered, key=lambda v: (len(set(_tokens(v)) & words), -offered.index(v)))
    return Found(values=[best], note=f"{a.name}: not on the photos — eBay requires one of its own values, '{best}' "
                                     f"taken: please check")


def aspects_for(view: ItemView, category: Category, aliases=None) -> tuple[dict[str, list[str]], list[str]]:
    """({aspect name: its values}, the notes) for a listing in `category`: every required aspect, the recommended ones
    the facts give, the optional ones only by their own reader. Size Type before Size (the size list depends on it)."""
    chosen: dict[str, list[str]] = {}
    notes: list[str] = []
    closed = any(_key(p) == _key(FASHION_ROOT) for p in category.path)     # eBay's closed size lists: fashion only
    order = sorted(category.aspects, key=lambda a: (a.name.strip().lower() not in ("size type", "department", "brand"),))
    for a in order:
        name = a.name.strip().lower()
        own = name in ("brand", "department", "size type", "country/region of manufacture", "vintage", "model",
                       "product line") or name in SIZE_ASPECTS + COLOR_ASPECTS + MATERIAL_ASPECTS
        if not a.required and a.usage != "RECOMMENDED" and not own:
            continue
        found = resolve(view, a, chosen, aliases, closed)
        if not found.values and a.required:
            if name in SIZE_ASPECTS:
                want = (view.size_in().value or "").strip()
                if not want:
                    raise MappingError(NO_SIZE)           # her size on the card puts every site back in line
                # a size is never made up (the owner, WO50): eBay's list for this category has nothing near it
                raise MappingError(f"size '{want}' isn't on eBay's size list for {category.name}")
            found = _fallback(view, a, chosen)
        values = [v[: a.max_length] if a.max_length else v for v in found.values if v]
        if not values:
            continue
        chosen[a.name] = values if a.multi else values[:1]
        if found.note:
            notes.append(found.note)
    return {a.name: chosen[a.name] for a in category.aspects if a.name in chosen}, notes


# ---------------------------------------------------------------- the listing

class Config(_Loose):
    """`marketplaces.ebay` as the mapping reads it."""
    tax_rate: float = fees.DEFAULT_TAX
    best_offer: bool = True
    handling_days: int = shipping.DEFAULT_HANDLING_DAYS
    max_photos: int = PHOTOS_MAX
    shipping_type: str = "calculated"
    shipping_flat: float | None = None
    returns_accepted: bool = True
    returns_days: int = 30
    returns_payer: str = "BUYER"
    city: str = ""               # the item's location is the seller's own data: private/settings.yaml
    state: str = ""
    postal_code: str = ""
    country: str = "US"

    @classmethod
    def from_settings(cls, s) -> Config:
        e = (s.get("marketplaces.ebay") or {}) if s is not None else {}
        ship, ret, loc = e.get("shipping") or {}, e.get("returns") or {}, e.get("location") or {}
        raw = {"tax_rate": e.get("tax_rate"), "best_offer": e.get("best_offer"), "handling_days": e.get("handling_days"),
               "max_photos": e.get("max_photos"), "shipping_type": ship.get("type"), "shipping_flat": ship.get("flat"),
               "returns_accepted": ret.get("accepted"), "returns_days": ret.get("days"),
               "returns_payer": str(ret.get("payer") or "").upper() or None, "city": loc.get("city"),
               "state": loc.get("state"), "postal_code": str(loc.get("postal_code")) if loc.get("postal_code") else None,
               "country": loc.get("country")}
        return cls(**{k: v for k, v in raw.items() if v is not None})


class EbayFields(BaseModel):
    """One eBay listing, mapped: what the API call carries and what the read-back is compared with."""
    title: str
    description: str             # plain text; the client sends it as simple HTML (`description_html`)
    price: int                   # the eBay price (fees.quote)
    base_price: int              # her price it was worked out from
    quote: dict[str, Any] = Field(default_factory=dict)
    category: str                # "Clothing, Shoes & Accessories > Women > Women's Clothing > Dresses"
    category_id: str
    category_ids: list[str] = Field(default_factory=list)     # root → leaf: the fee rule reads them
    condition: str               # eBay's name for it
    condition_id: str
    aspects: dict[str, list[str]] = Field(default_factory=dict)
    brand: str | None = None
    size: str | None = None
    colors: list[str] = Field(default_factory=list)
    photos: list[str] = Field(default_factory=list)
    shipping: dict[str, Any] = Field(default_factory=dict)
    returns: dict[str, Any] = Field(default_factory=dict)
    location: dict[str, Any] = Field(default_factory=dict)
    best_offer: bool = True
    sku: str = ""
    guesses: list[str] = Field(default_factory=list)


def description_html(text: str) -> str:
    """The description as the simple HTML eBay shows: paragraphs and line breaks, everything else escaped."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", str(text or "").replace("\r\n", "\n")) if b.strip()]
    return "".join(f"<p>{html.escape(b, quote=False).replace(chr(10), '<br>')}</p>" for b in blocks)


def _label_line(view: ItemView, description: str, size: str | None) -> str:
    """WO57 §4: the label's own size in the description when eBay's standard value differs from it."""
    printed = (view.facts.size_printed.value or "").strip()
    if not printed or not size or _key(printed) == _key(size) or _key(printed) in _key(description):
        return description
    if any(_key(c) == _key(size) for c in size_candidates(printed)):
        return description                                # the same size, eBay's spelling
    return f"{description.rstrip()}\n\nLabel size: {printed}."


def map_ebay(view: ItemView, category: Category, cfg: Config | None = None, aliases=None,
             fee_table: fees.Fees | None = None, ship_table: shipping.Table | None = None) -> EbayFields:
    """The item as an eBay listing in `category` (chosen by `choose_category` from eBay's suggestions): fixed price,
    Good 'Til Cancelled, one of it, SKU = our item id. Raises MappingError only for an item with no size at all in a
    category that needs one — every other gap takes the closest valid choice and says so."""
    cfg = cfg or Config()
    f, r = view.facts, view.render
    notes: list[str] = []
    aspects, more = aspects_for(view, category, aliases)
    notes += more
    cid, cname, cnote = condition_for(f.condition, category.conditions)
    if cnote:
        notes.append(cnote)
    family = families.family_of_type(f.item_type)
    plan = shipping.plan(family, common.package_class(view), r.department, kind=cfg.shipping_type,
                         handling_days=cfg.handling_days, flat=cfg.shipping_flat, table=ship_table)
    quote = fees.quote(view.price, category.ids, plan.buyer_pays, cfg.tax_rate, fee_table)
    size = next((aspects[a][0] for a in aspects if a.strip().lower() in SIZE_ASPECTS), None)
    title = r.title.strip()
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX].rsplit(" ", 1)[0]
    photos = common.photos_for(view, min(int(cfg.max_photos), PHOTOS_MAX))
    return EbayFields(
        title=title, description=_label_line(view, r.description, size), price=quote.price, base_price=int(view.price),
        quote={"rule": quote.rule, "exact": quote.exact, "shipping": quote.shipping, "tax_rate": quote.tax_rate,
               "poshmark_payout": quote.poshmark_payout, "ebay_payout": quote.ebay_payout,
               "checked": quote.checked.isoformat()},
        category=category.text, category_id=category.id, category_ids=list(category.ids), condition=cname,
        condition_id=cid, aspects=aspects, brand=(aspects.get("Brand") or [None])[0], size=size,
        colors=[v for a in aspects if a.strip().lower() in COLOR_ASPECTS for v in aspects[a]], photos=photos,
        shipping=plan.model_dump(),
        returns={"accepted": cfg.returns_accepted, "days": cfg.returns_days, "payer": cfg.returns_payer},
        location={"city": cfg.city, "state": cfg.state, "postal_code": cfg.postal_code, "country": cfg.country},
        best_offer=cfg.best_offer, sku=view.iid, guesses=notes)
