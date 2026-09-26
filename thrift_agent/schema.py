"""Data contracts. Facts are marketplace-neutral; Renders are per marketplace."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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


class Facts(BaseModel):
    item_type: str = Field(description="Plain noun phrase, e.g. 'suede ankle boots', 'wrap midi dress'")
    department: Literal["Women", "Men", "Kids", "Unisex", "Home"]
    category: str = Field(description="The real Poshmark category, from evidence (labels, retailer page, sizing): "
                                      "Shoes, Dresses, Tops, Sweaters, Jackets & Coats, Swim, Skirts, Shorts, "
                                      "Pants & Jumpsuits, Jeans, Bags, Accessories, Intimates & Sleepwear… Never 'Other'")
    subcategory: str | None = Field(None, description="The real Poshmark subcategory, e.g. Ankle Boots & Booties, "
                                                      "Flats & Loafers, Maxi. Never 'Other'")
    brand: Ev
    style_name: Ev = Field(default_factory=Ev, description="Model/style name if printed on a label or shown on a "
                                                           "retail screenshot, e.g. 'Gizeh', 'Arizona'")
    size_printed: Ev = Field(description="Size exactly as printed on the label/insole")
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
    condition_evidence: Ev = Field(description="Photo(s) and the observation that justify the condition grade "
                                               "(soles, insoles, pilling, tag), from the seller's own photos only — "
                                               "never a retail screenshot. For NWT include the hang-tag photo "
                                               "(also given in hang_tag_photo)")
    hang_tag_photo: int | None = Field(None, description="Index of the seller's OWN photo showing an ATTACHED retail "
                                                         "hang tag, else null. Required for NWT. A box, a loose tag "
                                                         "or a retail screenshot does not count")
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
