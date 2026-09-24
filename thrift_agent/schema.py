"""Data contracts. Facts are marketplace-neutral; Renders are per marketplace."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Condition = Literal["NWT", "NWOT", "like_new", "excellent", "good", "fair"]
Color = Literal["Red", "Pink", "Orange", "Yellow", "Green", "Blue", "Purple", "Gold", "Silver",
                "Black", "Gray", "White", "Cream", "Brown", "Tan"]
Source = Literal["photo", "note", "derived", "none"]

CONDITION_LABEL = {
    "NWT": "New with tags", "NWOT": "New without tags", "like_new": "Like new",
    "excellent": "Excellent used condition", "good": "Good used condition", "fair": "Fair condition",
}


class Ev(BaseModel):
    """A fact plus the proof for it."""
    value: str | None = Field(None, description="The value, or null if it cannot be read")
    photos: list[int] = Field(default_factory=list, description="Indices of photos that show it")
    source: Source = Field("none", description="photo = read from a photo; note = seller's note; "
                                               "derived = standard conversion (e.g. EU→US shoe size)")
    confidence: float = Field(0.0, ge=0, le=1)


class Flaw(BaseModel):
    description: str
    photos: list[int] = Field(default_factory=list)


class Facts(BaseModel):
    item_type: str = Field(description="Plain noun phrase, e.g. 'suede ankle boots', 'wrap midi dress'")
    department: Literal["Women", "Men", "Kids", "Unisex", "Home"]
    category: str = Field(description="Poshmark category, e.g. Shoes, Dresses, Tops, Sweaters, "
                                      "Jackets & Coats, Swim, Skirts, Shorts, Pants & Jumpsuits, Jeans, "
                                      "Bags, Accessories, Intimates & Sleepwear")
    subcategory: str | None = Field(None, description="e.g. Ankle Boots & Booties, Flats & Loafers, Maxi")
    brand: Ev
    style_name: Ev = Field(default_factory=Ev, description="Model/style name if printed, e.g. 'Gizeh'")
    size_printed: Ev = Field(description="Size exactly as printed on the label/insole")
    size_us: Ev = Field(description="US size. For EU shoe sizes use source=derived")
    size_eu: Ev = Field(default_factory=Ev)
    colors: list[Color] = Field(description="1-2 main colors from the palette")
    color_name: str | None = Field(None, description="Natural color words for copy, e.g. 'chocolate brown'")
    material: Ev = Field(default_factory=Ev, description="Only from a care/content label or insole stamp")
    condition: Condition
    condition_evidence: Ev = Field(description="For NWT: the photo showing the attached hang tag")
    flaws: list[Flaw] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list, description="Visible details: lining, hardware, heel height…")
    cover_photo: int = Field(description="Best photo for the cover (full item, clean)")
    photo_order: list[int] = Field(description="All photo indices: cover, back/sides, details, labels, flaws")
    questions: list[str] = Field(default_factory=list,
                                 description="What the seller must answer because a photo can't settle it")


class PriceResult(BaseModel):
    target: int | None
    list_price: int | None
    source: Literal["brand", "brand_category", "category_default", "note", "none"]
    by_marketplace: dict[str, int] = Field(default_factory=dict)
    basis: str = ""


class CopyOut(BaseModel):
    poshmark_title: str = Field(description="≤80 chars. Brand first, item, key detail, color, 'size X'")
    poshmark_description: str
    # No max_length on the lists: a model that returns one tag too many must not fail validation and kill
    # the whole call — copy.clean() truncates instead.
    poshmark_style_tags: list[str] = Field(default_factory=list, description="Up to 3 short style tags")
    depop_description: str = Field(description="≤1000 chars INCLUDING the hashtag line")
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
    poshmark_title: str
    poshmark_description: str
    depop_description: str


def tool_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $refs inlined (safer for tool input_schema)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)
