"""A fake eBay for the tests (WO57 §6): the Taxonomy, Metadata and Media calls and the Listing API (GraphQL), answered
in eBay's own shapes by an httpx.MockTransport — no network. It keeps listings in memory, refuses what eBay refuses
(a missing required item specific, a title over 80 characters, a condition the category doesn't take, no photo), keeps
a createListing by its idempotency key, and can be told to fail (`fail`).

THE CATEGORY TABLE BELOW IS A STAND-IN, written from eBay's documented answer shapes and the public category tree as
remembered — its ids, value lists and "required" marks are NOT recordings. On the day the keys arrive the spike records
eBay's real answers (`thrift ebay taxonomy --record`, into tests/fixtures/ebay/); a recorded file wins over the
stand-in for its category (`FakeEbay(recorded=…)`)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

CSA = ("11450", "Clothing, Shoes & Accessories")
WOMEN, WCLOTH, WSHOES = ("260010", "Women"), ("15724", "Women's Clothing"), ("3034", "Women's Shoes")
MEN, MCLOTH, MSHOES = ("260012", "Men"), ("1059", "Men's Clothing"), ("93427", "Men's Shoes")
KIDS, GIRLS, BOYS, BABY = ("171146", "Kids"), ("260015", "Girls"), ("260014", "Boys"), ("260018", "Baby")
PETS, DOGS = ("1281", "Pet Supplies"), ("20742", "Dog Supplies")
RECORDED = Path(__file__).parent / "fixtures" / "ebay"

APPAREL = [("1000", "New with tags"), ("1500", "New without tags"), ("1750", "New with defects"),
           ("2990", "Pre-owned - Excellent"), ("3000", "Pre-owned - Good"), ("3010", "Pre-owned - Fair")]
SHOES = [("1000", "New with box"), ("1500", "New without box"), ("1750", "New with defects"),
         ("2990", "Pre-owned - Excellent"), ("3000", "Pre-owned - Good"), ("3010", "Pre-owned - Fair")]
PLAIN = [("1000", "New"), ("1500", "New other (see details)"), ("3000", "Used")]

COLORS = ["Beige", "Black", "Blue", "Brown", "Gold", "Gray", "Green", "Ivory", "Multicolor", "Orange", "Pink", "Purple",
          "Red", "Silver", "White", "Yellow"]
BRANDS = ["J.Crew", "Nike", "Zara", "Levi's", "Free People", "Madewell", "Tory Burch", "Coach", "The North Face",
          "Birkenstock", "Carter's", "Alo Yoga", "Lululemon", "Ralph Lauren", "H&M", "Unbranded"]
LETTERS = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL"]
NUMBERS = ["00", "0", "2", "4", "6", "8", "10", "12", "14", "16", "18"]
W_SIZES = {"Regular": [*LETTERS, *NUMBERS, "One Size"], "Petite": ["XXS", "XS", "S", "M", "L", "XL", *NUMBERS[:9]],
           "Plus": ["XL", "1X", "2X", "3X", "4X", "14W", "16W", "18W", "20W", "22W", "24W"],
           "Tall": ["XS", "S", "M", "L", "XL", *NUMBERS[2:9]], "Maternity": ["XS", "S", "M", "L", "XL"],
           "Juniors": ["XS", "S", "M", "L", "XL", "0", "1", "3", "5", "7", "9", "11", "13"]}
W_WAISTS = {"Regular": ["23", "24", "25", "26", "27", "28", "29", "30", "31", "32", "33", "34", *NUMBERS, *LETTERS],
            "Petite": ["24", "25", "26", "27", "28", "29", "30", *NUMBERS[:8]], "Plus": ["14W", "16W", "18W", "20W"],
            "Tall": ["26", "27", "28", "29", "30", "31", "32"]}
M_SIZES = {"Regular": ["XS", "S", "M", "L", "XL", "2XL", "3XL", "14.5", "15", "15.5", "16", "16.5", "17"],
           "Big & Tall": ["LT", "XLT", "2XLT", "2XB", "3XB", "4XB"]}
HALVES = [f"{n}{h}" for n in range(4, 14) for h in ("", ".5")]
KID_SHOES = ["8", "8.5", "9", "9.5", "10", "10.5", "11", "11.5", "12", "12.5", "13", "13.5", "1", "1.5", "2", "2.5", "3",
             "3.5", "4", "4.5", "5", "5.5", "6", "6.5", "7"]
BABY_SIZES = ["Preemie", "Newborn", "0-3 Months", "3 Months", "3-6 Months", "6 Months", "6-9 Months", "9 Months",
              "6-12 Months", "12 Months", "12-18 Months", "18 Months", "18-24 Months", "24 Months", "2T", "3T", "4T",
              "5T"]
GIRL_SIZES = ["4", "5", "6", "6X", "7", "8", "10", "12", "14", "16", "XS", "S", "M", "L", "XL"]


def asp(name, values=(), required=False, mode="FREE_TEXT", multi=False, usage="RECOMMENDED", by=None, max_len=65):
    """One aspect; `by`: {"Size Type": {type: [values]}} — values tied to another aspect's (eBay's valueConstraints)."""
    return {"name": name, "values": list(values), "required": required, "mode": mode, "multi": multi, "usage": usage,
            "by": by or {}, "max": max_len}


def sized(table):
    return asp("Size", sorted({v for vs in table.values() for v in vs}, key=str), True, by={"Size Type": table})


def stype(table):
    return asp("Size Type", list(table), True, mode="SELECTION_ONLY")


def brand():
    return asp("Brand", BRANDS, True)


def dept(*values):
    return asp("Department", values, True, mode="SELECTION_ONLY")


def color(name="Color", required=True):
    return asp(name, COLORS, required)


def material(name="Material", required=False, values=("Cotton", "Polyester", "Wool", "Silk", "Linen", "Cashmere",
                                                        "Leather", "Denim", "Nylon", "Viscose", "Rayon", "Canvas",
                                                        "Suede", "Faux Leather")):
    return asp(name, values, required, multi=True)


EXTRA = [asp("Pattern", ["Solid", "Floral", "Striped", "Plaid", "Polka Dot", "Animal Print", "Geometric"]),
         asp("Features", ["Pockets", "Lined", "Stretch", "Zip", "Hooded", "Adjustable", "Pleated"], multi=True),
         asp("Vintage", ["Yes", "No"]), asp("Country/Region of Manufacture", ["Italy", "France", "Japan",
                                                                               "United States", "Portugal"]),
         asp("Theme", ["Classic", "Boho", "Preppy", "Sports", "Western"], usage="OPTIONAL"),
         asp("Model", [], usage="OPTIONAL"), asp("MPN", [], usage="OPTIONAL")]

CATEGORIES: dict[str, dict] = {}


def cat(cid, name, parents, keywords, aspects, conditions=APPAREL):
    CATEGORIES[cid] = {"id": cid, "name": name, "parents": list(parents), "keywords": keywords, "aspects": aspects,
                       "conditions": conditions}


WD = dept("Women", "Unisex Adults")
MD = dept("Men", "Unisex Adults")
cat("11555", "Shorts", [CSA, WOMEN, WCLOTH], ["shorts", "short", "biker", "bermuda"],
    [brand(), stype(W_WAISTS), sized(W_WAISTS), color(), WD,
     asp("Style", ["Athletic", "Bike", "Bermuda", "Cargo", "Chino", "Denim", "Running", "Biker"], True),
     asp("Rise", ["High", "Mid", "Low"]), material(), *EXTRA])
cat("63861", "Dresses", [CSA, WOMEN, WCLOTH], ["dress", "gown", "sundress"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD,
     asp("Dress Length", ["Short", "Midi", "Long", "Knee Length"], True),
     asp("Style", ["A-Line", "Wrap", "Shift", "Sheath", "Maxi", "Mini", "Bodycon", "Fit & Flare", "Shirt Dress",
                   "Slip Dress", "Sweater Dress", "Sundress"], True),
     asp("Sleeve Length", ["Sleeveless", "Short Sleeve", "Long Sleeve", "3/4 Sleeve"]),
     asp("Neckline", ["V-Neck", "Crew Neck", "Scoop Neck", "Halter", "Square Neck"]),
     asp("Occasion", ["Casual", "Party/Cocktail", "Formal", "Wedding", "Business"]), material(), *EXTRA])
cat("53159", "Tops", [CSA, WOMEN, WCLOTH], ["top", "blouse", "tee", "shirt", "tank", "cami", "bodysuit", "tunic"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD,
     asp("Type", ["Blouse", "T-Shirt", "Tank", "Tunic", "Bodysuit", "Button-Up", "Cami", "Crop Top", "Polo", "Henley"],
         True),
     asp("Sleeve Length", ["Sleeveless", "Short Sleeve", "Long Sleeve", "3/4 Sleeve"]),
     asp("Neckline", ["V-Neck", "Crew Neck", "Scoop Neck", "Collared", "Square Neck"]), material(), *EXTRA])
cat("63866", "Sweaters", [CSA, WOMEN, WCLOTH], ["sweater", "cardigan", "pullover", "jumper", "turtleneck"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD,
     asp("Type", ["Pullover", "Cardigan", "Sweater Vest", "Poncho", "Shrug"], True),
     asp("Style", ["Crewneck", "V-Neck", "Turtleneck", "Cable Knit", "Cropped"]), material(), *EXTRA])
cat("11554", "Jeans", [CSA, WOMEN, WCLOTH], ["jeans", "denim"],
    [brand(), stype(W_WAISTS), sized(W_WAISTS), color(), WD,
     asp("Style", ["Straight", "Skinny", "Bootcut", "Wide Leg", "Flare", "Boyfriend", "Mom", "Cropped"], True),
     asp("Rise", ["High", "Mid", "Low"]), asp("Wash", ["Light", "Medium", "Dark"]),
     asp("Inseam", [str(n) for n in range(24, 37)], usage="OPTIONAL"), material(), *EXTRA])
cat("63863", "Pants", [CSA, WOMEN, WCLOTH], ["pants", "trousers", "leggings", "joggers", "chinos", "sweatpants"],
    [brand(), stype(W_WAISTS), sized(W_WAISTS), color(), WD,
     asp("Style", ["Wide Leg", "Straight", "Leggings", "Joggers", "Cargo", "Dress Pants", "Capri", "Flare",
                   "Sweatpants"], True), asp("Rise", ["High", "Mid", "Low"]), material(), *EXTRA])
cat("63862", "Coats, Jackets & Vests", [CSA, WOMEN, WCLOTH],
    ["coat", "jacket", "blazer", "vest", "parka", "puffer", "trench", "bomber"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD,
     asp("Type", ["Jacket", "Coat", "Vest", "Blazer", "Parka", "Raincoat"], True),
     asp("Style", ["Puffer", "Bomber", "Trench", "Denim Jacket", "Moto", "Peacoat", "Quilted", "Windbreaker"], True),
     material("Outer Shell Material", True), asp("Lining Material", ["Polyester", "Silk", "Cotton"]), *EXTRA])
cat("63864", "Skirts", [CSA, WOMEN, WCLOTH], ["skirt", "skort"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD,
     asp("Style", ["A-Line", "Pencil", "Pleated", "Mini", "Maxi", "Wrap", "Tiered"], True),
     asp("Skirt Length", ["Short", "Midi", "Long", "Knee Length"], True), material(), *EXTRA])
cat("3009", "Jumpsuits & Rompers", [CSA, WOMEN, WCLOTH], ["jumpsuit", "romper", "overalls", "catsuit"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD, asp("Type", ["Jumpsuit", "Romper", "Overalls"], True),
     material(), *EXTRA])
cat("95672", "Athletic Shoes", [CSA, WOMEN, WSHOES], ["sneakers", "sneaker", "trainers", "running", "athletic"],
    [brand(), asp("US Shoe Size", HALVES, True), color(), WD,
     asp("Type", ["Athletic"], True), asp("Style", ["Sneaker", "Running", "Slip-On", "High Top"], True),
     material("Upper Material", True, ("Leather", "Canvas", "Mesh", "Suede", "Synthetic", "Textile")),
     asp("Performance/Activity", ["Running", "Walking", "Training", "Tennis"]), asp("Model", [], usage="OPTIONAL"),
     asp("EU Shoe Size", [], usage="OPTIONAL"), *EXTRA[:2]], SHOES)
cat("53557", "Boots", [CSA, WOMEN, WSHOES], ["boots", "boot", "booties", "bootie"],
    [brand(), asp("US Shoe Size", HALVES, True), color(), WD,
     asp("Style", ["Ankle", "Knee High", "Chelsea", "Combat", "Riding", "Western", "Rain Boot", "Winter Boot"], True),
     material("Upper Material", True, ("Leather", "Suede", "Rubber", "Synthetic", "Textile")),
     asp("Heel Height", ["Flat (Under 1 in)", "Low (1-2 in)", "Mid (2-3 in)", "High (3 in and Up)"]), *EXTRA[:2]], SHOES)
cat("45333", "Flats", [CSA, WOMEN, WSHOES], ["flats", "loafers", "loafer", "moccasins", "ballet"],
    [brand(), asp("US Shoe Size", HALVES, True), color(), WD,
     asp("Style", ["Ballet", "Loafer", "Moccasin", "Mule", "Slip-On", "Espadrille"], True),
     material("Upper Material", True, ("Leather", "Suede", "Canvas", "Synthetic", "Textile")), *EXTRA[:2]], SHOES)
cat("169291", "Women's Bags & Handbags", [CSA, WOMEN], ["bag", "handbag", "purse", "tote", "crossbody", "satchel",
                                                        "clutch", "backpack", "shoulder"],
    [brand(), color("Exterior Color"), material("Exterior Material", True, ("Leather", "Canvas", "Nylon", "Suede",
                                                                             "Faux Leather", "Straw", "Unknown")),
     asp("Style", ["Shoulder Bag", "Tote", "Crossbody", "Satchel", "Clutch", "Backpack", "Hobo", "Bucket Bag"], True),
     WD, asp("Bag Width", [], usage="OPTIONAL"), asp("Handle Style", ["Top Handle", "Shoulder Strap", "Chain"]),
     asp("Closure", ["Zip", "Magnetic Snap", "Flap", "Drawstring"]), *EXTRA[:1], *EXTRA[2:4]])
cat("45238", "Scarves & Wraps", [CSA, WOMEN, ("4251", "Women's Accessories")], ["scarf", "wrap", "shawl"],
    [brand(), color(), WD, asp("Type", ["Scarf", "Wrap", "Shawl", "Bandana"], True),
     asp("Size", ["One Size", "S", "M", "L"], usage="RECOMMENDED"), material(), *EXTRA[:1]])
cat("57990", "Casual Button-Down Shirts", [CSA, MEN, MCLOTH], ["shirt", "button", "flannel", "oxford"],
    [brand(), stype(M_SIZES), sized(M_SIZES), color(), MD,
     asp("Type", ["Button-Up", "Button-Down", "Flannel", "Western"], True),
     asp("Sleeve Length", ["Short Sleeve", "Long Sleeve"], True),
     asp("Fit", ["Regular", "Slim", "Relaxed", "Classic"]), material(), *EXTRA])
cat("57988", "Coats, Jackets & Vests", [CSA, MEN, MCLOTH], ["coat", "jacket", "blazer", "vest", "parka", "puffer"],
    [brand(), stype(M_SIZES), sized(M_SIZES), color(), MD,
     asp("Type", ["Jacket", "Coat", "Vest", "Blazer", "Parka"], True),
     asp("Style", ["Puffer", "Bomber", "Fleece Jacket", "Windbreaker", "Quilted"], True),
     material("Outer Shell Material", True), *EXTRA])
cat("15709", "Athletic Shoes", [CSA, MEN, MSHOES], ["sneakers", "sneaker", "trainers", "running", "athletic"],
    [brand(), asp("US Shoe Size", [f"{n}{h}" for n in range(6, 16) for h in ("", ".5")], True), color(), MD,
     asp("Type", ["Athletic"], True), asp("Style", ["Sneaker", "Running", "Slip-On", "High Top"], True),
     material("Upper Material", True, ("Leather", "Canvas", "Mesh", "Suede", "Synthetic", "Textile")),
     asp("Model", [], usage="OPTIONAL"), *EXTRA[:2]], SHOES)
cat("57974", "Girls' Shoes", [CSA, KIDS, GIRLS], ["shoes", "sneakers", "sandals", "boots", "sandal", "sneaker"],
    [brand(), asp("US Shoe Size", KID_SHOES, True), color(), dept("Girls", "Unisex Kids"),
     asp("Type", ["Sneaker", "Sandal", "Boot", "Flat", "Slipper"], True),
     material("Upper Material", False, ("Leather", "Canvas", "Synthetic", "Textile", "Suede")), *EXTRA[:2]], SHOES)
cat("57929", "Boys' Shoes", [CSA, KIDS, BOYS], ["shoes", "sneakers", "sandals", "boots", "sandal", "sneaker"],
    [brand(), asp("US Shoe Size", KID_SHOES, True), color(), dept("Boys", "Unisex Kids"),
     asp("Type", ["Sneaker", "Sandal", "Boot", "Slipper"], True), *EXTRA[:2]], SHOES)
cat("147324", "Outerwear", [CSA, BABY, ("3082", "Baby & Toddler Clothing")],
    ["snowsuit", "bunting", "jacket", "coat", "outerwear", "pram"],
    [brand(), asp("Size", BABY_SIZES, True), color(), dept("Baby", "Girls", "Boys", "Unisex Kids"),
     asp("Type", ["Snowsuit", "Jacket", "Coat", "Bunting", "Vest"], True),
     asp("Season", ["Winter", "Fall", "Spring"]), material(), *EXTRA[:2]])
cat("80913", "Kids' Costumes", [CSA, ("155240", "Specialty"), ("163147", "Costumes, Reenactment, Theater"),
                                 ("52762", "Costumes")], ["costume", "cosplay", "princess", "superhero"],
    [brand(), asp("Size", ["One Size", "XS", "S", "M", "L", "XL", "2T", "3T", "4T", "4", "5", "6", "7", "8", "10", "12",
                          "14", "16"], True), color(), dept("Girls", "Boys", "Unisex Kids"),
     asp("Theme", ["Princess", "Superhero", "Animal", "Fairy Tale", "Holiday"]), material(), *EXTRA[:2]])
cat("15720", "Dresses", [CSA, KIDS, GIRLS, ("11462", "Girls' Clothing (Sizes 4 & Up)")], ["dress", "gown"],
    [brand(), asp("Size", GIRL_SIZES, True), color(), dept("Girls"),
     asp("Style", ["A-Line", "Party", "Sundress", "Tutu", "Smocked"]), material(), *EXTRA[:2]])
KID = dept("Girls", "Boys", "Unisex Kids")
GCLOTH, BCLOTH = ("11462", "Girls' Clothing (Sizes 4 & Up)"), ("11452", "Boys' Clothing (Sizes 4 & Up)")
TODDLER = ("3082", "Baby & Toddler Clothing")
BOY_SIZES = ["4", "5", "6", "7", "8", "10", "12", "14", "16", "18", "20", "XS", "S", "M", "L", "XL"]
for cid, name, parents, words, sizes in (
        ("155199", "Tops, Shirts & T-Shirts", [CSA, KIDS, GIRLS, GCLOTH], ["top", "tee", "shirt", "blouse", "tank"], GIRL_SIZES),
        ("175521", "Tops, Shirts & T-Shirts", [CSA, KIDS, BOYS, BCLOTH], ["top", "tee", "shirt", "polo", "button"], BOY_SIZES),
        ("51581", "Sweaters", [CSA, KIDS, GIRLS, GCLOTH], ["sweater", "cardigan", "pullover"], GIRL_SIZES),
        ("51951", "Sweaters", [CSA, KIDS, BOYS, BCLOTH], ["sweater", "cardigan", "pullover"], BOY_SIZES),
        ("51933", "Pants", [CSA, KIDS, GIRLS, GCLOTH], ["pants", "leggings", "joggers", "jeans", "chinos"], GIRL_SIZES),
        ("51920", "Pants", [CSA, KIDS, BOYS, BCLOTH], ["pants", "joggers", "jeans", "chinos", "khakis"], BOY_SIZES),
        ("51580", "Outerwear", [CSA, KIDS, GIRLS, GCLOTH], ["jacket", "coat", "puffer", "vest", "parka"], GIRL_SIZES),
        ("51930", "Outerwear", [CSA, KIDS, BOYS, BCLOTH], ["jacket", "coat", "puffer", "vest", "parka"], BOY_SIZES),
        ("163222", "Tops & T-Shirts", [CSA, BABY, TODDLER], ["top", "tee", "shirt", "polo", "bodysuit"], BABY_SIZES),
        ("163223", "Sweaters", [CSA, BABY, TODDLER], ["sweater", "cardigan", "pullover"], BABY_SIZES),
        ("163224", "Bottoms", [CSA, BABY, TODDLER], ["pants", "leggings", "joggers", "jeans", "shorts"], BABY_SIZES)):
    cat(cid, name, parents, words, [brand(), asp("Size", sizes, True), color(), KID if BABY not in parents
                                    else dept("Baby", "Girls", "Boys", "Unisex Kids"),
                                    asp("Type", ["T-Shirt", "Polo", "Button-Up", "Pullover", "Cardigan", "Jacket", "Coat",
                                                 "Vest", "Leggings", "Joggers", "Jeans", "Chinos"]), material(), *EXTRA[:2]])
cat("177789", "Apparel", [PETS, DOGS], ["dog", "coat", "sweater", "harness", "jacket", "puppy"],
    [brand(), asp("Type", ["Coat/Jacket", "Sweater", "Shirt", "Raincoat", "Costume", "Hoodie"], True),
     asp("Size", ["XXS", "XS", "S", "M", "L", "XL", "2XL"], True), color(required=False),
     asp("Dog Breed", ["All Breeds", "Small Breeds", "Large Breeds"], usage="OPTIONAL"), material()], PLAIN)
cat("63853", "Panties", [CSA, WOMEN, WCLOTH, ("11514", "Intimates & Sleep")], ["panties", "underwear", "thong", "briefs"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(), WD, asp("Style", ["Thong", "Bikini", "Brief", "Boyshort"], True)],
    APPAREL[:3])                                           # eBay takes underwear only new
cat("314", "Other Women's Clothing", [CSA, WOMEN, WCLOTH], ["women"],
    [brand(), stype(W_SIZES), sized(W_SIZES), color(required=False), WD])


def suggestions_answer(q: str) -> dict:
    """getCategorySuggestions' answer for a query: the stand-in categories whose words the query names, best first —
    a category of the query's own department (women's / men's / girls / boys / baby) ahead of the others."""
    words = set(re.findall(r"[a-z]+", q.lower().replace("'s", "")))
    words |= {w[:-1] for w in words if w.endswith("s")}
    scored = []
    for order, c in enumerate(CATEGORIES.values()):
        hits = sum(1 for k in c["keywords"] if k in words or k.rstrip("s") in words)
        own = " ".join(n for _, n in c["parents"]).lower()
        dept_hit = sum(1 for w in ("women", "men", "girls", "boys", "baby", "dog") if w in words and
                       re.search(rf"\b{w}\b", own.replace("'s", "")))
        if hits:
            scored.append((-hits - dept_hit, order, c))
    out = []
    for _, _, c in sorted(scored)[:6]:
        ups = list(reversed(c["parents"]))
        out.append({"category": {"categoryId": c["id"], "categoryName": c["name"]},
                    "categoryTreeNodeLevel": len(ups) + 1,
                    "categoryTreeNodeAncestors": [{"categoryId": i, "categoryName": n,
                                                   "categoryTreeNodeLevel": len(ups) - k} for k, (i, n) in enumerate(ups)]})
    return {"categorySuggestions": out, "categoryTreeId": "0", "categoryTreeVersion": "stand-in"}


def aspects_answer(cid: str) -> dict:
    c = CATEGORIES.get(str(cid))
    if c is None:
        return {}
    out = []
    for a in c["aspects"]:
        values = []
        for v in a["values"]:
            entry = {"localizedValue": v}
            ties = [{"applicableForLocalizedAspectName": other, "applicableForLocalizedAspectValues":
                     [k for k, vs in table.items() if v in vs]} for other, table in a["by"].items()]
            if any(t["applicableForLocalizedAspectValues"] for t in ties):
                entry["valueConstraints"] = ties
            values.append(entry)
        out.append({"localizedAspectName": a["name"],
                    "aspectConstraint": {"aspectDataType": "STRING", "aspectRequired": a["required"],
                                         "aspectMode": a["mode"], "aspectUsage": a["usage"], "aspectMaxLength": a["max"],
                                         "itemToAspectCardinality": "MULTI" if a["multi"] else "SINGLE",
                                         "aspectEnabledForVariations": False, "aspectApplicableTo": ["PRODUCT"]},
                    "aspectValues": values})
    return {"aspects": out}


def conditions_answer(cid: str) -> dict:
    c = CATEGORIES.get(str(cid))
    if c is None:
        return {"itemConditionPolicies": []}
    return {"itemConditionPolicies": [{"categoryId": str(cid), "categoryTreeId": "0", "itemConditionRequired": True,
                                       "itemConditions": [{"conditionId": i, "conditionDescription": n}
                                                          for i, n in c["conditions"]]}]}


class FakeEbay:
    """eBay in memory. `fail[what]` — a list of things to answer the next calls of that kind with instead: an int (an
    HTTP status), "timeout", "lost" (the call is carried out, its answer never arrives), or a dict (the GraphQL body).
    `what`: suggest | aspects | conditions | image | create | validate | update | price | end | read | search."""

    def __init__(self, recorded: Path | None = RECORDED, seller_listings: list[dict] | None = None):
        self.recorded = recorded
        self.listings: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.images: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list] = {}
        self.next_id = 256000000001
        self.tokens_seen: set[str] = set()
        self.insertion_fee = "0.00"
        self.ground_code = "USPSGroundAdvantage"      # eBay's own code for it, as its service list gives it
        for extra in seller_listings or []:           # listings made by hand, before the agent (no SKU of ours)
            self._store(extra)

    # ---- plumbing
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    def count(self, what: str) -> int:
        return sum(1 for w, _ in self.calls if w == what)

    def _planned(self, what: str):
        queue = self.fail.get(what) or []
        return queue.pop(0) if queue else None

    @staticmethod
    def _json(status: int, body) -> httpx.Response:
        return httpx.Response(status, json=body)

    def _recorded(self, name: str) -> dict | None:
        if self.recorded is None:
            return None
        try:
            return json.loads((self.recorded / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- the routes
    def handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth == "Bearer expired":
            return self._json(401, {"errors": [{"errorId": 1001, "message": "Invalid access token"}]})
        self.tokens_seen.add(auth[7:])
        host, path = request.url.host, request.url.path
        if host.startswith("graphqlapi."):
            return self._graphql(request)
        if host.startswith("apim.") and path.endswith("/image/create_image_from_file"):
            return self._image(request)
        if path.endswith("/get_category_suggestions"):
            return self._rest("suggest", lambda: suggestions_answer(request.url.params.get("q", "")), request)
        if path.endswith("/get_item_aspects_for_category"):
            cid = request.url.params.get("category_id", "")
            return self._rest("aspects", lambda: self._recorded(f"aspects_{cid}.json") or aspects_answer(cid), request)
        if path.endswith("/get_item_condition_policies"):
            cid = re.sub(r"\D", "", request.url.params.get("filter", ""))
            return self._rest("conditions", lambda: self._recorded(f"conditions_{cid}.json") or conditions_answer(cid),
                              request)
        if path.endswith("/get_shipping_services"):
            return self._rest("services", lambda: {"shippingServices": [
                {"shippingService": "USPSPriority", "description": "USPS Priority Mail", "shippingCarrier": "USPS",
                 "validForSellingFlow": True, "internationalService": False},
                {"shippingService": self.ground_code, "description": "USPS Ground Advantage", "shippingCarrier": "USPS",
                 "validForSellingFlow": True, "internationalService": False}]}, request)
        return self._json(404, {"errors": [{"errorId": 2002, "message": "Resource not found"}]})

    def _rest(self, what: str, answer, request: httpx.Request) -> httpx.Response:
        self.calls.append((what, dict(request.url.params)))
        planned = self._planned(what)
        if planned == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(planned, int):
            return self._json(planned, {"errors": [{"errorId": planned, "message": "planned failure"}]})
        return self._json(200, answer())

    def _image(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(("image", {"bytes": len(request.content)}))
        planned = self._planned("image")
        if planned == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(planned, int):
            return self._json(planned, {"errors": [{"errorId": 190000, "message": "planned failure"}]})
        if b'name="image"' not in request.content:
            return self._json(400, {"errors": [{"errorId": 190205, "message": "The image file is required."}]})
        url = f"https://i.ebayimg.com/00/s/fake/z/{len(self.images) + 1:04d}/$_57.JPG"
        self.images.append(url)
        return httpx.Response(201, json={"imageUrl": url, "maxDimensionImageUrl": url.replace("$_57", "$_10"),
                                         "expirationDate": "2027-01-01T00:00:00.000Z"},
                              headers={"Location": f"https://apim.ebay.com/commerce/media/v1_beta/image/v1|{len(self.images)}|0"})

    # ---- GraphQL
    def _graphql(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, variables = body.get("query", ""), body.get("variables") or {}
        if re.search(r"\(\s*input:\s*\{", query):         # eBay takes input arguments through variables only
            return self._json(200, {"errors": [{"message": "inline input values are not supported",
                                                "extensions": {"code": 1, "category": "REQUEST"}}]})
        op = re.search(r"(?:mutation|query)\s+(\w+)", query).group(1)
        route = {"CreateListing": self._create, "UpdateListing": self._update, "EndListing": self._end,
                 "BulkUpdateInventory": self._price, "SellerListings": self._read, "ActiveListings": self._search,
                 "EndedListings": self._search}[op]
        what = {"CreateListing": "validate" if ((variables.get("input") or {}).get("options") or {}).get(
            "operationMode") == "VALIDATE" else "create", "UpdateListing": "update", "EndListing": "end",
                "BulkUpdateInventory": "price", "SellerListings": "read", "ActiveListings": "search",
                "EndedListings": "search"}[op]
        self.calls.append((what, variables))
        planned = self._planned(what)
        if planned == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(planned, int):
            return self._json(planned, {"errors": [{"errorId": planned, "message": "planned failure"}]})
        if isinstance(planned, dict):
            return self._json(200, planned)
        data = route(variables, op)
        if planned == "lost":                              # done on eBay's side; the answer never reaches us
            raise httpx.ReadTimeout("timed out", request=request)
        return self._json(200, {"data": data})

    @staticmethod
    def _violation(code: str, text: str, path: str) -> dict:
        return {"violationCode": code, "violationDescription": text, "inputPath": [path]}

    def check(self, inp: dict) -> list[dict]:
        """What eBay would refuse a createListing for."""
        out = []
        p = inp.get("product") or {}
        cid = str((p.get("categories") or {}).get("primaryCategoryId") or "")
        c = CATEGORIES.get(cid)
        if c is None:
            return [self._violation("LSTNG_INVALID_CATEGORY", f"Category {cid} is not a valid leaf category.",
                                    "product.categories.primaryCategoryId")]
        title = p.get("title") or ""
        if not title or len(title) > 80:
            out.append(self._violation("LSTNG_TITLE_LENGTH", "The title must be 1 to 80 characters.", "product.title"))
        if re.search(r"\breplica\b|\bfake\b", title, re.I):
            out.append(self._violation("LSTNG_POLICY_COUNTERFEIT", "This listing violates the counterfeit item policy "
                                       "and was not created.", "product.title"))
        if not p.get("imageUrls"):
            out.append(self._violation("LSTNG_MISSING_IMAGE", "At least one image is required.", "product.imageUrls"))
        if len(p.get("imageUrls") or []) > 24:
            out.append(self._violation("LSTNG_TOO_MANY_IMAGES", "At most 24 images.", "product.imageUrls"))
        given = {a["name"]: a["values"] for a in p.get("aspects") or []}
        for a in c["aspects"]:
            values = given.get(a["name"]) or []
            if a["required"] and not values:
                out.append(self._violation("LSTNG_MISSING_REQUIRED_ASPECT", f"The item specific {a['name']} is missing.",
                                           "product.aspects"))
            if values and a["mode"] == "SELECTION_ONLY" and any(v not in a["values"] for v in values):
                out.append(self._violation("LSTNG_INVALID_ASPECT_VALUE", f"{values[0]} is not a valid value for "
                                           f"{a['name']}.", "product.aspects"))
            if a["name"] in ("Size", "US Shoe Size") and values and a["values"] and values[0] not in a["values"]:
                out.append(self._violation("LSTNG_NON_STANDARD_SIZE", f"{values[0]} is not a standard size value for "
                                           "this category.", "product.aspects"))
        cond = str((p.get("itemCondition") or {}).get("conditionId") or "")
        if cond not in {i for i, _ in c["conditions"]}:
            out.append(self._violation("LSTNG_INVALID_CONDITION", f"Condition {cond} is not valid for this category.",
                                       "product.itemCondition.conditionId"))
        terms = inp.get("listingTerms") or {}
        item = ((inp.get("listingItems") or {}).get("items") or [{}])[0]
        price = ((item.get("itemTerms") or {}).get("pricingTerms") or {}).get("fixedPrice")
        try:
            if float(price) < 0.99:
                raise ValueError
        except (TypeError, ValueError):
            out.append(self._violation("LSTNG_INVALID_PRICE", "The price must be at least $0.99.",
                                       "listingItems.items.itemTerms.pricingTerms.fixedPrice"))
        if terms.get("listingFormat") != "FIXED_PRICE" or (terms.get("lifecycleTerms") or {}).get(
                "listingDurationInDays") != "GTC":
            out.append(self._violation("LSTNG_INVALID_DURATION", "Fixed price listings must be GTC.", "listingTerms"))
        if not (terms.get("itemLocation") or {}).get("postalCode"):
            out.append(self._violation("LSTNG_MISSING_LOCATION", "The item location is missing.",
                                       "listingTerms.itemLocation"))
        ship = ((terms.get("fulfillmentTerms") or {}).get("shippingTerms")) or {}
        if not ship.get("shippingServices"):
            out.append(self._violation("LSTNG_MISSING_SHIPPING", "A shipping service is required.",
                                       "listingTerms.fulfillmentTerms"))
        if not re.fullmatch(r"[0-9A-F]{32}", str(inp.get("idempotencyKey") or "")):
            out.append(self._violation("LSTNG_INVALID_IDEMPOTENCY_KEY", "The idempotency key must be 32 hexadecimal "
                                       "characters.", "idempotencyKey"))
        return out

    def _store(self, inp: dict, lid: str | None = None) -> dict:
        lid = lid or str(self.next_id)
        self.next_id += 1
        p, terms = inp.get("product") or {}, inp.get("listingTerms") or {}
        item = ((inp.get("listingItems") or {}).get("items") or [{}])[0]
        ship = ((terms.get("fulfillmentTerms") or {}).get("shippingTerms")) or {}
        ret = ((terms.get("returnTerms") or {}).get("domesticReturnTerms")) or {}
        cid = str((p.get("categories") or {}).get("primaryCategoryId") or "")
        listing = {
            "listingId": lid, "sku": inp.get("sku"),
            "items": [{"sku": item.get("sku") or inp.get("sku"), "itemTerms": {
                "pricingTerms": {"fixedPrice": {"original": {
                    "value": ((item.get("itemTerms") or {}).get("pricingTerms") or {}).get("fixedPrice"),
                    "currency": "USD"}}},
                "availability": {"quantityAvailable": 1, "quantitySold": 0}}}],
            "sellerProduct": {"title": p.get("title"), "description": p.get("description"),
                              "aspects": [dict(a) for a in p.get("aspects") or []],
                              "images": [{"ebayImageUrl": u, "order": n} for n, u in enumerate(p.get("imageUrls") or [])],
                              "itemCondition": {"itemConditionId": (p.get("itemCondition") or {}).get("conditionId"),
                                                "displayName": ""},
                              "categories": {"primaryCategory": {"categoryId": cid,
                                                                 "name": (CATEGORIES.get(cid) or {}).get("name", "")}}},
            "listingTerms": {
                "listingFormat": terms.get("listingFormat", "FIXED_PRICE"),
                "lifecycleTerms": {"listingStatus": "ACTIVE", "onHold": False, "createdAt": "2026-11-10T19:00:00.000Z",
                                   "actualEndAt": None, "endListingReason": None, "listingDurationInDays": "GTC"},
                "itemLocation": dict(terms.get("itemLocation") or {}),
                "bestOfferTerms": {"acceptBestOffer": bool((terms.get("bestOfferTerms") or {}).get("bestOfferEnabled"))},
                "listingReturnTerms": {"domesticReturnTerms": {
                    "returnAccepted": ret.get("returnAccepted"), "returnDuration": ret.get("returnDuration"),
                    "returnShipmentInvoicee": ret.get("returnShipmentInvoicee"),
                    "listingRefundMethod": ret.get("refundMethod")}},
                "listingFulfillmentTerms": {"shippingTerms": {
                    "handlingTimeDuration": ship.get("handlingTimeDuration"),
                    "marketplaceShippingOptions": {"shippingCostType": (ship.get("marketplaceShippingOptions") or {}).get(
                        "costType")},
                    "shippingServices": [{"rank": s.get("rank"), "shippingCostAmount": {"original": {
                        "value": s.get("shippingCost") or "0.00", "currency": "USD"}},
                        "shippingService": {"shippingServiceCode": s.get("shippingServiceCode"), "displayName": ""}}
                        for s in ship.get("shippingServices") or []]}}},
            "listingOnHoldPolicies": None,
        }
        self.listings[lid] = listing
        return listing

    def _create(self, variables: dict, op: str) -> dict:
        inp = variables.get("input") or {}
        validate = (inp.get("options") or {}).get("operationMode") == "VALIDATE"
        key = str(inp.get("idempotencyKey") or "")
        fees = [{"listingFeeType": "INSERTION_FEE", "feeAmount": {"value": self.insertion_fee, "currency": "USD"}}]
        if not validate and key in self.by_key:           # the same call again: the same listing, never a second one
            return {"createListing": {"__typename": "CreateListingSuccess", "listing": self.listings[self.by_key[key]],
                                      "listingFees": fees, "warnings": []}}
        if violations := self.check(inp):
            return {"createListing": {"__typename": "CreateListingValidationError",
                                      "errorCode": "LSTNG_CREATE_VALIDATION_ERROR",
                                      "errorMessage": "The listing was not created.", "violations": violations,
                                      "warnings": []}}
        if validate:
            return {"createListing": {"__typename": "CreateListingValidationSuccess", "listing": {"listingId": "0"},
                                      "listingFees": fees, "warnings": []}}
        listing = self._store(inp)
        self.by_key[key] = listing["listingId"]
        return {"createListing": {"__typename": "CreateListingSuccess", "listing": listing, "listingFees": fees,
                                  "warnings": []}}

    def _mine(self, ident: dict) -> dict | None:
        if ident.get("listingId"):
            return self.listings.get(str(ident["listingId"]))
        return next((x for x in self.listings.values() if x.get("sku") == ident.get("sku") and
                     x["listingTerms"]["lifecycleTerms"]["listingStatus"] == "ACTIVE"), None)

    def _gone(self, typename: str, ident: dict, why: str = "The listing was not found or has ended.") -> dict:
        return {"__typename": typename, "errorCode": "LSTNG_NOT_FOUND", "errorMessage": why,
                "listingIdentifier": {"listingId": ident.get("listingId"), "sku": ident.get("sku")},
                "violations": [self._violation("LSTNG_NOT_ACTIVE", why, "listingIdentifier")], "warnings": []}

    def _update(self, variables: dict, op: str) -> dict:
        inp = variables.get("input") or {}
        listing = self._mine(inp.get("listingIdentifier") or {})
        if listing is None or listing["listingTerms"]["lifecycleTerms"]["listingStatus"] != "ACTIVE":
            return {"updateListing": self._gone("UpdateListingValidationError", inp.get("listingIdentifier") or {})}
        p = inp.get("product") or {}
        if "title" in p and (not p["title"] or len(p["title"]) > 80):
            return {"updateListing": {"__typename": "UpdateListingValidationError", "errorCode": "LSTNG_UPDATE",
                                      "errorMessage": "not updated", "listingIdentifier": {},
                                      "violations": [self._violation("LSTNG_TITLE_LENGTH", "The title must be 1 to 80 "
                                                                     "characters.", "product.title")], "warnings": []}}
        if (inp.get("options") or {}).get("operationMode") == "VALIDATE":
            return {"updateListing": {"__typename": "UpdateListingValidationSuccess",
                                      "listing": {"listingId": listing["listingId"]}, "warnings": []}}
        sp = listing["sellerProduct"]
        for k in ("title", "description"):
            if k in p:
                sp[k] = p[k]
        if "imageUrls" in p:
            sp["images"] = [{"ebayImageUrl": u, "order": n} for n, u in enumerate(p["imageUrls"])]
        if "aspects" in p:
            sp["aspects"] = [dict(a) for a in p["aspects"]]
        if "categories" in p:
            sp["categories"] = {"primaryCategory": {"categoryId": str(p["categories"]["primaryCategoryId"]), "name": ""}}
        return {"updateListing": {"__typename": "UpdateListingSuccess", "listing": listing, "warnings": []}}

    def _price(self, variables: dict, op: str) -> dict:
        entry = ((variables.get("input") or {}).get("inventory") or [{}])[0]
        ident = entry.get("listingItemIdentifier") or {}
        listing = self._mine(ident)
        if listing is None or listing["listingTerms"]["lifecycleTerms"]["listingStatus"] != "ACTIVE":
            return {"bulkUpdateInventory": {"__typename": "BulkUpdateInventoryFailure",
                                            "errorCode": "LSTNG_BULK_UPDATE_INVENTORY_FAILURE", "errorMessage": "failed",
                                            "failures": [{"listingItemIdentifier": ident, "violations": [
                                                self._violation("LSTNG_NOT_ACTIVE", "The listing is not active.",
                                                                "listingItemIdentifier")]}]}}
        listing["items"][0]["itemTerms"]["pricingTerms"]["fixedPrice"]["original"]["value"] = entry.get("fixedPrice")
        return {"bulkUpdateInventory": {"__typename": "BulkUpdateInventorySuccess",
                                        "successes": [{"listingItemIdentifier": {"listingId": listing["listingId"],
                                                                                 "sku": listing.get("sku")},
                                                       "warnings": []}]}}

    def _end(self, variables: dict, op: str) -> dict:
        inp = variables.get("input") or {}
        listing = self._mine(inp.get("listingIdentifier") or {})
        if listing is None:
            return {"endListing": self._gone("EndListingValidationError", inp.get("listingIdentifier") or {})}
        life = listing["listingTerms"]["lifecycleTerms"]
        if life["listingStatus"] != "ACTIVE":
            return {"endListing": self._gone("EndListingValidationError", inp.get("listingIdentifier") or {},
                                             "The listing has already ended.")}
        life.update(listingStatus="ENDED", actualEndAt="2026-11-10T20:00:00.000Z",
                    endListingReason=inp.get("endListingReason"))
        return {"endListing": {"__typename": "EndListingSuccess", "listing": listing, "warnings": []}}

    def _read(self, variables: dict, op: str) -> dict:
        asked = [str(x.get("listingId")) for x in ((variables.get("input") or {}).get("listings") or [])]
        if len(asked) > 20:
            return {"sellerListings": {"__typename": "ListingIdsMaxLimitError", "errorCode": "MAX",
                                       "errorMessage": "too many listing ids", "maxAllowed": 20}}
        return {"sellerListings": {"__typename": "ListingsSuccess", "listings": [
            {"__typename": "ListingSuccess", "listingId": i, "listing": self.listings.get(i)} for i in asked]}}

    def _search(self, variables: dict, op: str) -> dict:
        search = variables.get("search") or {}
        want = "ACTIVE" if op == "ActiveListings" else "ENDED"
        skus = (search.get("filters") or {}).get("skus")
        rows = [x for x in self.listings.values() if x["listingTerms"]["lifecycleTerms"]["listingStatus"] == want
                and (not skus or x.get("sku") in skus)]
        page = search.get("pagination") or {}
        size = int(page.get("maxPageSize") or 25)
        start = int(page.get("pageCursor") or 0)
        part = rows[start:start + size]
        nxt = str(start + size) if start + size < len(rows) else None
        field = "activeListings" if op == "ActiveListings" else "endedListings"
        return {"sellerListingSearch": {field: {"__typename": "SellerListings", "listings": [
            {"listingId": x["listingId"], "listing": x} for x in part],
            "pagination": {"nextCursor": nxt, "totalElements": len(rows), "totalPages": -(-len(rows) // size) or 1}}}}

    # ---- what a test does to eBay by hand
    def sell(self, lid: str) -> None:
        """A buyer bought it: the listing ends with one sold."""
        x = self.listings[lid]
        x["items"][0]["itemTerms"]["availability"] = {"quantityAvailable": 0, "quantitySold": 1}
        x["listingTerms"]["lifecycleTerms"].update(listingStatus="ENDED", actualEndAt="2026-11-11T15:00:00.000Z")

    def remove(self, lid: str, why: str = "Counterfeit item policy") -> None:
        """eBay itself took it down (a policy): ended and on hold."""
        x = self.listings[lid]
        x["listingTerms"]["lifecycleTerms"].update(listingStatus="ENDED", onHold=True)
        x["listingOnHoldPolicies"] = {"onHoldPolicies": [{"policyName": why, "policyViolationMessage":
                                                          f"The listing was removed: {why}."}]}

    def hand_edit(self, lid: str, *, price: str | None = None, title: str | None = None) -> None:
        """The owner changed it in eBay's own app."""
        x = self.listings[lid]
        if price is not None:
            x["items"][0]["itemTerms"]["pricingTerms"]["fixedPrice"]["original"]["value"] = price
        if title is not None:
            x["sellerProduct"]["title"] = title


def hand_listing(title: str, price: str = "25.00") -> dict:
    """A listing the owner made by hand before the agent: no SKU of ours."""
    return {"sku": None, "product": {"title": title, "description": "<p>By hand.</p>",
                                     "categories": {"primaryCategoryId": "53159"},
                                     "itemCondition": {"conditionId": "3000"}, "aspects": [],
                                     "imageUrls": ["https://i.ebayimg.com/hand.jpg"]},
            "listingItems": {"items": [{"itemTerms": {"pricingTerms": {"fixedPrice": price}}}]},
            "listingTerms": {"listingFormat": "FIXED_PRICE", "itemLocation": {"country": "US", "postalCode": "27401"}}}
