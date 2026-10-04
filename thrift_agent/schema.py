"""Data contracts. Facts are marketplace-neutral; Renders are per marketplace."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Condition = Literal["NWT", "NWOT", "like_new", "excellent", "good", "fair"]
Color = Literal["Red", "Pink", "Orange", "Yellow", "Green", "Blue", "Purple", "Gold", "Silver",
                "Black", "Gray", "White", "Cream", "Brown", "Tan"]
Source = Literal["photo", "note", "derived", "owner", "none"]
KidsGender = Literal["girls", "boys", "unisex"]

CONDITION_LABEL = {
    "NWT": "New with tags", "NWOT": "New without tags", "like_new": "Like new",
    "excellent": "Excellent used condition", "good": "Good used condition", "fair": "Fair condition",
}
# The Poshmark condition each grade goes up as, in the words the owner's card shows (WO20): the form selects the full
# label of post/poshmark.py:CONDITION_TO_POSH ("New With Tags (NWT)", "Like New", "Good"); Fair is never used.
POSH_CONDITION = {"NWT": "NWT", "NWOT": "Like New", "like_new": "Like New", "excellent": "Like New", "good": "Good",
                  "fair": "Good"}
PhotoRoleName = Literal["front", "back", "side", "detail", "label", "tag", "flaw", "worn", "box", "other"]
ITEM_ALONE = ("front", "side", "back")       # photos of the whole item and nothing else: the only possible covers


class Ev(BaseModel):
    """A fact plus the proof for it."""
    value: str | None = Field(None, description="The value, or null if it cannot be read")
    photos: list[int] = Field(default_factory=list, description="Indices of photos that show it")
    source: Source = Field("none", description="photo = read from a photo; note = seller's note; "
                                               "derived = standard conversion (e.g. EU→US shoe size); "
                                               "owner = the owner's answer in Telegram (set by code, never by you)")
    confidence: float = Field(0.0, ge=0, le=1)

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v):
        """A size the model emits as a JSON number (7.5, 8) is still a value; a blank string is not one."""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
        if isinstance(v, str):
            return v.strip() or None
        return v


class Flaw(BaseModel):
    description: str
    photos: list[int] = Field(default_factory=list)


class View(BaseModel):
    """One photo in the front/back comparison (brain/cover.py, WO23)."""
    photo: int
    view: Literal["front", "back", "side", "unclear"] = Field(description="Which side of the item this photo shows")
    design: Literal["none", "some", "strong"] = Field("none", description="How much printed design it shows: a print, "
                                                                          "graphic, text or logo (not seams, zips or "
                                                                          "pockets)")
    upright: Literal[0, 90, 180, 270] = Field(0, description="Clockwise degrees that would put the item upright: "
                                                             "collar, neckline or waistband at the top; shoes sole "
                                                             "down. 0 when it already is")


class FrontOut(BaseModel):
    views: list[View] = Field(description="Every photo you were shown, once")
    front: int = Field(description="The number of the photo that shows the FRONT of the item")


class SizeLabel(BaseModel):
    printed: str | None = Field(None, description="The size exactly as printed, every system and unit on the label "
                                                  "(e.g. '4 ans / 104 cm', '4A', 'EU 38 / US 7.5'); null if unreadable")


class PhotoRole(BaseModel):
    photo: int
    role: PhotoRoleName = Field(description="front = the item alone from the front (print, buttons, neckline; shoes: "
                                            "the outer side or the pair from the front), flat lay or on a hanger; "
                                            "back / side = the item alone from the back / side; detail = a close-up "
                                            "of part of the item; label = brand/size/care label or insole stamp; "
                                            "tag = hang tag or price tag; flaw = a close-up of a flaw; worn = on a "
                                            "person (try-on, mirror); box = box or packaging; other")


class Facts(BaseModel):
    item_type: str = Field(description="Plain noun phrase, e.g. 'suede ankle boots', 'wrap midi dress'")
    department: Literal["Women", "Men", "Kids", "Unisex", "Home"]
    kids_gender: KidsGender | None = Field(None, description="Kids items only: girls | boys | unisex, your best reading "
                                                             "of the item itself (style, colour, the box or label, a "
                                                             "retail screenshot). It picks Poshmark's Girls or Boys "
                                                             "size list. null for adults")
    kids_gender_confidence: float = Field(0.0, ge=0, le=1, description="Kids items: how sure you are of kids_gender "
                                                                       "(0..1); below 0.70 the owner is asked")
    category: str = Field(description="The real Poshmark category, from evidence (labels, retailer page, sizing): "
                                      "Shoes, Dresses, Tops, Sweaters, Jackets & Coats, Swim, Skirts, Shorts, "
                                      "Pants & Jumpsuits, Jeans, Bags, Accessories, Intimates & Sleepwear… (Kids: "
                                      "Shirts & Tops, Bottoms, Dresses, Shoes…). Never 'Other', never a department "
                                      "(Women, Men, Kids, Home)")
    subcategory: str | None = Field(None, description="The real Poshmark subcategory, e.g. Ankle Boots & Booties, "
                                                      "Flats & Loafers, Maxi. Never 'Other'")
    brand: Ev
    style_name: Ev = Field(default_factory=Ev, description="Model/style name if printed on a label or shown on a "
                                                           "retail screenshot, e.g. 'Gizeh', 'Arizona'")
    size_printed: Ev = Field(description="Size exactly as printed on the label/insole, every system and unit "
                                         "('4 ans / 104 cm', '4A', 'EU 38 / US 7.5', 'M')")
    size_us: Ev = Field(description="US size. For EU shoe sizes use source=derived")
    size_eu: Ev = Field(default_factory=Ev)
    colors: list[Color] = Field(description="1-2 main colors from the palette")
    color_name: str | None = Field(None, description="Natural color words for copy, e.g. 'chocolate brown'; the "
                                                     "retailer's color name when a retail screenshot shows it")
    material: Ev = Field(default_factory=Ev, description="Only from a care/content label or insole stamp")
    retail_price: Ev = Field(default_factory=Ev, description="Full retail price, digits only, e.g. '128'. Only from a "
                                                            "photo marked (retail screenshot) — cite its index, "
                                                            "source=photo — or from a seller note. Never from memory")
    retailer: Ev = Field(default_factory=Ev, description="Retailer or brand site a retail screenshot shows, e.g. "
                                                        "'Nordstrom'; cite the screenshot index, source=photo")
    condition: Condition
    condition_alternative: Condition | None = Field(None, description="Only when unsure between two grades: the "
                                                                      "other grade you weighed, else null")
    condition_evidence: Ev = Field(description="Photo(s) and the observation that justify the condition grade "
                                               "(soles, insoles, pilling, tag), from the seller's own photos only — "
                                               "never a retail screenshot. For NWT include the hang-tag photo "
                                               "(also given in hang_tag_photo)")
    hang_tag_photo: int | None = Field(None, description="Index of the seller's OWN photo showing an ATTACHED retail "
                                                         "hang tag, else null. Required for NWT. A box, a loose tag "
                                                         "or a retail screenshot does not count")
    unworn: Ev = Field(default_factory=Ev, description="Shoes only (any department): is the pair unworn? value "
                                                        "'yes' or 'no', with your confidence in that value and the "
                                                        "photos that show it (soles, insoles, toe box, box, tags, "
                                                        "sole stickers). null for anything that isn't shoes")
    box_photo: int | None = Field(None, description="Shoes: index of the seller's OWN photo showing the shoe box, "
                                                    "else null")
    flaws: list[Flaw] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list, description="Visible details: lining, hardware, heel height…")
    photo_roles: list[PhotoRole] = Field(default_factory=list, description="One entry per photo: what it shows")
    cover_photo: int = Field(description="The cover: the item alone, its FRONT, flat lay or on a hanger, clean "
                                         "background — also when it lies sideways or upside down in the photo. Never "
                                         "the back, never worn/try-on/mirror, never a label, tag, flaw close-up or "
                                         "screenshot")
    cover_upright: int = Field(0, description="Leave 0: set by code from the front check (clockwise degrees that turn "
                                              "the cover upright)")
    photo_order: list[int] = Field(description="All photo indices: cover, back/sides, details, labels, flaws, worn")
    questions: list[str] = Field(default_factory=list,
                                 description="What the seller must answer because a photo can't settle it")


class PriceResult(BaseModel):
    target: int | None
    list_price: int | None
    source: Literal["brand", "brand_category", "category_default", "default", "note", "owner", "none"]
    by_marketplace: dict[str, int] = Field(default_factory=dict)
    basis: str = ""
    original_price: int | None = None      # retail price from a seller note; a retailer screenshot takes precedence


class CopyOut(BaseModel):
    # min_length=1 on the texts: an empty field fails validation, which llm.ask sends back for a one-shot repair
    # instead of letting an empty listing through.
    poshmark_title: str = Field(min_length=1,
                                description="≤80 chars. Brand first, item, key detail, color, 'size X'")
    poshmark_description: str = Field(min_length=1)
    # No max_length on the lists: a model that returns one tag too many must not fail validation and kill
    # the whole call — copy.clean() truncates instead.
    poshmark_style_tags: list[str] = Field(default_factory=list, description="Up to 3 short style tags")
    depop_description: str = Field(min_length=1, description="≤1000 chars INCLUDING the hashtag line")
    depop_hashtags: list[str] = Field(description="Exactly 5, no # sign")


class Render(BaseModel):
    marketplace: Literal["poshmark", "depop"]
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    brand: str | None
    department: str
    category: str
    subcategory: str | None
    size: str | None
    kids_gender: KidsGender | None = None   # Kids: picks Poshmark's Girls/Boys size tab (unisex -> Girls)
    colors: list[str]
    condition: Condition
    price: int
    original_price: int | None = None
    photos: list[str]
    sku: str


class Unsupported(BaseModel):
    text: str
    reason: str


class VerifyOut(BaseModel):
    unsupported: list[Unsupported] = Field(default_factory=list,
                                           description="Claims in the copy not backed by the facts")
    poshmark_title: str = Field(min_length=1)
    poshmark_description: str = Field(min_length=1)
    depop_description: str = Field(min_length=1)


def tool_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $refs inlined (safer for tool input_schema).

    A field like `size_us: Ev = Field(description=...)` is emitted as {"$ref": ..., "description": ...}; the
    siblings of the $ref (the field's own description) win over the referenced model's docstring."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                siblings = {k: resolve(v) for k, v in node.items() if k != "$ref"}
                return {**resolve(defs[node["$ref"].split("/")[-1]]), **siblings}
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)
