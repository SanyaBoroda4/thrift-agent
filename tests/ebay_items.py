"""Twenty items as the cross-lister sees them (WO57 §6), for the eBay mapping tests: the kinds the owner's closet
holds — among them a kids' shoe, a 12-month snowsuit, a handbag, an item with no brand and the dog coat. Made here,
with no photo and no private data; the same checks run over real items when private/tests/ebay/items.json exists."""
from __future__ import annotations

from thrift_agent.catalogs.common import ItemView
from thrift_agent.schema import Ev, Facts, Fiber, Premium, Render

IID = "i_261110_abc123"


def view(dept="Women", cat="Dresses", sub="Midi", item_type="wrap midi dress", title="J.Crew Wrap Midi Dress Navy size 8",
         size_value="8", size_us="8", printed="8", colors=("Blue",), cond="good", kids=None, tab="Standard",
         color_name=None, comp=(), material=None, vintage=None, made_in=None, style_name=None, brand="J.Crew",
         label=None, price=35, n_photos=8, features=(), desc="Wrap dress with a tie waist.\n\nGently pre-loved, please "
         "see photos for condition.", iid=IID) -> ItemView:
    premium = None
    if comp or vintage or made_in:
        premium = Premium(composition=[Fiber(fiber=f, pct=p) for f, p in comp], vintage=Ev(value=vintage),
                          made_in=Ev(value=made_in))
    f = Facts(item_type=item_type, department=dept, kids_gender=kids, category=cat, subcategory=sub,
              kids_gender_confidence=0.9 if kids in ("girls", "boys") else 0.0,   # a sure reading (or hers)
              brand=Ev(value=brand), size_printed=Ev(value=printed, source="photo"),
              size_us=Ev(value=size_us, source="photo"), colors=list(colors), color_name=color_name, condition=cond,
              condition_evidence=Ev(), cover_photo=0, photo_order=list(range(n_photos)), premium=premium,
              material=Ev(value=material, source="photo") if material else Ev(), features=list(features),
              style_name=Ev(value=style_name) if style_name else Ev())
    photos = ["/w/cover.jpg"] + [f"/w/photos/{i:02d}.jpg" for i in range(1, n_photos)]
    r = Render(marketplace="poshmark", title=title, description=desc, tags=[], brand=brand, brand_label=label,
               department=dept, category=cat, subcategory=sub, size=size_us, kids_gender=kids, size_tab=tab,
               size_value=size_value, colors=list(colors), condition=cond, price=price, photos=photos, sku=iid)
    return ItemView(iid=iid, facts=f, render=r, price=price)


# name → (the item, the stand-in eBay category it belongs in, a few values the listing must carry)
ITEMS: dict[str, tuple[dict, str, dict]] = {
    "wrap dress": (dict(), "63861", {"Brand": "J.Crew", "Size": "8", "Size Type": "Regular", "Dress Length": "Midi",
                                     "Style": "Wrap", "Department": "Women", "Color": "Blue"}),
    "plus dress": (dict(item_type="floral maxi dress", sub="Maxi", title="Torrid Floral Maxi Dress Pink size 16W",
                        size_value="16W", size_us="16W", printed="16W", tab="Plus", brand="Torrid", colors=("Pink",)),
                   "63861", {"Size": "16W", "Size Type": "Plus", "Style": "Maxi", "Pattern": "Floral"}),
    "silk blouse": (dict(cat="Tops", sub="Blouses", item_type="silk blouse", title="Equipment 100% Silk Blouse Ivory "
                         "size S", size_value="S", size_us="S", printed="S", colors=("Cream",), brand="Equipment",
                         comp=(("silk", 100),), cond="like_new"),
                    "53159", {"Type": "Blouse", "Size": "S", "Color": "Ivory", "Material": "Silk"}),
    "cashmere cardigan": (dict(cat="Sweaters", sub="Cardigans", item_type="cashmere cardigan",
                               title="Vince Cashmere Cardigan Gray size M", size_value="M", size_us="M", printed="S/M",
                               colors=("Gray",), brand="Vince", comp=(("cashmere", 100),), made_in="Italy"),
                          "63866", {"Type": "Cardigan", "Size": "M", "Color": "Gray", "Material": "Cashmere",
                                    "Country/Region of Manufacture": "Italy"}),
    "jeans": (dict(cat="Jeans", sub="Straight Leg", item_type="high rise straight jeans",
                   title="Levi's 501 High Rise Straight Jeans Medium Wash size 27", size_value="Waist 27", size_us="27",
                   printed="W27 L30", brand="Levi's", style_name="501"),
              "11554", {"Size": "27", "Style": "Straight", "Brand": "Levi's", "Rise": "High", "Material": "Denim"}),
    "wide leg pants": (dict(cat="Pants & Jumpsuits", sub="Wide Leg", item_type="wide leg sweater pants",
                            title="J.Crew 100% Merino Wide Leg Sweater Pants Blue size M", size_value="M", size_us="M",
                            printed="M", comp=(("merino wool", 100),)),
                       "63863", {"Style": "Wide Leg", "Size": "M"}),
    "puffer jacket": (dict(cat="Jackets & Coats", sub="Puffers", item_type="quilted puffer jacket",
                           title="The North Face Quilted Puffer Jacket Black size XL", size_value="XL", size_us="XL",
                           printed="XL", colors=("Black",), brand="The North Face", cond="NWT", price=120),
                      "63862", {"Type": "Jacket", "Style": "Quilted", "Size": "XL", "Outer Shell Material": "Unknown"}),
    "pleated skirt": (dict(cat="Skirts", sub="Mini", item_type="pleated mini skirt",
                           title="Zara Pleated Mini Skirt Black size XS", size_value="XS", size_us="XS", printed="XS",
                           colors=("Black",), brand="Zara"),
                      "63864", {"Style": "Pleated", "Skirt Length": "Short", "Size": "XS"}),
    "petite jumpsuit": (dict(cat="Pants & Jumpsuits", sub="Jumpsuits & Rompers", item_type="sleeveless jumpsuit",
                             title="Banana Republic Sleeveless Jumpsuit Black size 4P", size_value="4P", size_us="4P",
                             printed="4P", tab="Petite", colors=("Black",), brand="Banana Republic"),
                        "3009", {"Type": "Jumpsuit", "Size": "4", "Size Type": "Petite"}),
    "women's sneakers": (dict(cat="Shoes", sub="Sneakers", item_type="leather sneakers",
                              title="Nike Air Force 1 Leather Sneakers White size 8.5", size_value="8.5", size_us="8.5",
                              printed="US 8.5 / EU 40", colors=("White",), brand="Nike", style_name="Air Force 1",
                              material="leather", price=60),
                         "95672", {"US Shoe Size": "8.5", "Type": "Athletic", "Style": "Sneaker", "Brand": "Nike",
                                   "Upper Material": "Leather"}),
    "ankle boots": (dict(cat="Shoes", sub="Ankle Boots & Booties", item_type="suede ankle boots",
                         title="Sam Edelman Suede Ankle Boots Tan size 7", size_value="7", size_us="7", printed="7M",
                         colors=("Tan",), brand="Sam Edelman", cond="excellent"),
                    "53557", {"US Shoe Size": "7", "Style": "Ankle", "Upper Material": "Suede"}),
    "loafers": (dict(cat="Shoes", sub="Flats & Loafers", item_type="leather loafers",
                     title="Tory Burch Leather Loafers Black size 9", size_value="9", size_us="9", printed="9M",
                     colors=("Black",), brand="Tory Burch", material="leather", price=95),
                "45333", {"Style": "Loafer", "US Shoe Size": "9", "Upper Material": "Leather"}),
    "handbag": (dict(cat="Bags", sub="Shoulder Bags", item_type="leather shoulder bag",
                     title="Coach Leather Shoulder Bag Brown", size_value="One Size", size_us="OS", printed=None,
                     colors=("Brown",), brand="Coach", price=100, material="leather"),
                "169291", {"Brand": "Coach", "Style": "Shoulder Bag", "Exterior Color": "Brown",
                           "Exterior Material": "Leather", "Department": "Women"}),
    "scarf": (dict(cat="Accessories", sub="Scarves & Wraps", item_type="wool scarf", title="Burberry Wool Scarf Beige",
                   size_value="One Size", size_us="OS", printed=None, colors=("Tan",), brand="Burberry",
                   comp=(("wool", 100),), price=80),
              "45238", {"Type": "Scarf", "Size": "One Size", "Material": "Wool"}),
    "men's flannel": (dict(dept="Men", cat="Shirts", sub="Casual Button Down Shirts", item_type="plaid flannel shirt",
                           title="L.L.Bean Plaid Flannel Shirt Red size L", size_value="L", size_us="L", printed="L",
                           colors=("Red",), brand="L.L.Bean", features=("long sleeve",)),
                      "57990", {"Type": "Button-Down", "Size": "L", "Department": "Men", "Sleeve Length": "Long Sleeve",
                                "Pattern": "Plaid"}),
    "men's sneakers": (dict(dept="Men", cat="Shoes", sub="Sneakers", item_type="running sneakers",
                            title="New Balance 990v5 Running Sneakers Gray size 10.5", size_value="10.5",
                            size_us="10.5", printed="10.5 D", colors=("Gray",), brand="New Balance", price=250,
                            cond="NWT"),
                       "15709", {"US Shoe Size": "10.5", "Style": "Running", "Department": "Men"}),
    "kids' shoe": (dict(dept="Kids", cat="Shoes", sub="Sneakers", item_type="toddler girl sneakers", kids="girls",
                        title="Nike Toddler Girl Sneakers Pink Toddler size 7.5", size_value="7.5 (Toddler Girl)",
                        size_us="7.5", printed="7.5C", tab="Girls", colors=("Pink",), brand="Nike", price=20),
                   "57974", {"US Shoe Size": "8", "Department": "Girls", "Type": "Sneaker", "Brand": "Nike"}),
    "12M snowsuit": (dict(dept="Kids", cat="Jackets & Coats", sub="Pram Suits", item_type="fleece-lined snowsuit",
                          kids="unisex", title="Carter's Fleece Lined Snowsuit Navy size 12M", size_value="12 Months",
                          size_us="12M", printed="12M", tab="Baby", colors=("Blue",), brand="Carter's", price=18),
                     "147324", {"Size": "12 Months", "Type": "Snowsuit", "Department": "Baby", "Brand": "Carter's"}),
    "no-brand top": (dict(cat="Tops", sub="Tees - Short Sleeve", item_type="striped cotton tee", brand=None,
                          title="Striped Cotton Tee White size M", size_value="M", size_us="M", printed="M",
                          colors=("White", "Blue"), material="100% cotton", price=12),
                     "53159", {"Brand": "Unbranded", "Type": "T-Shirt", "Size": "M", "Pattern": "Striped",
                               "Material": "Cotton"}),
    "dog coat": (dict(cat="Accessories", sub="None", item_type="quilted dog coat", brand=None,
                      title="Quilted Dog Coat Green size M", size_value="M", size_us="M", printed="M",
                      colors=("Green",), price=15, cond="NWOT"),
                 "177789", {"Type": "Coat/Jacket", "Size": "M", "Brand": "Unbranded", "Color": "Green"}),
}
