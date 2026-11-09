"""What kind of thing an item is — its family (WO51 §5): a dress, a top, a sweater, a pair of shoes… read from the
item's own type words (`facts.item_type`, "crochet-knit mini dress") or from a site's category ("Women > Clothing >
Dresses > Mini-dresses"), so a listing's category can be checked against the item it shows: a dress is a dress on all
three sites (live 2026-11-01: the Edikted crochet mini dress went to Poshmark's Sweaters › V-Necks and Vinted's
"V-neck sweaters"). Pure functions, no I/O.

    family_of_type("crochet-knit mini dress")                                  → "dress"
    family_of_category("poshmark", ("Women", "Sweaters", "V-Necks"))           → "sweater"
    family_of_category("vinted", "Women > Clothing > Dresses > Mini-dresses")  → "dress"
    same_family("dress", "sweater")                                            → False

How a phrase is read: its words (and a few two-word names: "tank top", "sports bra", "dressing gown", "2-piece") are
looked up in one vocabulary. A costume, sleepwear or swim word decides at once ("Belle princess costume dress" is a
costume, "gingham pajama set" sleepwear, "bikini top" swim); a set word makes a set unless the other words are a
bag, shoes or accessory ("jewelry set"); otherwise English puts the head noun last ("dress shirt" is a top, "shirt
dress" a dress, "boot cut" no boot). Weak words — tie, belt, hat, scarf, gloves — count only when nothing else does
("mini dress v-neck flower tie" stays a dress). A category is read from its deepest level that says something
("Sweaters › V-Necks" → the parent's "sweater"); a level is a list, read by its first part that names a family
("Pants & Jumpsuits" → pants); a swim, sleepwear or costume parent wins over a garment below it ("Swim › Board
Shorts", "Lingerie & nightwear › Dressing gowns"). Unknown is None, and None agrees with everything: the check holds
a listing back only when both sides are known and far apart."""
from __future__ import annotations

import re

FAMILIES = ("dress", "skirt", "bottoms", "shorts", "top", "sweater", "outerwear", "set", "sleepwear", "swim",
            "intimates", "jumpsuit", "shoes", "bag", "accessory", "costume", "pet")

# Neighbours that are no error side by side (both orders): a set goes under its bottom (WO27) or a matching-set
# category; a bralette, bodysuit or cami is a top as much as an intimate; a hoodie is a sweater on Vinted and a top on
# Poshmark; a cardigan or a sweater vest sits near outerwear; overalls are bottoms on Poshmark's Jeans; a skort is a
# skirt or shorts; a costume dress may sit in Dresses; a wallet is a bag or an accessory. Far apart (never paired):
# a dress and a set, a top or a sweater (the WO24 / WO51 misses), shoes or a bag and any garment.
COMPATIBLE = frozenset(frozenset(p) for p in (
    ("set", "bottoms"), ("set", "skirt"), ("set", "shorts"), ("set", "top"), ("set", "sweater"), ("set", "outerwear"),
    ("set", "sleepwear"), ("set", "swim"), ("set", "jumpsuit"), ("set", "intimates"),
    ("intimates", "sleepwear"), ("intimates", "top"), ("intimates", "accessory"),
    ("sweater", "top"), ("sweater", "outerwear"),
    ("bottoms", "shorts"), ("bottoms", "jumpsuit"), ("skirt", "shorts"),
    ("costume", "dress"), ("bag", "accessory"),
    # WO54 §1.4 (live 2026-11-09, i_261109_de26f1 "quilted dog coat": its "coat" made it outerwear, Women › Accessories
    # refused on Depop and Vinted, live nowhere): a pet's coat, collar or bed is a pet item — none of the three sites
    # has a pet category in our catalogs, and Accessories / Other is the closest place for one
    ("pet", "accessory"),
    # WO51 audit (live): lounge / pajama pants, sleep shorts, sleep tees and nightdresses sit in Sleepwear rightly
    ("sleepwear", "bottoms"), ("sleepwear", "shorts"), ("sleepwear", "top"), ("sleepwear", "dress"),
))

_WORDS: dict[str, tuple[str, ...]] = {
    "dress": ("dress", "gown", "sundress", "minidress", "maxidress", "frock", "kaftan", "caftan"),
    "skirt": ("skirt", "miniskirt", "skort"),
    "bottoms": ("pants", "pant", "trousers", "jeans", "leggings", "legging", "jeggings", "treggings", "joggers",
                "jogger", "sweatpants", "trackpants", "chinos", "chino", "khakis", "slacks", "culottes", "capris",
                "capri", "cargos", "bottoms", "flares", "palazzos"),
    "shorts": ("shorts", "bermudas"),
    "top": ("top", "tee", "tshirt", "shirt", "blouse", "cami", "camisole", "tank", "bodysuit", "tunic", "polo",
            "henley", "corset", "bustier", "halter", "jersey", "overshirt", "tops"),
    "sweater": ("sweater", "cardigan", "jumper", "hoodie", "sweatshirt", "pullover", "turtleneck", "crewneck",
                "shrug", "bolero", "knitwear"),
    "outerwear": ("jacket", "coat", "vest", "blazer", "puffer", "parka", "anorak", "windbreaker", "raincoat",
                  "trench", "bomber", "shacket", "gilet", "peacoat", "overcoat", "cape", "poncho", "outerwear"),
    "set": ("set", "coord", "twinset", "tracksuit", "pantsuit", "suits", "tuxedo"),   # "suit jacket": no suit
    "sleepwear": ("pajama", "pajamas", "pyjama", "pyjamas", "pj", "pjs", "nightgown", "nightie", "nightshirt",
                  "nightdress", "sleepwear", "nightwear", "robe", "bathrobe", "sleep"),
    "swim": ("swim", "swimsuit", "swimwear", "bikini", "tankini", "monokini", "coverup", "rashguard", "trunks",
             "sarong", "beachwear"),
    "intimates": ("bra", "bralette", "underwear", "panties", "panty", "thong", "briefs", "boxers", "lingerie",
                  "shapewear", "slip", "chemise", "teddy", "bandeau", "undershirt", "socks", "tights", "hosiery",
                  "stockings", "intimates"),
    "jumpsuit": ("jumpsuit", "romper", "playsuit", "overalls", "overall", "dungarees", "catsuit", "unitard",
                 "shortall", "onesie", "footie"),
    "shoes": ("shoes", "shoe", "sneakers", "sneaker", "boots", "boot", "booties", "bootie", "sandals", "sandal",
              "heels", "pumps", "loafers", "loafer", "flats", "mules", "mule", "clogs", "clog", "slippers", "slipper",
              "slides", "espadrilles", "espadrille", "oxfords", "oxford", "derby", "brogues", "brogue", "moccasins",
              "moccasin", "wedges", "wedge", "trainers", "trainer", "cleats", "platforms", "footwear", "flops"),
    "bag": ("bag", "handbag", "purse", "tote", "clutch", "backpack", "crossbody", "satchel", "wallet", "wristlet",
            "pouch", "duffel", "duffle", "hobo", "briefcase", "bags"),
    "accessory": ("belt", "scarf", "scarves", "hat", "cap", "beanie", "gloves", "glove", "mittens", "sunglasses",
                  "glasses", "jewelry", "jewellery", "necklace", "bracelet", "earring", "earrings", "ring", "watch",
                  "headband", "tie", "suspenders", "brooch", "accessories", "accessory", "bandana", "clip",
                  "scrunchie", "barrette"),
    "costume": ("costume", "cosplay"),
}
VOCAB = {w: fam for fam, words in _WORDS.items() for w in words}
# Two-word names, read before the single words (their second word's place is theirs).
BIGRAMS = {("tank", "top"): "top", ("t", "shirt"): "top", ("tee", "shirt"): "top", ("button", "down"): "top",
           ("crop", "top"): "top", ("tube", "top"): "top",
           ("sweater", "vest"): "sweater", ("sweater", "vests"): "sweater",
           ("sports", "bra"): "intimates", ("sports", "bras"): "intimates",
           ("cover", "up"): "swim", ("cover", "ups"): "swim", ("rash", "guard"): "swim", ("rash", "guards"): "swim",
           ("swim", "suit"): "swim", ("bathing", "suit"): "swim", ("board", "shorts"): "swim",
           ("dressing", "gown"): "sleepwear", ("dressing", "gowns"): "sleepwear", ("night", "gown"): "sleepwear",
           ("co", "ord"): "set", ("co", "ords"): "set", ("two", "piece"): "set", ("three", "piece"): "set",
           ("2", "piece"): "set", ("3", "piece"): "set", ("jump", "suit"): "jumpsuit",
           ("one", "piece"): "jumpsuit", ("one", "pieces"): "jumpsuit",     # a baby's; under Swim, swim (the parent)
           ("flip", "flop"): "shoes", ("flip", "flops"): "shoes",
           ("costume", "jewelry"): "accessory", ("costume", "jewellery"): "accessory"}
# The first word of these pairs isn't the thing ("boot cut" jeans, "slip-on" sneakers, "tie-dye", a "top-handle" bag).
NOT_A_THING = {("boot", "cut"), ("slip", "on"), ("slip", "ons"), ("tie", "dye"), ("top", "handle"), ("bag", "style")}
OVERRIDES = ("costume", "sleepwear", "swim")          # decide at once, in this order
GOODS = {"bag", "shoes", "accessory"}                 # with a set word: "jewelry set" is jewelry, never a set
WEAK = {"tie", "belt", "scarf", "scarves", "hat", "cap", "gloves", "glove", "bandana"}   # only when nothing else
GARMENTS = {"dress", "top", "bottoms", "shorts", "set", "jumpsuit", "skirt"}   # what a swim / sleep / costume parent
#                                                                               turns into its own ("Swim › Board Shorts")
NOT_CLOTHING = {"home", "pets", "electronics", "entertainment", "books", "hobbies", "makeup", "skincare", "hair",
                "bath & body", "grooming", "toys", "bath, skin & hair", "beauty", "everything else"}
# WO54 §1.4: a pet item — a pet word naming who wears or uses the thing ("dog coat", "cat bed", "coat for dogs"), never
# a print or a slogan on a garment ("dog print dress", "cat lover tee", "dog mom sweatshirt")
PETS = {"dog", "dogs", "puppy", "puppies", "doggie", "doggy", "cat", "cats", "kitten", "kittens", "kitty", "pet",
        "pets"}
PET_GOODS = {"collar", "leash", "harness", "bed", "bowl", "carrier", "crate", "kennel", "toy", "toys", "muzzle",
             "jacket", "coat", "sweater", "hoodie", "vest", "raincoat", "bandana", "costume", "boots", "shoes", "dress",
             "shirt", "tee", "pajamas", "life", "cooling", "clothes", "clothing", "apparel", "outfit", "tag"}
NOT_PET = {"print", "printed", "prints", "pattern", "patterned", "graphic", "motif", "lover", "lovers", "mom", "mama",
           "dad", "love", "face", "faces", "head", "embroidered", "applique", "appliqué", "theme", "themed", "lady",
           "eye", "ear", "ears", "paw", "paws", "tooth", "lady", "walker", "sitter"}


def is_pet_item(item_type: str | None) -> bool:
    """WO54 §1.4: "quilted dog coat", "cat bed", "harness for dogs" — a pet's own thing; "dog print dress" isn't."""
    toks = _tokens(_main_phrase(item_type or ""))
    for i, t in enumerate(toks):
        if t not in PETS:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt in NOT_PET:
            continue
        if nxt is None:                                  # "… for dogs" / "a coat for a dog"
            if i > 0 and toks[i - 1] in ("for", "a", "small", "large", "medium") and any(w in PET_GOODS for w in toks):
                return True
            continue
        if nxt in PET_GOODS or (_word(nxt) is not None and nxt not in NOT_PET):
            return True
    return False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower().replace("’", "").replace("'", ""))


def _word(tok: str) -> str | None:
    """A token's family: as written, else without a plural "s" / "es" ("dresses", "tees", "purses", "brooches")."""
    for t in (tok, tok[:-1] if tok.endswith("s") else None, tok[:-2] if tok.endswith("es") else None):
        if t and t in VOCAB:
            return VOCAB[t]
    return None


def _hits(tokens: list[str]) -> list[tuple[int, str, str]]:
    """(place, family, word) for every name in the words, two-word names first."""
    out, used = [], set()
    for i in range(len(tokens) - 1):
        pair = (tokens[i], tokens[i + 1])
        if pair in BIGRAMS and i not in used and i + 1 not in used:
            out.append((i + 1, BIGRAMS[pair], " ".join(pair)))
            used |= {i, i + 1}
        elif pair in NOT_A_THING:
            used.add(i)
    for i, tok in enumerate(tokens):
        if i not in used and (fam := _word(tok)):
            out.append((i, fam, tok))
    return sorted(out)


def _decide(hits: list[tuple[int, str, str]]) -> str | None:
    if not hits:
        return None
    families = {fam for _, fam, _ in hits}
    for fam in OVERRIDES:
        if fam in families:
            return fam
    if "set" in families:
        rest = [h for h in hits if h[1] != "set"]
        if not any(fam in GOODS for _, fam, _ in rest):
            return "set"
        hits = rest
    strong = [h for h in hits if h[2] not in WEAK]
    return (strong or hits)[-1][1]


def _main_phrase(text: str) -> str:
    """The item itself, before its details: "mini dress with flower tie" → "mini dress"."""
    return re.split(r",|\(|;| with | w/ | - | featuring | including ", f" {str(text or '').lower()} ", maxsplit=1)[0]


def family_of_type(item_type: str | None) -> str | None:
    """The family of an item type phrase ("crochet-knit mini dress" → "dress", "gingham pajama set, cami top and
    shorts" → "sleepwear", "Belle princess costume dress with tiara and gloves" → "costume"), or None when no word
    of it names a garment, shoes, a bag or an accessory."""
    if not item_type:
        return None
    if is_pet_item(item_type):
        return "pet"                                     # WO54 §1.4: a dog's coat is no woman's coat
    return _decide(_hits(_tokens(_main_phrase(item_type)))) or _decide(_hits(_tokens(item_type)))


def _levels(category) -> list[str]:
    if category is None:
        return []
    if isinstance(category, (list, tuple)):
        return [str(x).strip() for x in category if x is not None and str(x).strip()]
    return [p.strip() for p in re.split(r"\s*(?:>|›|/)\s*", str(category)) if p.strip()]


def _level_family(level: str) -> str | None:
    """One level of a category: a swim / sleepwear / costume word anywhere in it decides ("Lingerie & nightwear",
    "Swim briefs and shorts"); else its first part that names a family — a level is a list ("Pants & Jumpsuits" →
    pants, "Shorts & cropped pants" → shorts, "Combat & Moto Boots" → boots)."""
    low = level.strip().lower()
    if low == "dress":                      # Men › Pants › Dress: dress pants — the word alone is a modifier there
        return None
    families = {fam for _, fam, _ in _hits(_tokens(low))}
    for fam in OVERRIDES:
        if fam in families:
            return fam
    for part in re.split(r"\s*(?:&|,|\band\b)\s*", low):
        if fam := _decide(_hits(_tokens(part))):
            return fam
    return None


def family_of_category(site: str, category) -> str | None:
    """The family of a site's category: Poshmark's (department, category, subcategory) or a path ("Women > Dresses >
    Mini"), Vinted's category path ("Women > Clothing > Dresses > Mini-dresses"), Depop's ("Women > Tops > Sweaters").
    None for a category that names no family ("Other", "Clothing bundles") or isn't clothing, shoes, bags or
    accessories (Home, Makeup, Toys). `site` is kept for site-specific readings; the three read alike today."""
    levels = _levels(category)
    if not levels or any(lv.lower() in NOT_CLOTHING for lv in levels):
        return None
    found = [_level_family(lv) for lv in levels]
    deepest = next((i for i in range(len(found) - 1, -1, -1) if found[i]), None)
    if deepest is None:
        return None
    fam = found[deepest]
    for parent in found[:deepest]:
        if parent in OVERRIDES and (fam in GARMENTS or fam is None):
            return parent
    return fam


def same_family(a: str | None, b: str | None) -> bool:
    """True when the two families agree: the same, neighbours (COMPATIBLE), or either unknown (None)."""
    if not a or not b or a == b:
        return True
    return frozenset((a, b)) in COMPATIBLE


def closest(site: str, family: str | None, options: dict, words: str = "") -> str | None:
    """WO54 §1.4 (the owner: never leave an item unposted): the option whose category is in `family` and fits the
    item best — {key: category path} the site offers (the department's own) → the key with the most of the item's words
    in its path, then the broadest (a group's own "Other"), else None when the site has none of that family."""
    if not family:
        return None
    want = {w for w in _tokens(words) if len(w) > 2}
    best, best_score = None, None
    for key, path in options.items():
        if family_of_category(site, path) != family:
            continue
        levels = _levels(path)
        toks = set(_tokens(" ".join(levels)))
        words_in = sum(1 for w in want if w in toks or (w.endswith("s") and w[:-1] in toks) or f"{w}s" in toks)
        other = 1 if levels and levels[-1].lower().startswith("other") else 0
        score = (words_in, other, -len(levels), str(path))
        if best_score is None or score[:3] > best_score[:3] or (score[:3] == best_score[:3] and score[3] < best_score[3]):
            best, best_score = key, score
    return best
